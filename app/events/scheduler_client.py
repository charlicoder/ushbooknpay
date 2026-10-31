"""
app/events/scheduler_client.py
──────────────────────────────
Async AWS EventBridge Scheduler client wrapping aioboto3.

Provides:
- One-time schedule creation for voucher delivery
- Schedule update (reschedule)
- Schedule cancellation (delete)
- Graceful degradation when AWS credentials or scheduler role are missing (dev mode)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

KUWAIT_TZ = ZoneInfo("Asia/Kuwait")


class SchedulerClient:
    """
    Async AWS EventBridge Scheduler client for scheduled gift voucher delivery.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            try:
                import aioboto3

                session_kwargs: dict[str, Any] = {
                    "region_name": (self._settings.AWS_REGION or "ap-south-1").split("#")[0].strip(),
                }
                access_key = (self._settings.AWS_ACCESS_KEY_ID or "").split("#")[0].strip()
                secret_key = (self._settings.AWS_SECRET_ACCESS_KEY or "").split("#")[0].strip()
                session_token = (self._settings.AWS_SESSION_TOKEN or "").split("#")[0].strip()

                if access_key:
                    session_kwargs["aws_access_key_id"] = access_key
                if secret_key:
                    session_kwargs["aws_secret_access_key"] = secret_key
                if session_token:
                    session_kwargs["aws_session_token"] = session_token

                self._session = aioboto3.Session(**session_kwargs)
            except ImportError:
                raise RuntimeError(
                    "aioboto3 is required for AWS EventBridge Scheduler. "
                    "Install with: pip install aioboto3"
                )
        return self._session

    def _client_kwargs(self) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {}
        endpoint = (
            getattr(self._settings, "scheduler_endpoint_url", None)
            or getattr(self._settings, "AWS_SCHEDULER_ENDPOINT_URL", "")
            or getattr(self._settings, "AWS_SCHEDULER_URL", "")
            or getattr(self._settings, "EVENTBRIDGE_SCHEDULER_URL", "")
            or getattr(self._settings, "AWS_ENDPOINT_URL", "")
            or ""
        )
        if isinstance(endpoint, str):
            endpoint = endpoint.split("#")[0].strip()
        if endpoint:
            client_kwargs["endpoint_url"] = endpoint
        return client_kwargs

    def _get_target_sqs_arn(self) -> str:
        if self._settings.AWS_SQS_NOTIFICATION_QUEUE_ARN:
            return self._settings.AWS_SQS_NOTIFICATION_QUEUE_ARN.strip()

        # Derive ARN from URL if standard format: https://sqs.<region>.amazonaws.com/<account>/<queue_name>
        url = (
            self._settings.AWS_SQS_NOTIFICATION_QUEUE_URL
            or getattr(self._settings, "AWS_SQS_QUEUE_URL", "")
            or getattr(self._settings, "AWS_SQS_URL", "")
            or ""
        ).split("#")[0].strip().rstrip("/")
        if "sqs." in url and ".amazonaws.com/" in url:
            parts = url.split(".amazonaws.com/")[1].split("/")
            if len(parts) >= 2:
                account_id, queue_name = parts[0], parts[1]
                region = self._settings.AWS_REGION or "ap-south-1"
                return f"arn:aws:sqs:{region}:{account_id}:{queue_name}"

        return ""

    @staticmethod
    def format_schedule_expression(dt: datetime) -> str:
        """
        Format datetime into EventBridge Scheduler expression: at(yyyy-mm-ddThh:mm:ss)
        in Kuwait timezone without offset.
        """
        if dt.tzinfo is None:
            dt_kuwait = dt.replace(tzinfo=KUWAIT_TZ)
        else:
            dt_kuwait = dt.astimezone(KUWAIT_TZ)
        return f"at({dt_kuwait.strftime('%Y-%m-%dT%H:%M:%S')})"

    async def create_voucher_schedule(
        self,
        voucher_id: str,
        scheduled_delivery_at: datetime,
    ) -> tuple[str, str]:
        """
        Create a one-time schedule in AWS EventBridge Scheduler to deliver the voucher.

        Returns:
            (schedule_name, schedule_arn)
        """
        schedule_name = f"voucher-delivery-{voucher_id}"
        group_name = self._settings.AWS_SCHEDULER_GROUP_NAME or "ush-gift-delivery"
        schedule_expr = self.format_schedule_expression(scheduled_delivery_at)
        target_arn = self._get_target_sqs_arn()
        role_arn = self._settings.AWS_SCHEDULER_ROLE_ARN

        # In dev mode or when credentials/role are not set, log and return dev mock
        if not self._settings.AWS_ACCESS_KEY_ID or not target_arn or not role_arn:
            logger.info(
                "scheduler_dev_mode_create",
                schedule_name=schedule_name,
                group_name=group_name,
                schedule_expression=schedule_expr,
                voucher_id=voucher_id,
            )
            mock_arn = f"arn:aws:scheduler:{self._settings.AWS_REGION}:mock:schedule/{group_name}/{schedule_name}"
            return schedule_name, mock_arn

        sched_dt_kuwait = (
            scheduled_delivery_at.astimezone(KUWAIT_TZ)
            if scheduled_delivery_at.tzinfo
            else scheduled_delivery_at.replace(tzinfo=KUWAIT_TZ)
        )
        payload = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "voucher.delivery_due",
            "event_name": "Voucher.DeliveryDue",
            "version": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "source": "aws_eventbridge_scheduler",
            "voucher_id": str(voucher_id),
            "scheduled_delivery_at": sched_dt_kuwait.isoformat(),
            "data": {
                "voucher_id": str(voucher_id),
                "scheduled_delivery_at": sched_dt_kuwait.isoformat(),
            },
        })

        session = self._get_session()
        async with session.client("scheduler", **self._client_kwargs()) as client:
            try:
                response = await client.create_schedule(
                    Name=schedule_name,
                    GroupName=group_name,
                    ScheduleExpression=schedule_expr,
                    ScheduleExpressionTimezone="Asia/Kuwait",
                    FlexibleTimeWindow={"Mode": "OFF"},
                    Target={
                        "Arn": target_arn,
                        "RoleArn": role_arn,
                        "Input": payload,
                    },
                    ActionAfterCompletion="NONE",
                    Description=f"Deliver gift voucher {voucher_id} at scheduled Kuwait time",
                )
                schedule_arn = response.get("ScheduleArn", "")
                logger.info(
                    "scheduler_schedule_created",
                    schedule_name=schedule_name,
                    schedule_arn=schedule_arn,
                    voucher_id=voucher_id,
                )
                return schedule_name, schedule_arn
            except Exception as e:
                logger.error(
                    "scheduler_create_failed",
                    schedule_name=schedule_name,
                    voucher_id=voucher_id,
                    error=str(e),
                )
                raise

    async def update_voucher_schedule(
        self,
        schedule_name: str,
        voucher_id: str,
        new_scheduled_delivery_at: datetime,
    ) -> str:
        """
        Update an existing schedule with a new delivery time.
        """
        group_name = self._settings.AWS_SCHEDULER_GROUP_NAME or "ush-gift-delivery"
        schedule_expr = self.format_schedule_expression(new_scheduled_delivery_at)
        target_arn = self._get_target_sqs_arn()
        role_arn = self._settings.AWS_SCHEDULER_ROLE_ARN

        if not self._settings.AWS_ACCESS_KEY_ID or not target_arn or not role_arn:
            logger.info(
                "scheduler_dev_mode_update",
                schedule_name=schedule_name,
                group_name=group_name,
                new_schedule_expression=schedule_expr,
                voucher_id=voucher_id,
            )
            return f"arn:aws:scheduler:{self._settings.AWS_REGION}:mock:schedule/{group_name}/{schedule_name}"

        sched_dt_kuwait = (
            new_scheduled_delivery_at.astimezone(KUWAIT_TZ)
            if new_scheduled_delivery_at.tzinfo
            else new_scheduled_delivery_at.replace(tzinfo=KUWAIT_TZ)
        )
        payload = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "voucher.delivery_due",
            "event_name": "Voucher.DeliveryDue",
            "version": 1,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "source": "aws_eventbridge_scheduler",
            "voucher_id": str(voucher_id),
            "scheduled_delivery_at": sched_dt_kuwait.isoformat(),
            "data": {
                "voucher_id": str(voucher_id),
                "scheduled_delivery_at": sched_dt_kuwait.isoformat(),
            },
        })

        session = self._get_session()
        async with session.client("scheduler", **self._client_kwargs()) as client:
            try:
                response = await client.update_schedule(
                    Name=schedule_name,
                    GroupName=group_name,
                    ScheduleExpression=schedule_expr,
                    ScheduleExpressionTimezone="Asia/Kuwait",
                    FlexibleTimeWindow={"Mode": "OFF"},
                    Target={
                        "Arn": target_arn,
                        "RoleArn": role_arn,
                        "Input": payload,
                    },
                    ActionAfterCompletion="NONE",
                    Description=f"Rescheduled gift voucher {voucher_id} at Kuwait time",
                )
                schedule_arn = response.get("ScheduleArn", "")
                logger.info(
                    "scheduler_schedule_updated",
                    schedule_name=schedule_name,
                    schedule_arn=schedule_arn,
                    voucher_id=voucher_id,
                )
                return schedule_arn
            except Exception as e:
                logger.error(
                    "scheduler_update_failed",
                    schedule_name=schedule_name,
                    voucher_id=voucher_id,
                    error=str(e),
                )
                raise

    async def delete_voucher_schedule(self, schedule_name: str) -> None:
        """
        Delete a schedule (e.g. if voucher is cancelled before delivery).
        """
        group_name = self._settings.AWS_SCHEDULER_GROUP_NAME or "ush-gift-delivery"

        if not self._settings.AWS_ACCESS_KEY_ID:
            logger.info("scheduler_dev_mode_delete", schedule_name=schedule_name)
            return

        session = self._get_session()
        async with session.client("scheduler", **self._client_kwargs()) as client:
            try:
                await client.delete_schedule(
                    Name=schedule_name,
                    GroupName=group_name,
                )
                logger.info("scheduler_schedule_deleted", schedule_name=schedule_name)
            except Exception as e:
                # If already deleted or completed, ignore ResourceNotFoundException
                err_msg = str(e)
                if "ResourceNotFoundException" in err_msg or "not found" in err_msg.lower():
                    logger.debug("scheduler_schedule_already_gone", schedule_name=schedule_name)
                    return
                logger.warning(
                    "scheduler_delete_warning",
                    schedule_name=schedule_name,
                    error=err_msg,
                )


_scheduler_client: SchedulerClient | None = None


def get_scheduler_client() -> SchedulerClient:
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = SchedulerClient()
    return _scheduler_client

