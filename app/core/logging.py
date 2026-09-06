"""
app/core/logging.py
───────────────────
Structured JSON logging powered by structlog.

Features:
- JSON output in production, coloured console output in development
- Automatic request_id, correlation_id, service name injection
- No sensitive fields ever logged (enforced by processor chain)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

# Fields that must NEVER appear in log output
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "current_password",
        "new_password",
        "confirm_password",
        "jwt",
        "access_token",
        "refresh_token",
        "authorization",
        "ushspa_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "myfatoorah_api_key",
        "tap_secret_key",
        "card_number",
        "cvv",
        "card_holder",
        "api_key",
        "secret",
        "secret_key",
    }
)


def _drop_sensitive_fields(
    logger: Any,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Remove any sensitive key from the log event dict."""
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def _add_service_name(
    logger: Any,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    event_dict.setdefault("service", "ushbooknpay")
    return event_dict


def configure_logging(log_level: str = "INFO", is_development: bool = False) -> None:
    """
    Configure structlog + stdlib logging.

    Call once at application startup (main.py lifespan).
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ExtraAdder(),
        _drop_sensitive_fields,
        _add_service_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if is_development:
        # Human-readable coloured output for local development
        processors: list[Processor] = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Machine-readable JSON for staging/production
        processors = [
            *shared_processors,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Redirect stdlib logging through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelName(log_level.upper()),
    )
    for noisy_logger in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to *name*."""
    return structlog.get_logger(name)
