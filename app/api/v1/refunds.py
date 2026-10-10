"""
app/api/v1/refunds.py
─────────────────────
API routes for booking refunds:
- Manual refunds recorded by USH staff / branch agents (ushdesk)
- Automated refunds executed through payment gateways (Tap / MyFatoorah)
- Listing and inspecting refund records
"""

from __future__ import annotations

from app.common.utils import to_local_tz as _to_local_tz
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, DBSession, RefundServiceDep, RequireAppToken
from app.common.pagination import make_paginated_response
from app.core.exceptions import NotFoundError, ValidationError
from app.payment.interfaces.schemas import (
    CreateManualRefundRequest,
    ProcessGatewayRefundRequest,
    RefundListResponse,
    RefundResponse,
)

router = APIRouter(tags=["Refunds"])


def _refund_to_dict(refund: Any) -> dict[str, Any]:
    return {
        "id": str(refund.id),
        "refund_number": refund.refund_number,
        "booking_id": str(refund.booking_id) if refund.booking_id else None,
        "booking_data": refund.booking_data,
        "payment_id": str(refund.payment_id) if refund.payment_id else None,
        "invoice_number": refund.invoice_number,
        "credit_note_number": refund.credit_note_number,
        "customer_id": str(refund.customer_id) if refund.customer_id else None,
        "customer_data": refund.customer_data,
        "branch_id": str(refund.branch_id) if refund.branch_id else None,
        "branch_data": refund.branch_data,
        "refund_type": refund.refund_type,
        "refund_method": refund.refund_method,
        "status": refund.status,
        "requested_amount": float(refund.requested_amount),
        "cancellation_fee": float(refund.cancellation_fee),
        "refunded_amount": float(refund.refunded_amount),
        "currency": refund.currency,
        "payment_gateway": refund.payment_gateway,
        "gateway_refund_id": refund.gateway_refund_id,
        "gateway_transaction_id": refund.gateway_transaction_id,
        "reason": refund.reason,
        "notes": refund.notes,
        "customer_confirmation": refund.customer_confirmation,
        "reference_number": refund.reference_number,
        "processed_by": refund.processed_by,
        "processed_by_data": refund.processed_by_data,
        "processed_at": _to_local_tz(refund.processed_at).isoformat() if refund.processed_at else None,
        "created_at": _to_local_tz(refund.created_at).isoformat() if refund.created_at else None,
        "updated_at": _to_local_tz(refund.updated_at).isoformat() if getattr(refund, "updated_at", None) else None,
    }


@router.post(
    "/bookings/{booking_id}/refunds/manual/",
    summary="Record manual refund (ushdesk / branch)",
    description=(
        "Used by USH staff or customer agents in ushdesk when a refund is processed "
        "outside the automated gateway (e.g. Cash, Card at counter, or Bank Transfer). "
        "Generates a sequential internal refund number (REF/YYYY/MM/{NNNNNN}) and "
        "records the transaction without requiring an external gateway transaction ID."
    ),
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def record_manual_refund(
    booking_id: uuid.UUID = Path(..., description="Booking UUID"),
    body: CreateManualRefundRequest = ...,
    current_user: CurrentUser = ...,
    refund_service: RefundServiceDep = ...,
    session: DBSession = ...,
) -> JSONResponse:
    user_id = str(body.change_by_user or current_user.sub)
    user_data = body.change_by_user_data or {
        "id": current_user.sub,
        "first_name": current_user.first_name or "",
        "last_name": current_user.last_name or "",
        "phone_number": current_user.phone_number or "",
        "email": current_user.email or "",
        "role": getattr(current_user, "role", None),
        "user_type": getattr(current_user, "user_type", None),
    }

    try:
        refund = await refund_service.record_manual_refund(
            booking_id=booking_id,
            req=body,
            current_user_id=user_id,
            current_user_data=user_data,
        )
        await session.commit()
    except NotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to record manual refund: {exc}")

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"success": True, "data": _refund_to_dict(refund)},
    )


@router.post(
    "/bookings/{booking_id}/refunds/gateway/",
    summary="Process automated payment gateway refund",
    description=(
        "Executes a refund via the payment gateway integration (Tap / MyFatoorah). "
        "Marks the refund as completed only after confirmation from the gateway."
    ),
    response_model=None,
    status_code=status.HTTP_200_OK,
)
async def process_gateway_refund(
    booking_id: uuid.UUID = Path(..., description="Booking UUID"),
    body: ProcessGatewayRefundRequest = ...,
    current_user: CurrentUser = ...,
    refund_service: RefundServiceDep = ...,
    session: DBSession = ...,
) -> JSONResponse:
    user_id = str(body.change_by_user or current_user.sub)
    user_data = body.change_by_user_data or {
        "id": current_user.sub,
        "first_name": current_user.first_name or "",
        "last_name": current_user.last_name or "",
        "phone_number": current_user.phone_number or "",
        "email": current_user.email or "",
        "role": getattr(current_user, "role", None),
        "user_type": getattr(current_user, "user_type", None),
    }

    try:
        refund = await refund_service.process_gateway_refund(
            booking_id=booking_id,
            req=body,
            current_user_id=user_id,
            current_user_data=user_data,
        )
        await session.commit()
    except NotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to process gateway refund: {exc}")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"success": True, "data": _refund_to_dict(refund)},
    )


@router.get(
    "/bookings/{booking_id}/refunds/",
    summary="List all refunds for a booking",
    response_model=None,
)
async def list_booking_refunds(
    booking_id: uuid.UUID = Path(..., description="Booking UUID"),
    current_user: CurrentUser = ...,
    refund_service: RefundServiceDep = ...,
) -> JSONResponse:
    refunds = await refund_service.get_booking_refunds(booking_id)
    return JSONResponse(
        content={"success": True, "data": [_refund_to_dict(r) for r in refunds]}
    )


