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


# Gateways that are processed through Tap Payments (grouped as one provider family in the UI
# and in the ``payment_provider_group`` trace column).
TAP_FAMILY_GATEWAYS = frozenset({"deema", "tap_knet", "tap_card", "tap_applepay"})


def provider_group_for(gateway_name: str | None) -> str | None:
    """Provider family for a gateway name: ``Tap`` | ``MyFatoorah`` | the name itself."""
    name = (gateway_name or "").strip().lower()
    if not name:
        return None
    if name in TAP_FAMILY_GATEWAYS:
        return "Tap"
    if name == "myfatoorah":
        return "MyFatoorah"
    return name


def resolve_gateway_name(provider: str | None, default: str) -> str:
    """Map a stored ``payment_provider`` label (e.g. 'MyFatoorah') to a registry key."""
    key = (provider or default or "").strip().lower().replace(" ", "")
    # Exact match first so labels such as 'Tap KNET' resolve to 'tap_knet'.
    for name in _FACTORIES:
        if name.replace("_", "") == key.replace("_", ""):
            return name
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
