"""
tests/conftest.py
──────────────────
Shared pytest fixtures for ushbooknpay tests.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the session."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings():
    """Return test settings with safe defaults."""
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
    os.environ.setdefault("USHSPA_TOKEN", "test-token")
    os.environ.setdefault("JWT_PUBLIC_KEY", "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0000\n-----END PUBLIC KEY-----")

    from app.core.config import Settings
    return Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test",
        USHSPA_TOKEN="test-token",
        JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----",
    )