@router.get(
    "/refunds/{refund_id}/",
    summary="Get refund details by ID",
    response_model=None,
)
async def get_refund_detail(
    refund_id: uuid.UUID = Path(..., description="Refund UUID"),
    current_user: CurrentUser = ...,
    refund_service: RefundServiceDep = ...,
) -> JSONResponse:
    try:
        refund = await refund_service.get_refund_by_id(refund_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return JSONResponse(
        content={"success": True, "data": _refund_to_dict(refund)}
    )


@router.get(
    "/refunds/",
    summary="List all refunds",
    description="List all refunds across all bookings and payments with pagination, multi-attribute filtering, and financial metrics.",
    response_model=RefundListResponse,
)
@router.get(
    "/refunds",
    summary="List all refunds",
    include_in_schema=False,
)
async def list_all_refunds(
    request: Request,
    current_user: CurrentUser,
    refund_service: RefundServiceDep,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    booking_id: uuid.UUID | None = Query(default=None, description="Filter by booking ID"),
    payment_id: uuid.UUID | None = Query(default=None, description="Filter by payment ID"),
    customer_id: uuid.UUID | None = Query(default=None, description="Filter by customer ID"),
    branch_id: uuid.UUID | None = Query(default=None, description="Filter by branch ID"),
    refund_type: str | None = Query(default=None, description="Filter by refund type (manual | gateway)"),
    refund_method: str | None = Query(default=None, description="Filter by refund method (cash | knet | card | bank_transfer | tap | myfatoorah)"),
    status: str | None = Query(default=None, description="Filter by status (completed | pending | failed)"),
    refund_number: str | None = Query(default=None, description="Filter by refund number"),
    invoice_number: str | None = Query(default=None, description="Filter by invoice number"),
    credit_note_number: str | None = Query(default=None, description="Filter by credit note number"),
    payment_gateway: str | None = Query(default=None, description="Filter by payment gateway"),
    gateway_refund_id: str | None = Query(default=None, description="Filter by gateway refund ID"),
    gateway_transaction_id: str | None = Query(default=None, description="Filter by gateway transaction ID"),
    reference_number: str | None = Query(default=None, description="Filter by reference number"),
    processed_by: str | None = Query(default=None, description="Filter by staff/agent user ID"),
    from_date: datetime | None = Query(default=None, alias="from_date", description="Filter from created date"),
    to_date: datetime | None = Query(default=None, alias="to_date", description="Filter to created date"),
    date_from: datetime | None = Query(default=None, alias="date_from", description="Alias for from_date"),
    date_to: datetime | None = Query(default=None, alias="date_to", description="Alias for to_date"),
    search: str | None = Query(default=None, description="Search across refund number, credit note, invoice, gateway IDs, reason, or notes"),
    currency: str | None = Query(default=None, description="Filter by currency (e.g. KWD)"),
) -> JSONResponse:
    from fastapi.params import Query as QueryParam

    def _val(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, QueryParam) else v

    p_page = _val(page, 1)
    p_page_size = _val(page_size, 20)
    p_booking_id = _val(booking_id)
    p_payment_id = _val(payment_id)
    p_customer_id = _val(customer_id)
    p_branch_id = _val(branch_id)
    p_refund_type = _val(refund_type)
    p_refund_method = _val(refund_method)
    p_status = _val(status)
    p_refund_number = _val(refund_number)
    p_invoice_number = _val(invoice_number)
    p_credit_note_number = _val(credit_note_number)
    p_payment_gateway = _val(payment_gateway)
    p_gateway_refund_id = _val(gateway_refund_id)
    p_gateway_transaction_id = _val(gateway_transaction_id)
    p_reference_number = _val(reference_number)
    p_processed_by = _val(processed_by)
    p_from_date = _val(from_date) or _val(date_from)
    p_to_date = _val(to_date) or _val(date_to)
    p_search = _val(search)
    p_currency = _val(currency)

    # If the requester is a regular customer, restrict visibility strictly to their own refunds
    if getattr(current_user, "user_type", None) == "customer":
        try:
            p_customer_id = uuid.UUID(current_user.sub)
        except (ValueError, TypeError):
            p_customer_id = current_user.sub

    refunds, total_count, analytics = await refund_service.list_refunds(
        page=p_page,
        page_size=p_page_size,
        booking_id=p_booking_id,
        payment_id=p_payment_id,
        customer_id=p_customer_id,
        branch_id=p_branch_id,
        refund_type=p_refund_type,
        refund_method=p_refund_method,
        status=p_status,
        refund_number=p_refund_number,
        invoice_number=p_invoice_number,
        credit_note_number=p_credit_note_number,
        payment_gateway=p_payment_gateway,
        gateway_refund_id=p_gateway_refund_id,
        gateway_transaction_id=p_gateway_transaction_id,
        reference_number=p_reference_number,
        processed_by=p_processed_by,
        from_date=p_from_date,
        to_date=p_to_date,
        search=p_search,
        currency=p_currency,
    )

    items = [_refund_to_dict(r) for r in refunds]
    paginated = make_paginated_response(
        items,
        count=total_count,
        page=p_page,
        page_size=p_page_size,
        base_url=str(request.url.remove_query_params(["page", "page_size"])),
    )
    out = paginated.model_dump(mode="json")
    out["analytics"] = analytics
    return JSONResponse(content=out)

