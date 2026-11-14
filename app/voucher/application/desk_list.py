"""
Desk (ushdesk) voucher list queries.

The desk "Gift Vouchers" page used to download every voucher with all of its JSON blobs and filter /
paginate / aggregate in the browser. This module serves it from the database instead:

* ``fetch_page``     – one page of *slim* rows (only the columns / JSON keys the list renders)
* ``fetch_stats``    – aggregate cards (total / active / redeemed / expired / revenue …)
* ``fetch_creators`` – distinct creators for the "Created by" filter

All of them share :func:`build_conditions`, which mirrors the filter rules the page applied client-side.
Full voucher details are fetched on demand through the regular detail mapping.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, func, literal_column, not_, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.utils import LocalDateTime
from app.voucher.infrastructure.models import GiftVoucher as G
from app.voucher.infrastructure.repository import _parse_filter_date

_STATUS_KEYS = {"scheduled", "pending", "redeemed", "partially_redeemed", "active", "expired", "cancelled"}


@dataclass
class DeskFilters:
    statuses: list[str] = field(default_factory=list)
    channel: str | None = None
    created_by: str | None = None
    q: str | None = None
    created_from: str | None = None
    created_to: str | None = None
    redeemed_from: str | None = None
    redeemed_to: str | None = None
    activated_from: str | None = None
    activated_to: str | None = None
    # Sales report window: created in range OR activated in range OR redeemed in range
    period_from: str | None = None
    period_to: str | None = None


# ── SQL expressions (same semantics as the page's client-side filters) ───────

_STATUS = func.lower(func.coalesce(G.status, ""))
_IS_PENDING = or_(_STATUS.in_(["pending", "payment_pending", "created"]), _STATUS.like("%pending%"))
_IS_REDEEMED = and_(
    _STATUS != "partially_redeemed",
    or_(_STATUS == "redeemed", G.redeemed_at.is_not(None)),
)
_CHANNEL = func.lower(func.coalesce(G.payment_through, G.payment_data["payment_through"].astext, ""))


def _status_clause(key: str):
    key = key.strip().lower()
    if key == "pending":
        return _IS_PENDING
    if key == "redeemed":
        return _IS_REDEEMED
    if key == "active":
        return and_(_STATUS == "active", not_(_IS_REDEEMED))
    return _STATUS == key  # scheduled / partially_redeemed / expired / cancelled / anything else


def _contains(expr, needle: str):
    esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return expr.ilike(f"%{esc}%", escape="\\")


def _digits(expr):
    return func.regexp_replace(func.coalesce(expr, ""), r"\D", "", "g")


def _compact(expr):
    return func.replace(func.replace(func.replace(func.lower(func.coalesce(expr, "")), "-", ""), " ", ""), "_", "")


def _search_clause(q: str):
    q = q.strip().lower()
    code = re.sub(r"[\s\-_]", "", q)
    digits = re.sub(r"\D", "", q)
    parts = [
        _contains(func.coalesce(G.secret_code, ""), q),
        _contains(func.coalesce(G.voucher_number, ""), q),
        _contains(cast(G.id, String), q),
        _contains(func.coalesce(G.payment_through, ""), q),
        _contains(func.coalesce(G.recipient_phone, ""), q),
        _contains(func.coalesce(G.sender_data["name"].astext, ""), q),
        _contains(func.coalesce(G.sender_data["phone_number"].astext, ""), q),
        _contains(func.coalesce(G.recipient_data["name"].astext, ""), q),
        _contains(func.coalesce(G.recipient_data["email"].astext, ""), q),
        _contains(func.coalesce(G.recipient_data["phone_number"].astext, ""), q),
        _contains(func.coalesce(G.service_data["name"].astext, ""), q),
        _contains(func.coalesce(G.branch_data["name"].astext, ""), q),
    ]
    if code:
        parts.append(_compact(G.secret_code).like(f"%{code}%"))
        parts.append(_compact(G.voucher_number).like(f"%{code}%"))
    if len(digits) >= 3:
        for phone in (G.recipient_phone, G.recipient_data["phone_number"].astext, G.sender_data["phone_number"].astext):
            parts.append(_digits(phone).like(f"%{digits}%"))
    return or_(*parts)


def _redeemed_range_clause(frm: str | None, to: str | None):
    start = _parse_filter_date(frm, end_of_day=False) if frm else None
    end = _parse_filter_date(to, end_of_day=True) if to else None
    if not start and not end:
        return None

    def rng(col):
        cs = []
        if start:
            cs.append(col >= start)
        if end:
            cs.append(col <= end)
        return and_(*cs)

    def json_range(expr_sql: str, prefix: str):
        bounds, params = [], {}
        if start:
            bounds.append(f"({expr_sql})::timestamptz >= :{prefix}_s")
            params[f"{prefix}_s"] = start
        if end:
            bounds.append(f"({expr_sql})::timestamptz <= :{prefix}_e")
            params[f"{prefix}_e"] = end
        return " AND ".join(bounds), params

    r_bounds, r_params = json_range("r.e->>'redeemed_at'", "rd")
    in_redemptions = text(
        "EXISTS (SELECT 1 FROM jsonb_array_elements("
        "CASE WHEN jsonb_typeof(gift_vouchers.redemptions) = 'array' THEN gift_vouchers.redemptions ELSE '[]'::jsonb END"
        ") AS r(e) WHERE r.e->>'redeemed_at' ~ '^\\d{4}-\\d{2}-\\d{2}' AND " + r_bounds + ")"
    ).bindparams(**r_params)

    b_bounds, b_params = json_range("gift_vouchers.booking_data->>'created_at'", "bk")
    from_booking = and_(
        _STATUS == "redeemed",
        G.redeemed_at.is_(None),
        text("gift_vouchers.booking_data->>'created_at' ~ '^\\d{4}-\\d{2}-\\d{2}' AND " + b_bounds).bindparams(**b_params),
    )
    return or_(rng(G.redeemed_at), in_redemptions, from_booking)


def build_conditions(f: DeskFilters) -> list[Any]:
    conds: list[Any] = []
    statuses = [s.strip().lower() for s in f.statuses if s and s.strip()]
    # The page treats "6+ of the 7 statuses selected" as "no status filter"; the caller applies that rule.
    if statuses:
        conds.append(or_(*[_status_clause(s) for s in statuses]))

    ch = (f.channel or "").strip().lower()
    if ch and ch != "all":
        if ch == "ushweb":
            conds.append(_CHANNEL.in_(["ushweb", "web"]))
        elif ch == "desk":
            conds.append(_CHANNEL.in_(["desk", "ushdesk"]))
        elif ch == "ushspa":
            conds.append(_CHANNEL.in_(["ushspa", "spa", "app"]))
        else:
            conds.append(_CHANNEL == ch)

    cb = (f.created_by or "").strip()
    if cb and cb != "all":
        if cb == "__online__":
            conds.append(G.created_by.is_(None))
        else:
            try:
                conds.append(G.created_by == uuid.UUID(cb))
            except ValueError:
                conds.append(text("false"))

    if f.q and f.q.strip():
        conds.append(_search_clause(f.q))

    if f.created_from:
        start = _parse_filter_date(f.created_from, end_of_day=False)
        if start:
            conds.append(G.created_at >= start)
    if f.created_to:
        end = _parse_filter_date(f.created_to, end_of_day=True)
        if end:
            conds.append(G.created_at <= end)

    red = _redeemed_range_clause(f.redeemed_from, f.redeemed_to)
    if red is not None:
        conds.append(red)

    if f.activated_from:
        start = _parse_filter_date(f.activated_from, end_of_day=False)
        if start:
            conds.append(G.activation_datetime >= start)
    if f.activated_to:
        end = _parse_filter_date(f.activated_to, end_of_day=True)
        if end:
            conds.append(G.activation_datetime <= end)

    if f.period_from or f.period_to:
        p_start = _parse_filter_date(f.period_from, end_of_day=False) if f.period_from else None
        p_end = _parse_filter_date(f.period_to, end_of_day=True) if f.period_to else None
        created_in = and_(*([G.created_at >= p_start] if p_start else []), *([G.created_at <= p_end] if p_end else []))
        activated_in = and_(*([G.activation_datetime >= p_start] if p_start else []), *([G.activation_datetime <= p_end] if p_end else []))
        redeemed_in = _redeemed_range_clause(f.period_from, f.period_to)
        period_parts = [created_in, activated_in]
        if redeemed_in is not None:
            period_parts.append(redeemed_in)
        conds.append(or_(*period_parts))
    return conds


# ── Slim list rows ───────────────────────────────────────────────────────────


class DeskVoucherItem(BaseModel):
    """Only what the list page renders (same field names / shapes as the full voucher response)."""

    id: uuid.UUID
    voucher_number: str | None = None
    secret_code: str | None = None
    public_token: str | None = None
    status: str
    total_amount: str
    discount: str = "0.000"
    currency: str = "KWD"
    total_duration: int = 0
    extra_time: int = 0
    gift_category: str | None = None
    delivery_status: str | None = None
    delivery_mode: str | None = None
    expire_date: LocalDateTime | None = None
    created_at: LocalDateTime
    updated_at: LocalDateTime | None = None
    redeemed_at: LocalDateTime | None = None
    scheduled_delivery_at: LocalDateTime | None = None
    payment_through: str | None = None
    payment_url: str | None = None
    payment_provider: str | None = None
    created_by: uuid.UUID | None = None
    created_by_name: str | None = None
    created_by_role: str | None = None
    created_by_user_data: dict[str, Any] | None = None
    referenced_by: uuid.UUID | None = None
    referenced_by_name: str | None = None
    referral_number: str | None = None
    activation_datetime: LocalDateTime | None = None
    recipient_phone: str | None = None
    gift_message: str | None = None
    gift_from: str | None = None
    service_data: dict[str, Any] = Field(default_factory=dict)
    services: list[dict[str, Any]] = Field(default_factory=list)
    addons: list[dict[str, Any]] = Field(default_factory=list)
    sender_data: dict[str, Any] = Field(default_factory=dict)
    recipient_data: dict[str, Any] = Field(default_factory=dict)
    branch_data: dict[str, Any] = Field(default_factory=dict)


class DeskVoucherReportItem(DeskVoucherItem):
    """List row + the few extra fields the export / sales reports need."""

    branch_id: uuid.UUID | None = None
    service_arrangement_data: dict[str, Any] = Field(default_factory=dict)
    redeemed_by: uuid.UUID | None = None
    invoice_number: str | None = None
    payment_number: str | None = None
    payment_data: dict[str, Any] | None = None


def _jarr(col, path: str):
    return func.jsonb_path_query_array(col, literal_column(f"'{path}'::jsonpath"), type_=JSONB)


def _slim_columns(view: str) -> list[Any]:
    cols: list[Any] = [
        G.id, G.voucher_number, G.secret_code, G.public_token, G.status, G.total_amount, G.discount,
        G.currency, G.total_duration, G.extra_time, G.gift_category, G.delivery_status, G.delivery_mode,
        G.expire_date, G.created_at, G.updated_at, G.redeemed_at, G.scheduled_delivery_at,
        G.payment_through, G.payment_url, G.payment_provider, G.created_by, G.created_by_name,
        G.created_by_role, G.referenced_by, G.referenced_by_name, G.referral_number, G.activation_datetime,
        G.recipient_phone, G.gift_message, G.gift_from,
        G.created_by_user_data["name"].astext.label("cbu_name"),
        G.created_by_user_data["role"].astext.label("cbu_role"),
        G.service_data["name"].astext.label("sd_name"),
        G.service_data["duration_minutes"].astext.label("sd_duration"),
        G.service_data["service_types"][0]["name"].astext.label("sd_type"),
        G.sender_data["name"].astext.label("snd_name"),
        G.sender_data["phone_number"].astext.label("snd_phone"),
        G.recipient_data["name"].astext.label("rcp_name"),
        G.recipient_data["phone_number"].astext.label("rcp_phone"),
        G.branch_data["name"].astext.label("br_name"),
        _jarr(G.services, "$[*].service_data.name").label("svc_names"),
        _jarr(G.addons, "$[*].duration_minutes").label("addon_durations"),
    ]
    if view == "report":
        cols += [
            G.branch_id, G.redeemed_by,
            G.service_arrangement_data["arrangement_name"].astext.label("arr_name"),
            _jarr(G.addons, "$[*].name").label("addon_names"),
            G.payment_data["invoice_number"].astext.label("pd_invoice"),
            G.payment_data["payment_number"].astext.label("pd_payment"),
            G.payment_data["data"]["InvoiceTransactions"][0]["PaymentGateway"].astext.label("pd_gateway"),
        ]
    return cols


def _num(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _row_to_item(r: Any, view: str) -> DeskVoucherItem:
    m = r._mapping
    svc_names = [n for n in (m["svc_names"] or []) if isinstance(n, str) and n]
    addon_durations = [d for d in (m["addon_durations"] or [])]
    service_data: dict[str, Any] = {"name": m["sd_name"]}
    if m["sd_duration"] is not None:
        service_data["duration_minutes"] = _num(m["sd_duration"])
    if m["sd_type"]:
        service_data["service_types"] = [{"name": m["sd_type"]}]
    data: dict[str, Any] = {
        "id": m["id"], "voucher_number": m["voucher_number"], "secret_code": m["secret_code"],
        "public_token": m["public_token"], "status": m["status"], "total_amount": str(m["total_amount"]),
        "discount": str(m["discount"] if m["discount"] is not None else "0.000"),
        "currency": m["currency"], "total_duration": m["total_duration"] or 0,
        "extra_time": m["extra_time"] or 0, "gift_category": m["gift_category"],
        "delivery_status": m["delivery_status"], "delivery_mode": m["delivery_mode"],
        "expire_date": m["expire_date"], "created_at": m["created_at"], "updated_at": m["updated_at"],
        "redeemed_at": m["redeemed_at"], "scheduled_delivery_at": m["scheduled_delivery_at"],
        "payment_through": m["payment_through"], "payment_url": m["payment_url"],
        "payment_provider": m["payment_provider"], "created_by": m["created_by"],
        "created_by_name": m["created_by_name"], "created_by_role": m["created_by_role"],
        "created_by_user_data": (
            {k: v for k, v in (("name", m["cbu_name"]), ("role", m["cbu_role"])) if v} or None
        ),
        "referenced_by": m["referenced_by"], "referenced_by_name": m["referenced_by_name"],
        "referral_number": m["referral_number"], "activation_datetime": m["activation_datetime"],
        "recipient_phone": m["recipient_phone"], "gift_message": m["gift_message"], "gift_from": m["gift_from"],
        "service_data": service_data,
        # names only: the list joins them for multi-service vouchers
        "services": [{"service_data": {"name": n}} for n in svc_names],
        "addons": [{"duration_minutes": _num(d)} for d in addon_durations],
        "sender_data": {k: v for k, v in (("name", m["snd_name"]), ("phone_number", m["snd_phone"])) if v},
        "recipient_data": {k: v for k, v in (("name", m["rcp_name"]), ("phone_number", m["rcp_phone"])) if v},
        "branch_data": {"name": m["br_name"]} if m["br_name"] else {},
    }
    if view == "report":
        data.update(
            branch_id=m["branch_id"],
            service_arrangement_data={"arrangement_name": m["arr_name"]} if m["arr_name"] else {},
            redeemed_by=m["redeemed_by"],
            invoice_number=m["pd_invoice"],
            payment_number=m["pd_payment"],
            payment_data={"data": {"InvoiceTransactions": [{"PaymentGateway": m["pd_gateway"]}]}} if m["pd_gateway"] else None,
        )
        # the report lists add-on names
        names = list(m["addon_names"] or [])
        data["addons"] = [
            {
                "name": names[i] if i < len(names) else None,
                "duration_minutes": _num(addon_durations[i]) if i < len(addon_durations) else 0,
            }
            for i in range(max(len(names), len(addon_durations)))
        ]
    return DeskVoucherReportItem(**data) if view == "report" else DeskVoucherItem(**data)


async def fetch_page(
    session: AsyncSession, filters: DeskFilters, *, page: int, page_size: int, view: str = "list"
) -> tuple[list[DeskVoucherItem], int]:
    conds = build_conditions(filters)
    total = (await session.execute(select(func.count()).select_from(G).where(*conds))).scalar_one()
    stmt = (
        select(*_slim_columns(view))
        .where(*conds)
        .order_by(G.created_at.desc(), G.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).all()
    items = [_row_to_item(r, view) for r in rows]

    if view == "report":
        # Same fallback the full response uses: invoice / payment numbers from the Payment table
        missing = [it.id for it in items if not it.invoice_number or not it.payment_number]
        if missing:
            try:
                from app.payment.infrastructure.models import Payment

                res = await session.execute(
                    select(Payment.voucher_id, Payment.invoice_number, Payment.payment_number)
                    .where(Payment.voucher_id.in_(missing))
                    .order_by(Payment.created_at.desc())
                )
                seen: dict[Any, tuple[Any, Any]] = {}
                for vid, inv, pmt in res.all():
                    seen.setdefault(vid, (inv, pmt))
                for it in items:
                    inv, pmt = seen.get(it.id, (None, None))
                    it.invoice_number = it.invoice_number or inv
                    it.payment_number = it.payment_number or pmt
            except Exception:  # pragma: no cover - best effort
                pass
    return items, total


async def fetch_stats(session: AsyncSession, filters: DeskFilters) -> dict[str, Any]:
    conds = build_conditions(filters)
    amount = G.total_amount
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(_STATUS == "active"),
                func.count().filter(_IS_REDEEMED),
                func.count().filter(_STATUS == "expired"),
                func.coalesce(func.sum(amount).filter(_STATUS.in_(["active", "redeemed"])), 0),
                func.coalesce(func.sum(amount).filter(_IS_PENDING), 0),
                func.max(G.currency),
                func.coalesce(func.sum(amount), 0),
            )
            .select_from(G)
            .where(*conds)
        )
    ).one()
    return {
        "total": int(row[0]),
        "active": int(row[1]),
        "redeemed": int(row[2]),
        "expired": int(row[3]),
        "revenue": float(row[4] or 0),
        "pending_revenue": float(row[5] or 0),
        "currency": row[6] or "KWD",
        "amount_total": float(row[7] or 0),
    }


async def fetch_creators(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                G.created_by,
                func.max(G.created_by_name),
                func.max(G.created_by_user_data["name"].astext),
                func.max(G.created_by_role),
            )
            .where(G.created_by.is_not(None))
            .group_by(G.created_by)
        )
    ).all()
    return [
        {"id": str(r[0]), "name": (r[1] or r[2] or None), "role": r[3]}
        for r in rows
    ]
