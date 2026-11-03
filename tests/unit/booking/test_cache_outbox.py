"""
tests/unit/booking/test_cache_outbox.py
────────────────────────────────────────
Durable retry queue for ushauth appointment-cache writes (no database needed:
the persistence helpers are patched).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.booking.application import cache_outbox_relay as relay
from app.core.exceptions import AuthorizationError, GatewayError, GatewayTimeoutError, NotFoundError
from app.integrations.ushauth_client import USHAuthClient, is_retryable_upstream_error


def _gateway_error(status: int) -> GatewayError:
    err = GatewayError(f"ushauth returned {status}")
    err.upstream_status = status  # type: ignore[attr-defined]
    return err


def _settings(enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.USHSPA_TOKEN = "t"
    s.GATEWAY_TIMEOUT = 5
    s.ushauth_base_url = "http://testserver"
    s.CACHE_OUTBOX_ENABLED = enabled
    s.CACHE_OUTBOX_MAX_ATTEMPTS = 12
    s.CACHE_OUTBOX_BATCH_SIZE = 50
    return s


def _client(enabled: bool = True) -> USHAuthClient:
    return USHAuthClient(http_client=AsyncMock(), redis_client=MagicMock(), settings=_settings(enabled))


# ── error classification ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "exc,expected",
    [
        (GatewayTimeoutError("timeout"), True),
        (httpx.ConnectError("refused"), True),
        (_gateway_error(500), True),
        (_gateway_error(503), True),
        (_gateway_error(429), True),
        (_gateway_error(400), False),
        (_gateway_error(409), False),
        (_gateway_error(422), False),
        (NotFoundError("gone"), False),
        (AuthorizationError("nope"), False),
        (ValueError("bug"), False),
    ],
)
def test_is_retryable_upstream_error(exc, expected):
    assert is_retryable_upstream_error(exc) is expected


def test_backoff_is_exponential_and_capped():
    assert [relay.backoff_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert relay.backoff_seconds(50) == 15 * 60


# ── _post_reliable ───────────────────────────────────────────────────────────

async def test_post_reliable_success_does_not_enqueue(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(relay, "enqueue_cache_request", enqueue)
    monkeypatch.setattr(relay, "has_pending_for_booking", AsyncMock(return_value=False))
    client = _client()
    client._post = AsyncMock(return_value={"success": True})

    assert await client._post_reliable("/p/", {"a": 1}, "b1") == {"success": True}
    enqueue.assert_not_awaited()


async def test_post_reliable_enqueues_retryable_failure_and_reraises(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(relay, "enqueue_cache_request", enqueue)
    monkeypatch.setattr(relay, "has_pending_for_booking", AsyncMock(return_value=False))
    client = _client()
    client._post = AsyncMock(side_effect=GatewayTimeoutError("timeout"))

    with pytest.raises(GatewayTimeoutError):
        await client._post_reliable("/p/", {"a": 1}, "b1")
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[:3] == ("/p/", {"a": 1}, "b1")


async def test_post_reliable_does_not_enqueue_permanent_failure(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(relay, "enqueue_cache_request", enqueue)
    monkeypatch.setattr(relay, "has_pending_for_booking", AsyncMock(return_value=False))
    client = _client()
    client._post = AsyncMock(side_effect=_gateway_error(400))

    with pytest.raises(GatewayError):
        await client._post_reliable("/p/", {}, "b1")
    enqueue.assert_not_awaited()


async def test_post_reliable_queues_behind_pending_request_to_keep_order(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(relay, "enqueue_cache_request", enqueue)
    monkeypatch.setattr(relay, "has_pending_for_booking", AsyncMock(return_value=True))
    client = _client()
    client._post = AsyncMock()

    result = await client._post_reliable("/status/", {"s": "cancelled"}, "b1")
    assert result == {"success": True, "queued": True}
    client._post.assert_not_awaited()
    enqueue.assert_awaited_once()


async def test_post_reliable_is_plain_post_when_outbox_disabled(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(relay, "enqueue_cache_request", enqueue)
    client = _client(enabled=False)
    client._post = AsyncMock(side_effect=GatewayTimeoutError("timeout"))

    with pytest.raises(GatewayTimeoutError):
        await client._post_reliable("/p/", {}, "b1")
    enqueue.assert_not_awaited()


# ── relay ────────────────────────────────────────────────────────────────────

def _row(booking="b1", attempts=1, max_attempts=12, path="/p/"):
    return SimpleNamespace(
        id=f"{booking}-{attempts}-{path}", booking_id=booking, path=path, payload={"k": "v"},
        attempts=attempts, max_attempts=max_attempts,
    )


@pytest.fixture
def relay_env(monkeypatch):
    monkeypatch.setattr(relay, "get_settings", _settings)
    finished: list[tuple] = []

    async def fake_finish(row_id, **values):
        finished.append((row_id, values))

    monkeypatch.setattr(relay, "_finish", fake_finish)
    return finished


async def test_relay_marks_delivered(monkeypatch, relay_env):
    row = _row()
    monkeypatch.setattr(relay, "_claim_due_batch", AsyncMock(return_value=[row]))
    client = MagicMock()
    client._post = AsyncMock(return_value={"success": True})

    stats = await relay.process_outbox_once(client)
    assert stats == {"delivered": 1, "retried": 0, "dead": 0}
    client._post.assert_awaited_once_with("/p/", json_data={"k": "v"})
    assert relay_env[0][1]["status"] == relay.DELIVERED


async def test_relay_retries_with_backoff_and_blocks_later_rows_of_same_booking(monkeypatch, relay_env):
    first, second, other = _row("b1", path="/create/"), _row("b1", path="/status/"), _row("b2")
    monkeypatch.setattr(relay, "_claim_due_batch", AsyncMock(return_value=[first, second, other]))
    client = MagicMock()
    client._post = AsyncMock(side_effect=[GatewayTimeoutError("down"), {"success": True}])

    stats = await relay.process_outbox_once(client)
    assert stats == {"delivered": 1, "retried": 1, "dead": 0}
    # the second row of b1 was NOT sent (order preserved); b2 was
    assert client._post.await_count == 2
    sent_paths = [c.args[0] for c in client._post.await_args_list]
    assert "/status/" not in sent_paths
    retried = next(v for rid, v in relay_env if rid == first.id)
    assert retried["attempts"] == 2 and "next_attempt_at" in retried


async def test_relay_marks_permanent_errors_dead(monkeypatch, relay_env):
    monkeypatch.setattr(relay, "_claim_due_batch", AsyncMock(return_value=[_row()]))
    client = MagicMock()
    client._post = AsyncMock(side_effect=_gateway_error(422))

    stats = await relay.process_outbox_once(client)
    assert stats == {"delivered": 0, "retried": 0, "dead": 1}
    assert relay_env[0][1]["status"] == relay.DEAD


async def test_relay_gives_up_after_max_attempts(monkeypatch, relay_env):
    monkeypatch.setattr(relay, "_claim_due_batch", AsyncMock(return_value=[_row(attempts=11, max_attempts=12)]))
    client = MagicMock()
    client._post = AsyncMock(side_effect=GatewayTimeoutError("down"))

    stats = await relay.process_outbox_once(client)
    assert stats["dead"] == 1
    assert relay_env[0][1]["status"] == relay.DEAD


async def test_relay_dead_row_does_not_block_following_rows(monkeypatch, relay_env):
    bad, good = _row("b1", path="/create/"), _row("b1", path="/status/")
    monkeypatch.setattr(relay, "_claim_due_batch", AsyncMock(return_value=[bad, good]))
    client = MagicMock()
    client._post = AsyncMock(side_effect=[_gateway_error(400), {"success": True}])

    stats = await relay.process_outbox_once(client)
    assert stats == {"delivered": 1, "retried": 0, "dead": 1}


async def test_enqueue_never_raises(monkeypatch):
    monkeypatch.setattr(relay, "get_settings", _settings)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(relay, "get_session_factory", boom)
    await relay.enqueue_cache_request("/p/", {}, "b1", "err")  # must not raise
