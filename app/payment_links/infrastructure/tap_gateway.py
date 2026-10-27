"""
app/payment_links/infrastructure/tap_gateway.py
────────────────────────────────────────────────
Tap Payments hosted payment-link gateways (KNET, Visa/Master card, Apple Pay).

These reuse the Tap Charges API integration implemented by
:class:`~app.payment_links.infrastructure.deema_gateway.DeemaGateway` (create charge,
status lookup, refund, void, webhook signature + parsing are identical – only the
``source.id`` differs), so no Tap logic is duplicated.

Registered gateway names (stored in ``payment_links.gateway`` / ``payments.payment_gateway``):

====================  ===============  ================================================
name                  provider_label   Tap ``source.id``
====================  ===============  ================================================
``tap_knet``          Tap KNET         ``src_kw.knet``   (KNET hosted page, KWD only)
``tap_card``          Tap Card         ``src_card``      (Visa / Mastercard hosted page)
``tap_applepay``      Tap Apple Pay    ``src_apple_pay`` (Apple Pay only; shown on supported devices)
====================  ===============  ================================================

Deema (``deema``) keeps its own adapter and is grouped under the same ``Tap`` provider
family (see :func:`app.payment_links.domain.registry.provider_group_for`).

Webhook / redirect URLs are rewritten to ``/webhook/<gateway-name>/`` so the webhook route
resolves the same gateway that issued the link.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping

import httpx

from app.core.config import Settings
from app.core.exceptions import PaymentProviderError
from app.payment_links.domain.gateway import GatewayPaymentState
from app.payment_links.domain.registry import register_gateway
from app.payment_links.infrastructure.deema_gateway import DeemaGateway


class _TapSourceGateway(DeemaGateway):
    """Tap charge gateway bound to a specific Tap ``source.id``."""

    name = "tap"
    provider_label = "Tap"
    tap_source_id = "src_all"
    tap_source_setting = ""  # Settings attribute that can override ``tap_source_id``
    default_payment_method = "card"

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        super().__init__(http_client, settings)

    # Secret key / merchant: prefer the generic TAP_* credentials, fall back to the
    # Deema ones (both are the same Tap merchant in the current deployment).
    @property
    def _secret_key(self) -> str:  # type: ignore[override]
        return (
            getattr(self._settings, "TAP_SECRET_KEY", "")
            or getattr(self._settings, "DEEMA_SECRET_KEY", "")
        ).strip()

    @property
    def _merchant_id(self) -> str:  # type: ignore[override]
        return (
            getattr(self._settings, "TAP_MERCHANT_ID", "")
            or getattr(self._settings, "DEEMA_MERCHANT_ID", "")
        ).strip()

    @property
    def _source_id(self) -> str:  # type: ignore[override]
        override = getattr(self._settings, self.tap_source_setting, "") if self.tap_source_setting else ""
        return (override or self.tap_source_id).strip()

    async def create_link(self, request):  # type: ignore[override]
        try:
            return await super().create_link(request)
        except PaymentProviderError as exc:
            # The inherited messages are worded for Deema; re-label them for this source.
            msg = str(exc).replace("Deema", self.provider_label)
            if "Declined" in msg or "515" in msg:
                msg += (
                    f" ({self.provider_label} source '{self._source_id}' may not be enabled on "
                    "this Tap merchant account.)"
                )
            raise PaymentProviderError(msg, detail=getattr(exc, "detail", None), code=getattr(exc, "code", None)) from exc

    def _swap_webhook_path(self, url: str) -> str:
        return url.replace("/webhook/deema", f"/webhook/{self.name}") if url else url

    def _resolve_callback_url(self, request_callback: str | None) -> str:  # type: ignore[override]
        return self._swap_webhook_path(super()._resolve_callback_url(request_callback))

    def _resolve_webhook_url(self) -> str:  # type: ignore[override]
        return self._swap_webhook_path(super()._resolve_webhook_url())

    def _state_from_charge(  # type: ignore[override]
        self, body: Mapping[str, Any], *, fallback_id: str | None = None
    ) -> GatewayPaymentState:
        state = DeemaGateway._state_from_charge(body, fallback_id=fallback_id)
        # DeemaGateway labels an unknown method as "deema"; for Tap sources fall back to
        # the method this gateway represents instead.
        if state.payment_method == "deema":
            state = dataclasses.replace(state, payment_method=self.default_payment_method)
        return state


class TapKnetGateway(_TapSourceGateway):
    name = "tap_knet"
    provider_label = "Tap KNET"
    tap_source_id = "src_kw.knet"
    tap_source_setting = "TAP_KNET_SOURCE_ID"
    default_payment_method = "knet"


class TapCardGateway(_TapSourceGateway):
    name = "tap_card"
    provider_label = "Tap Card"
    tap_source_id = "src_card"
    tap_source_setting = "TAP_CARD_SOURCE_ID"
    default_payment_method = "card"


class TapApplePayGateway(_TapSourceGateway):
    name = "tap_applepay"
    provider_label = "Tap Apple Pay"
    tap_source_id = "src_apple_pay"
    tap_source_setting = "TAP_APPLEPAY_SOURCE_ID"
    default_payment_method = "apple_pay"


register_gateway(TapKnetGateway.name, lambda http, settings: TapKnetGateway(http, settings))
register_gateway(TapCardGateway.name, lambda http, settings: TapCardGateway(http, settings))
register_gateway(TapApplePayGateway.name, lambda http, settings: TapApplePayGateway(http, settings))
