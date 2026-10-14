"""
app/payment_links/domain/registry.py
─────────────────────────────────────
Gateway registry (factory). Adapters register a factory under a name; callers
resolve gateways by name so no service imports a concrete provider.
"""

from __future__ import annotations

from typing import Callable

import httpx

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.payment_links.domain.gateway import PaymentLinkGateway

GatewayFactory = Callable[[httpx.AsyncClient, Settings], PaymentLinkGateway]

_FACTORIES: dict[str, GatewayFactory] = {}


def register_gateway(name: str, factory: GatewayFactory) -> None:
    _FACTORIES[name.strip().lower()] = factory


def available_gateways() -> list[str]:
    return sorted(_FACTORIES)


def resolve_gateway_name(provider: str | None, default: str) -> str:
    """Map a stored ``payment_provider`` label (e.g. 'MyFatoorah') to a registry key."""
    key = (provider or default or "").strip().lower().replace(" ", "")
    for name in _FACTORIES:
        if name in key or key in name:
            return name
    return (default or "").strip().lower()


def get_gateway(name: str, http_client: httpx.AsyncClient, settings: Settings) -> PaymentLinkGateway:
    factory = _FACTORIES.get((name or "").strip().lower())
    if factory is None:
        raise ValidationError(
            f"Unsupported payment gateway '{name}'.",
            detail={"available": available_gateways()},
            code="UNSUPPORTED_GATEWAY",
        )
    return factory(http_client, settings)
