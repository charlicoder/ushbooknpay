"""
app/payment_links/api/router.py
────────────────────────────────
HTTP API for payment links.

Employee endpoints (ushdesk) — each guarded by an RBAC permission codename
that is seeded in ushauth (``payment_links.*``):

  POST   /payment-links/                      payment_links.create
  GET    /payment-links/                      payment_links.list     (?target_type&target_id)
  GET    /payment-links/{link_id}/            payment_links.view
  POST   /payment-links/{link_id}/sync/       payment_links.view     (pull status from gateway)
  POST   /payment-links/{link_id}/renew/      payment_links.create   (new link for an expired unpaid one)
  POST   /payment-links/{link_id}/cancel/     payment_links.cancel
  POST   /payment-links/{link_id}/refund/     payment_links.refund

Public server-to-server endpoint (authenticated by the gateway signature, NOT
by JWT / app token — the gateway cannot send those):

  POST   /payment-links/webhook/{gateway}/
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.api.deps import AppSettings, DBSession, get_http_client
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import TokenPayload, require_permission
from app.payment_links import get_gateway
from app.payment_links.api.schemas import (
    CreatePaymentLinkRequest,
    PaymentLinkOut,
    RefundOut,
    RefundPaymentRequest,
)
from app.payment_links.application.link_service import PaymentLinkService
from app.payment_links.application.refund_service import PaymentLinkRefundService
from app.payment_links.application.settlement import PaymentSettlementService
from app.payment_links.application.webhook_service import PaymentWebhookService
from app.payment_links.domain.gateway import GatewayPaymentStatus

router = APIRouter(prefix="/payment-links", tags=["Payment Links"])
logger = get_logger(__name__)


def _ok(data: Any, code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(status_code=code, content={"success": True, "data": jsonable_encoder(data)})


def _build_redirect_url(base_url: str, params: dict[str, Any]) -> str:
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    existing_query = dict(parse_qsl(parsed.query))
    for k, v in params.items():
        if v is not None and str(v) != "":
            existing_query[k] = str(v)
    new_query = urlencode(existing_query)
    return urlunparse(parsed._replace(query=new_query))


def _actor(user: TokenPayload) -> tuple[str, dict[str, Any]]:
    return user.sub, {
        "id": user.sub,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "phone_number": user.phone_number or "",
        "email": user.email or "",
        "role": user.role,
        "user_type": user.user_type,
    }


def _gateway(name: str | None, settings: Settings):
    return get_gateway(name or settings.PAYMENT_LINK_DEFAULT_GATEWAY, get_http_client(), settings)


# ── Public webhook (declared first so it never collides with /{link_id}/) ──
@router.api_route(
    "/webhook/{gateway}",
    methods=["GET", "POST"],
    summary="Payment gateway webhook (server-to-server or callback redirect)",
    include_in_schema=False,
)
@router.api_route(
    "/webhook/{gateway}/",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def gateway_webhook(
    request: Request,
    session: DBSession,
    settings: AppSettings,
    gateway: Annotated[str, Path(min_length=2, max_length=30, pattern=r"^[a-z0-9_-]+$")],
) -> Response:
    gw = _gateway(gateway, settings)

    # 1. Handle GET requests (browser redirects after payment or gateway verification pings)
    if request.method == "GET":
        qp = dict(request.query_params)
        lookup_key = (
            qp.get("paymentId")
            or qp.get("PaymentId")
            or qp.get("Id")
            or qp.get("id")
            or qp.get("InvoiceId")
            or qp.get("invoiceId")
            or qp.get("invoice_id")
        )
        if not lookup_key:
            logger.info("gateway_webhook_ping_received", gateway=gateway)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "status": "ok",
                    "message": f"{gw.provider_label} webhook endpoint is active",
                },
            )

        logger.info("gateway_webhook_get_callback", gateway=gateway, lookup_key=lookup_key, query_params=qp)
        settlement_outcome = None
        target_booking_id = None
        link_id_str = None
        state = None
        error_msg = None
        try:
            state = await gw.get_payment_state(str(lookup_key))
            logger.info("gateway_webhook_state_verified", gateway=gateway, lookup_key=lookup_key, status=state.status.value)
            settlement = PaymentSettlementService(session, settings)
            link = await settlement.get_locked_link_by_any_identifier(
                gw.name,
                str(state.invoice_id) if state.invoice_id else str(lookup_key),
                reference_id=str(state.reference_id or lookup_key),
                payment_id=str(state.payment_id or lookup_key),
            )
            if link is not None:
                outcome = await settlement.apply(link, state, source=f"webhook_get_{gw.name}")
                settlement_outcome = getattr(outcome, "value", outcome)
                await session.commit()
                await settlement.publish_pending_events()
                target_booking_id = str(link.target_id) if link.target_type == "booking" else None
                link_id_str = str(link.id)
                logger.info(
                    "gateway_webhook_settled",
                    link_id=link_id_str,
                    outcome=settlement_outcome,
                    target_id=target_booking_id,
                )
            else:
                logger.warning("gateway_webhook_link_not_found", gateway=gateway, lookup_key=lookup_key)
                try:
                    from app.booking.infrastructure.models import Booking
                    from app.payment.domain.gateway_protocol import RefundRequest
                    candidate_inv = str(getattr(state, "invoice_id", None) or lookup_key)
                    booking_stmt = (
                        select(Booking)
                        .where(
                            Booking.payment_data["cancelled_invoice_ids"].astext.contains(candidate_inv)
                            | (Booking.payment_data["gateway_invoice_id"].astext == candidate_inv)
                        )
                        .limit(1)
                    )
                    found_booking = (await session.execute(booking_stmt)).scalar_one_or_none()
                    if found_booking:
                        target_booking_id = str(found_booking.id)
                        if state and getattr(state, "status", None) == GatewayPaymentStatus.PAID:
                            logger.info("gateway_webhook_auto_refunding_cancelled_booking", booking_id=target_booking_id, invoice_id=candidate_inv)
                            try:
                                await gw.refund_payment(
                                    RefundRequest(
                                        provider_payment_id=candidate_inv,
                                        amount=Decimal(str(state.amount)),
                                        currency=str(state.currency or "KWD"),
                                        reason="Automatic refund: booking was cancelled",
                                    )
                                )
                            except Exception as ref_exc:
                                logger.error("gateway_webhook_auto_refund_failed", error=str(ref_exc))
                            settlement_outcome = "auto_refunded_cancelled_booking"
                            error_msg = "This booking has been cancelled and this payment link is no longer valid. Your payment has been automatically refunded."
                except Exception as b_exc:
                    logger.warning("gateway_webhook_check_cancelled_booking_failed", error=str(b_exc))
        except Exception as exc:
            error_msg = str(exc)
            logger.error("gateway_webhook_callback_error", gateway=gateway, lookup_key=lookup_key, error=str(exc))

        is_paid = (
            state is not None
            and getattr(state, "status", None) == GatewayPaymentStatus.PAID
            and settlement_outcome not in ("failure_recorded", "auto_refunded_cancelled_booking")
        )
        if settlement_outcome in ("failure_recorded", "auto_refunded_cancelled_booking") and not error_msg:
            error_msg = "This booking has been cancelled and this payment link is no longer valid. Your payment has been automatically refunded."

        target_redirect_base = (
            getattr(settings, "MYFATOORAH_SUCCESS_URL", "")
            if is_paid
            else getattr(settings, "MYFATOORAH_ERROR_URL", "")
        )

        redirect_params: dict[str, Any] = {
            "paymentId": str(getattr(state, "payment_id", None) or lookup_key),
            "invoiceId": str(getattr(state, "invoice_id", None) or ""),
            "booking_id": target_booking_id,
            "link_id": link_id_str,
            "outcome": settlement_outcome,
        }
        if state and getattr(state, "amount", None) is not None:
            redirect_params["amount"] = str(state.amount)
        if state and getattr(state, "currency", None):
            redirect_params["currency"] = str(state.currency)
        if not is_paid:
            reason = (state and getattr(state, "failure_reason", None)) or error_msg or "Payment not completed"
            redirect_params["error"] = str(reason)

        if target_redirect_base:
            redirect_url = _build_redirect_url(target_redirect_base, redirect_params)
            logger.info("gateway_webhook_redirecting", url=redirect_url, is_paid=is_paid)
            return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": is_paid,
                "status": "success" if is_paid else "failed",
                "message": f"{gw.provider_label} payment processed",
                "outcome": settlement_outcome,
                "link_id": link_id_str,
                "booking_id": target_booking_id,
            },
        )

    # 2. Handle POST requests (server-to-server webhook JSON payload or empty POST with query params)
    raw_body = await request.body()
    if not raw_body or raw_body.strip() == b"" or raw_body.strip() == b"{}":
        qp = dict(request.query_params)
        lookup_key = (
            qp.get("paymentId")
            or qp.get("PaymentId")
            or qp.get("Id")
            or qp.get("id")
            or qp.get("InvoiceId")
            or qp.get("invoiceId")
            or qp.get("invoice_id")
        )
        if lookup_key:
            try:
                state = await gw.get_payment_state(str(lookup_key))
                settlement = PaymentSettlementService(session, settings)
                link = await settlement.get_locked_link_by_any_identifier(
                    gw.name,
                    str(state.invoice_id) if state.invoice_id else str(lookup_key),
                    reference_id=str(state.reference_id or lookup_key),
                    payment_id=str(state.payment_id or lookup_key),
                )
                if link is not None:
                    outcome = await settlement.apply(link, state, source=f"webhook_post_{gw.name}")
                    outcome_str = getattr(outcome, "value", outcome)
                    await session.commit()
                    await settlement.publish_pending_events()
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content={"received": True, "outcome": outcome_str, "link_id": str(link.id)},
                    )
            except Exception as exc:
                logger.error("gateway_webhook_post_empty_body_error", gateway=gateway, error=str(exc))

    service = PaymentWebhookService(session, settings, gw)
    result = await service.process(request.headers, raw_body)
    # Commit first so the SQS consumer (ushnotice) never sees stale data.
    await session.commit()
    await service.publish_pending_events()
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)


async def _extract_callback_data(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    body_data: Any = None
    if raw_body:
        try:
            body_data = await request.json()
        except Exception:
            try:
                form = await request.form()
                body_data = dict(form)
            except Exception:
                body_data = raw_body.decode("utf-8", errors="replace")

    return {
        "method": request.method,
        "url": str(request.url),
        "query_params": dict(request.query_params),
        "body": body_data,
        "headers": dict(request.headers),
    }


# ── Public success/failed redirect & callback endpoints ─────────────────────
@router.api_route(
    "/success",
    methods=["GET", "POST"],
    summary="Payment link success callback (settles payment & updates booking)",
    include_in_schema=True,
)
@router.api_route(
    "/success/",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def payment_link_success(
    request: Request,
    session: DBSession,
    settings: AppSettings,
) -> Response:
    data = await _extract_callback_data(request)
    json_to_print = data["body"] if data["body"] is not None else data["query_params"]
    print("=== [PAYMENT-LINKS SUCCESS] RECEIVED DATA ===", flush=True)
    print(json.dumps(json_to_print, indent=2, default=str), flush=True)
    logger.info("payment_link_success_callback_received", data=data)

    qp = data.get("query_params") or {}
    bd = data.get("body") if isinstance(data.get("body"), dict) else {}
    lookup_key = (
        qp.get("paymentId")
        or qp.get("PaymentId")
        or qp.get("Id")
        or qp.get("id")
        or qp.get("InvoiceId")
        or bd.get("paymentId")
        or bd.get("PaymentId")
        or bd.get("Id")
        or bd.get("InvoiceId")
        or qp.get("link_id")
        or bd.get("link_id")
    )

    settlement_outcome = None
    target_booking_id = None
    link_status = None
    state = None
    link = None

    if lookup_key:
        try:
            gw = _gateway(None, settings)
            state = await gw.get_payment_state(str(lookup_key))
            logger.info("payment_link_success_state_verified", lookup_key=lookup_key, status=state.status.value)
            settlement = PaymentSettlementService(session, settings)
            link = await settlement.get_locked_link_by_any_identifier(
                gw.name,
                str(state.invoice_id) if state.invoice_id else str(lookup_key),
                reference_id=str(state.reference_id or lookup_key),
                payment_id=str(state.payment_id or lookup_key),
            )
            if link is not None:
                res = await settlement.apply(link, state, source="callback")
                settlement_outcome = getattr(res, "value", res)
                await session.commit()
                await settlement.publish_pending_events()
                target_booking_id = str(link.target_id) if link.target_type == "booking" else None
                link_status = link.status
                logger.info(
                    "payment_link_success_settled",
                    link_id=str(link.id),
                    outcome=settlement_outcome,
                    target_id=str(link.target_id),
                )
            else:
                logger.warning("payment_link_success_link_not_found", lookup_key=lookup_key)
        except Exception as exc:
            logger.error("payment_link_success_settlement_failed", lookup_key=lookup_key, error=str(exc))

    target_redirect_base = getattr(settings, "MYFATOORAH_SUCCESS_URL", "")
    if target_redirect_base and request.method == "GET":
        redirect_params: dict[str, Any] = {
            "paymentId": str(lookup_key) if lookup_key else "",
            "invoiceId": str(getattr(state, "invoice_id", None) or "") if 'state' in locals() and state else "",
            "booking_id": target_booking_id,
            "link_id": str(link.id) if 'link' in locals() and link else None,
            "outcome": settlement_outcome,
        }
        if 'state' in locals() and state and getattr(state, "amount", None) is not None:
            redirect_params["amount"] = str(state.amount)
        if 'state' in locals() and state and getattr(state, "currency", None):
            redirect_params["currency"] = str(state.currency)
        redirect_url = _build_redirect_url(target_redirect_base, redirect_params)
        logger.info("payment_link_success_redirecting", url=redirect_url)
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "status": "success",
            "message": "Payment link success received and processed",
            "data": {
                **data,
                "settlement_outcome": settlement_outcome,
                "booking_id": target_booking_id,
                "link_status": link_status,
            },
        },
    )


@router.api_route(
    "/failed",
    methods=["GET", "POST"],
    summary="Payment link failed callback (records failure outcome)",
    include_in_schema=True,
)
@router.api_route(
    "/failed/",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def payment_link_failed(
    request: Request,
    session: DBSession,
    settings: AppSettings,
) -> Response:
    data = await _extract_callback_data(request)
    json_to_print = data["body"] if data["body"] is not None else data["query_params"]
    print("=== [PAYMENT-LINKS FAILED] RECEIVED DATA ===", flush=True)
    print(json.dumps(json_to_print, indent=2, default=str), flush=True)
    logger.info("payment_link_failed_callback_received", data=data)

    qp = data.get("query_params") or {}
    bd = data.get("body") if isinstance(data.get("body"), dict) else {}
    lookup_key = (
        qp.get("paymentId")
        or qp.get("PaymentId")
        or qp.get("Id")
        or qp.get("id")
        or qp.get("InvoiceId")
        or bd.get("paymentId")
        or bd.get("PaymentId")
        or bd.get("Id")
        or bd.get("InvoiceId")
        or qp.get("link_id")
        or bd.get("link_id")
    )

    state = None
    link = None
    if lookup_key:
        try:
            gw = _gateway(None, settings)
            state = await gw.get_payment_state(str(lookup_key))
            settlement = PaymentSettlementService(session, settings)
            link = await settlement.get_locked_link_by_any_identifier(
                gw.name,
                str(state.invoice_id) if state.invoice_id else str(lookup_key),
                reference_id=str(state.reference_id or lookup_key),
                payment_id=str(state.payment_id or lookup_key),
            )
            if link is not None:
                await settlement.apply(link, state, source="callback")
                await session.commit()
                await settlement.publish_pending_events()
        except Exception as exc:
            logger.warning("payment_link_failed_handling_warn", lookup_key=lookup_key, error=str(exc))

    target_redirect_base = getattr(settings, "MYFATOORAH_ERROR_URL", "")
    if target_redirect_base and request.method == "GET":
        redirect_params: dict[str, Any] = {
            "paymentId": str(lookup_key) if lookup_key else "",
            "invoiceId": str(getattr(state, "invoice_id", None) or "") if state else "",
            "booking_id": str(link.target_id) if link and link.target_type == "booking" else None,
            "link_id": str(link.id) if link else None,
            "error": str(getattr(state, "failure_reason", None) or "Payment failed") if state else "Payment failed",
        }
        redirect_url = _build_redirect_url(target_redirect_base, redirect_params)
        logger.info("payment_link_failed_redirecting", url=redirect_url)
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "status": "failed",
            "message": "Payment link failed received",
            "data": data,
        },
    )


# ── Employee endpoints ───────────────────────────────────────────────────────
@router.post("/", summary="Create & share a payment link", status_code=status.HTTP_201_CREATED)
@router.post("", include_in_schema=False, status_code=status.HTTP_201_CREATED)
async def create_payment_link(
    body: CreatePaymentLinkRequest,
    session: DBSession,
    settings: AppSettings,
    user: Annotated[TokenPayload, Depends(require_permission("payment_links.create"))],
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    service = PaymentLinkService(session, settings, _gateway(body.gateway, settings))
    link = await service.create_link(
        target_type=body.target_type,
        target_id=body.target_id,
        amount=body.amount,
        actor_id=actor_id,
        actor_data=actor_data,
        invoice_id=body.invoice_id,
        invoice_number=body.invoice_number,
        force_new=body.force_new,
        expiry_minutes=body.expiry_minutes,
        request_data=body.model_dump(mode="json"),
    )
    return _ok(PaymentLinkOut.from_link(link), status.HTTP_201_CREATED)


@router.get("/", summary="List payment links for a booking / voucher")
@router.get("", include_in_schema=False)
async def list_payment_links(
    session: DBSession,
    settings: AppSettings,
    _: Annotated[TokenPayload, Depends(require_permission("payment_links.list"))],
    target_type: Annotated[Literal["booking", "voucher", "gift_voucher", "product"], Query()],
    target_id: Annotated[uuid.UUID, Query()],
) -> JSONResponse:
    service = PaymentLinkService(session, settings, _gateway(None, settings))
    links = await service.list_for_target(target_type, target_id)
    return _ok([PaymentLinkOut.from_link(link) for link in links])


@router.get("/{link_id}/", summary="Get a payment link / status")
@router.get("/{link_id}", include_in_schema=False)
async def get_payment_link(
    link_id: uuid.UUID,
    session: DBSession,
    settings: AppSettings,
    _: Annotated[TokenPayload, Depends(require_permission("payment_links.view"))],
) -> JSONResponse:
    service = PaymentLinkService(session, settings, _gateway(None, settings))
    return _ok(PaymentLinkOut.from_link(await service.get_link(link_id)))


@router.post("/{link_id}/renew/", summary="Renew an expired unpaid payment link (creates a new link)")
@router.post("/{link_id}/renew", include_in_schema=False)
async def renew_payment_link(
    link_id: uuid.UUID,
    session: DBSession,
    settings: AppSettings,
    user: Annotated[TokenPayload, Depends(require_permission("payment_links.create"))],
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    probe = await PaymentLinkService(session, settings, _gateway(None, settings)).get_link(link_id)
    service = PaymentLinkService(session, settings, _gateway(probe.gateway, settings))
    link = await service.renew_link(link_id, actor_id=actor_id, actor_data=actor_data)
    return _ok(PaymentLinkOut.from_link(link), status.HTTP_201_CREATED)


@router.post("/{link_id}/sync/", summary="Check status with the gateway and settle if paid")
@router.post("/{link_id}/sync", include_in_schema=False)
async def sync_payment_link(
    link_id: uuid.UUID,
    session: DBSession,
    settings: AppSettings,
    _: Annotated[TokenPayload, Depends(require_permission("payment_links.view"))],
) -> JSONResponse:
    probe = await PaymentLinkService(session, settings, _gateway(None, settings)).get_link(link_id)
    service = PaymentLinkService(session, settings, _gateway(probe.gateway, settings))
    link = await service.sync_status(link_id)
    # Commit first so the SQS consumer (ushnotice) never sees stale data.
    await session.commit()
    await service.publish_pending_events()
    return _ok(PaymentLinkOut.from_link(link))


@router.post("/{link_id}/cancel/", summary="Cancel an unpaid payment link")
@router.post("/{link_id}/cancel", include_in_schema=False)
async def cancel_payment_link(
    link_id: uuid.UUID,
    session: DBSession,
    settings: AppSettings,
    _: Annotated[TokenPayload, Depends(require_permission("payment_links.cancel"))],
) -> JSONResponse:
    service = PaymentLinkService(session, settings, _gateway(None, settings))
    return _ok(PaymentLinkOut.from_link(await service.cancel_link(link_id)))


@router.delete("/{link_id}/", summary="Delete a payment link (cancels with gateway if open and deletes record)")
@router.delete("/{link_id}", include_in_schema=False)
async def delete_payment_link(
    link_id: uuid.UUID,
    session: DBSession,
    settings: AppSettings,
    _: Annotated[TokenPayload, Depends(require_permission("payment_links.cancel"))],
) -> JSONResponse:
    probe = await PaymentLinkService(session, settings, _gateway(None, settings)).get_link(link_id)
    service = PaymentLinkService(session, settings, _gateway(probe.gateway, settings))
    await service.delete_link(link_id)
    await session.commit()
    return _ok({"success": True, "deleted_id": str(link_id)})


class CancelPaymentLinksByTargetRequest(BaseModel):
    target_type: Literal["booking", "voucher", "gift_voucher", "product"] = "booking"
    target_id: uuid.UUID


@router.post("/cancel-by-target/", summary="Cancel payment links for a target with gateway and delete/cancel records")
@router.post("/cancel-by-target", include_in_schema=False)
async def cancel_payment_links_by_target(
    body: CancelPaymentLinksByTargetRequest,
    session: DBSession,
    settings: AppSettings,
    request: Request,
) -> JSONResponse:
    from app.core.security import require_app_token
    await require_app_token(request, settings)
    service = PaymentLinkService(session, settings, _gateway(None, settings))
    count = await service.cancel_and_delete_links_for_target(body.target_type, body.target_id)
    await session.commit()
    return _ok({"success": True, "cancelled_count": count})


@router.delete("/", summary="Delete payment links for a target (cancels with gateway and deletes records)")
async def delete_payment_links_for_target(
    target_type: Annotated[Literal["booking", "voucher", "gift_voucher", "product"], Query()],
    target_id: Annotated[uuid.UUID, Query()],
    session: DBSession,
    settings: AppSettings,
    request: Request,
) -> JSONResponse:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        from app.core.security import require_authenticated_user
        user = await require_authenticated_user(request, settings=settings)
        if not user.has_permission("payment_links.cancel"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": "Permission 'payment_links.cancel' is required."},
            )
    else:
        from app.core.security import require_app_token
        await require_app_token(request, settings)
    service = PaymentLinkService(session, settings, _gateway(None, settings))
    count = await service.cancel_and_delete_links_for_target(target_type, target_id)
    await session.commit()
    return _ok({"success": True, "deleted_count": count})


@router.post("/{link_id}/refund/", summary="Refund a paid payment link to the customer")
@router.post("/{link_id}/refund", include_in_schema=False)
async def refund_payment_link(
    link_id: uuid.UUID,
    body: RefundPaymentRequest,
    session: DBSession,
    settings: AppSettings,
    user: Annotated[TokenPayload, Depends(require_permission("payment_links.refund"))],
) -> JSONResponse:
    actor_id, actor_data = _actor(user)
    service = PaymentLinkRefundService(session, settings, _gateway(None, settings), get_http_client())
    refund = await service.refund(
        link_id, amount=body.amount, reason=body.reason, actor_id=actor_id, actor_data=actor_data
    )
    return _ok(
        RefundOut(
            refund_id=refund.id,
            refund_number=refund.refund_number,
            status=refund.status,
            refunded_amount=refund.refunded_amount,
            currency=refund.currency,
            gateway_refund_id=refund.gateway_refund_id,
        )
    )
