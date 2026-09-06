"""
app/events/sqs_client.py
─────────────────────────
Async SQS client wrapping aioboto3.

Provides:
- Simple send_message interface
- Standard FIFO and non-FIFO queue support
- Graceful degradation when AWS credentials are not configured (dev mode)
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class SQSClient:
    """
    Async SQS client for publishing domain events.

    In development (no AWS credentials configured), messages are logged
    instead of sent to SQS so local development doesn't require AWS.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session: Any = None

    def _get_session(self) -> Any:
        """Return the aioboto3 session (created lazily)."""
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
                    "aioboto3 is required for SQS publishing. "
                    "Install with: pip install aioboto3"
                )
        return self._session

    async def send_message(
        self,
        queue_url: str,
        message_body: str,
        message_group_id: str | None = None,
        message_attributes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Send a message to an SQS queue.

        Args:
            queue_url: Full SQS queue URL.
            message_body: Message body (JSON string).
            message_group_id: Required for FIFO queues.
            message_attributes: Optional metadata attributes.

        Returns:
            SQS send response dict.

        Note:
            In development mode (no credentials), logs the message instead of sending.
        """
        if not self._settings.AWS_ACCESS_KEY_ID:
            # Dev mode — log instead of send
            logger.info(
                "sqs_dev_mode_message",
                queue_url=queue_url,
                message_group_id=message_group_id,
                body_preview=message_body[:200],
            )
            return {"MessageId": "dev-mode-no-send", "MD5OfMessageBody": ""}

        kwargs: dict[str, Any] = {
            "QueueUrl": queue_url,
            "MessageBody": message_body,
        }

        if message_group_id:
            import hashlib

            kwargs["MessageGroupId"] = message_group_id
            kwargs["MessageDeduplicationId"] = hashlib.md5(
                message_body.encode()
            ).hexdigest()

        if message_attributes:
            kwargs["MessageAttributes"] = {
                k: {"DataType": "String", "StringValue": v}
                for k, v in message_attributes.items()
            }

        session = self._get_session()
        async with session.client("sqs") as sqs:
            response: dict[str, Any] = await sqs.send_message(**kwargs)

        logger.info(
            "sqs_message_sent",
            queue_url=queue_url,
            message_id=response.get("MessageId"),
        )
        return response

    async def publish_event(
        self,
        event: Any,
        queue_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Publish a domain event directly to SQS (fire-and-publish).
        Routes by default to AWS_SQS_NOTIFICATION_QUEUE_URL.
        Catches and logs any transport errors to prevent background task crashes.
        """
        target_queue = queue_url or self._settings.AWS_SQS_NOTIFICATION_QUEUE_URL
        if not target_queue:
            logger.warning(
                "sqs_notification_queue_url_not_configured_event_dropped",
                event_name=getattr(event, "event_name", type(event).__name__),
            )
            return {}

        # Resolve event metadata
        event_name = getattr(event, "event_name", type(event).__name__)
        event_id = getattr(event, "event_id", "")
        event_type = getattr(event, "event_type", "")

        if not event_type:
            if event_name == "Booking.Requested":
                event_type = "booking.requested"
            elif event_name == "Booking.Created":
                event_type = "booking.created"
            elif event_name == "Booking.Confirmed":
                event_type = "booking.confirmed"
            elif event_name == "Loyalty.Redeemed":
                event_type = "loyalty.redeedmed"
            elif event_name:
                event_type = event_name.lower().replace(".", "_")

        if hasattr(event, "to_json"):
            payload_str = event.to_json()
        elif isinstance(event, dict):
            event_dict = dict(event)
            event_dict.setdefault("event_type", event_type)
            event_dict.setdefault("event_name", event_name)
            payload_str = json.dumps(event_dict, default=str)
        else:
            try:
                data = json.loads(json.dumps(event, default=str))
                if isinstance(data, dict):
                    data.setdefault("event_type", event_type)
                    data.setdefault("event_name", event_name)
                    payload_str = json.dumps(data, default=str)
                else:
                    payload_str = json.dumps(event, default=str)
            except Exception:
                payload_str = json.dumps(event, default=str)

        try:
            return await self.send_message(
                queue_url=target_queue,
                message_body=payload_str,
                message_attributes={
                    "event_name": event_name,
                    "event_type": event_type,
                    "event_id": str(event_id),
                },
            )
        except Exception as exc:
            if self._settings.APP_ENV != "production":
                logger.warning(
                    "sqs_publish_event_dev_notice",
                    event_name=event_name,
                    event_id=str(event_id),
                    queue_url=target_queue,
                    error=str(exc),
                )
            else:
                logger.error(
                    "sqs_publish_event_failed",
                    event_name=event_name,
                    event_id=str(event_id),
                    queue_url=target_queue,
                    error=str(exc),
                )
            return {"error": str(exc)}


_sqs_client: SQSClient | None = None


def get_sqs_client() -> SQSClient:
    """Return the application SQS client singleton."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = SQSClient()
    return _sqs_client
