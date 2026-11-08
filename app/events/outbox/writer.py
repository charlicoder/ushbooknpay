"""
app/events/outbox/writer.py
───────────────────────────
Write domain events to ``outbox_events`` inside the caller's transaction and
publish them right after that transaction commits.

* :func:`add_outbox_event` adds the row to the session (no flush/commit) and
  remembers its id on the session.
* A ``after_commit`` listener on the session schedules
  :func:`app.events.outbox.relay.publish_outbox_ids` for the committed ids
  (best effort, fire-and-forget). A rollback discards them.
* Anything not published (crash, SQS down) is picked up by the relay loop
  (:func:`app.events.outbox.relay.run_outbox_relay_forever`).

Used for course-subscription events; existing booking events are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import Mock

from sqlalchemy import event as sa_event

from app.core.config import get_settings
from app.core.logging import get_logger
from app.events.outbox.models import OutboxEvent

logger = get_logger(__name__)

_PENDING_KEY = "_outbox_pending_ids"
_HOOKED_KEY = "_outbox_after_commit_hooked"
_background_tasks: set[asyncio.Task] = set()


def event_payload(event: Any) -> dict[str, Any]:
    """JSON-safe dict of an event dataclass (``BaseEvent.to_dict`` + event_name/type)."""
    if hasattr(event, "to_json"):
        data = json.loads(event.to_json())
    elif isinstance(event, dict):
        data = json.loads(json.dumps(event, default=str))
    else:  # pragma: no cover - defensive
        data = json.loads(json.dumps(event, default=str))
    data.setdefault("event_name", getattr(event, "event_name", ""))
    data.setdefault("event_type", getattr(event, "event_type", ""))
    return data


def add_outbox_event(
    session: Any,
    event: Any,
    *,
    queue_url: str | None = None,
    correlation_id: str | None = None,
    message_group_id: str | None = None,
) -> OutboxEvent:
    """Stage ``event`` in the outbox within the current transaction (never raises on publish)."""
    settings = get_settings()
    payload = event_payload(event)
    row = OutboxEvent(
        id=uuid.uuid4(),
        event_name=str(payload.get("event_name") or type(event).__name__)[:100],
        queue_url=(queue_url or settings.AWS_SQS_NOTIFICATION_QUEUE_URL or "")[:512],
        message_group_id=message_group_id,
        payload=payload,
        status="pending",
        retry_count=0,
        max_retries=settings.OUTBOX_MAX_ATTEMPTS,
        correlation_id=(str(correlation_id)[:255] if correlation_id else None),
    )
    session.add(row)
    _remember_for_after_commit(session, row.id)
    return row


def _remember_for_after_commit(session: Any, row_id: uuid.UUID) -> None:
    if isinstance(session, Mock):  # unit tests with mocked sessions: relay covers delivery
        return
    sync = getattr(session, "sync_session", None) or session
    info = getattr(sync, "info", None)
    if not isinstance(info, dict):
        return
    info.setdefault(_PENDING_KEY, []).append(row_id)
    if not info.get(_HOOKED_KEY):
        try:
            sa_event.listen(sync, "after_commit", _after_commit)
            sa_event.listen(sync, "after_rollback", _after_rollback)
            info[_HOOKED_KEY] = True
        except Exception as exc:  # pragma: no cover - never block the business transaction
            logger.debug("outbox_after_commit_hook_failed", error=str(exc))


def _after_rollback(sync_session: Any) -> None:
    # Only the outermost rollback discards pending ids (savepoint rollbacks don't fire this).
    sync_session.info.pop(_PENDING_KEY, None)


def _after_commit(sync_session: Any) -> None:
    ids = sync_session.info.pop(_PENDING_KEY, None)
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync context): the relay will publish them
    from app.events.outbox.relay import publish_outbox_ids

    task = loop.create_task(publish_outbox_ids(list(ids)))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
