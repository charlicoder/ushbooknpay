"""
app/api/v1/customer_analytics.py
───────────────────────────────────
Unified Customer Analytics Endpoint for USH Spa.

Provides a single aggregated call returning:
  - Loyalty points, balance, lifetime earned/redeemed, expiry & transactions
  - Gift vouchers sent/received counts, spend, and lists
  - Total spent across bookings & gift vouchers (LTV)
  - Booking & appointment history, completion/cancellation rates
  - Marketing & CRM intelligence: customer tier/segment, favorite service,
    preferred branch, favorite therapist, peak visiting hours/days.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import DBSession, OptionalUser
from app.booking.infrastructure.models import Booking
from app.loyalty.infrastructure.models import LoyaltyAccount, LoyaltyTransaction
from app.payment.infrastructure.models import Payment
from app.voucher.infrastructure.models import GiftVoucher

router = APIRouter(prefix="/customers", tags=["Customer Analytics"])


# ── Pydantic Response Schemas ──────────────────────────────────────────────────

class LoyaltyTransactionSummary(BaseModel):
    id: str
    transaction_type: str
    points: int
    booking_id: str | None = None
    booking_number: str | None = None
    description: str | None = None
    created_at: datetime | None = None


class CustomerLoyaltyAnalytics(BaseModel):
    is_enrolled: bool = False
    balance_points: int = 0
    total_earned: int = 0
    total_redeemed: int = 0
    points_expire_at: datetime | None = None
    is_expired: bool = False
    points_value_kwd: float = 0.0
    recent_transactions: list[LoyaltyTransactionSummary] = Field(default_factory=list)


class VoucherSummaryItem(BaseModel):
    id: str
    voucher_number: str | None = None
    recipient_name: str | None = None
    recipient_phone: str | None = None
    sender_name: str | None = None
    total_amount: float = 0.0
    status: str = "created"
    created_at: datetime | None = None
    redeemed_at: datetime | None = None


class CustomerVoucherAnalytics(BaseModel):
    total_count: int = 0
    sent_count: int = 0
    received_count: int = 0
    active_count: int = 0
    redeemed_count: int = 0
    total_spent_on_vouchers: float = 0.0
    currency: str = "KWD"
    sent_vouchers: list[VoucherSummaryItem] = Field(default_factory=list)
    received_vouchers: list[VoucherSummaryItem] = Field(default_factory=list)


class BookingSummaryItem(BaseModel):
    id: str
    booking_number: str | None = None
    invoice_number: str | None = None
    service_name: str | None = None
    branch_name: str | None = None
    therapist_name: str | None = None
    appointment_date: datetime | None = None
    total_amount: float = 0.0
    status: str
    payment_status: str


class CustomerSalesAndBookingsAnalytics(BaseModel):
    total_bookings: int = 0
    completed_bookings: int = 0
    upcoming_bookings: int = 0
    cancelled_bookings: int = 0
    cancellation_rate_pct: float = 0.0
    first_booking_date: datetime | None = None
    last_visit_date: datetime | None = None
    days_since_last_visit: int | None = None
    recent_bookings: list[BookingSummaryItem] = Field(default_factory=list)


class CustomerFinancialAnalytics(BaseModel):
    total_spent: float = 0.0
    total_spent_bookings: float = 0.0
    total_spent_vouchers: float = 0.0
    total_refunded: float = 0.0
    net_revenue: float = 0.0
    average_order_value: float = 0.0
    currency: str = "KWD"
    total_transactions: int = 0


class CustomerMarketingAnalytics(BaseModel):
    customer_tier: str = "New Customer"  # VIP, Regular, Occasional, New Customer, Inactive
    favorite_service: str | None = None
    favorite_service_count: int = 0
    preferred_branch: str | None = None
    preferred_therapist: str | None = None
    preferred_day_of_week: str | None = None
    preferred_time_slot: str | None = None
    total_treatment_hours: float = 0.0
    lifecycle_status: str = "Active"


class CustomerFullAnalyticsResponse(BaseModel):
    success: bool = True
    customer_id: str
    phone: str | None = None
    loyalty: CustomerLoyaltyAnalytics
    gift_vouchers: CustomerVoucherAnalytics
    financials: CustomerFinancialAnalytics
    sales_and_bookings: CustomerSalesAndBookingsAnalytics
    marketing_analytics: CustomerMarketingAnalytics


# ── Unified Analytics Endpoint ─────────────────────────────────────────────────

@router.get(
    "/{customer_id}/analytics/",
    response_model=CustomerFullAnalyticsResponse,
    summary="Get unified customer analytics",
    description="Returns aggregated analytics for loyalty, gift vouchers, financials, bookings, and marketing.",
)
@router.get(
    "/{customer_id}/analytics",
    response_model=CustomerFullAnalyticsResponse,
    include_in_schema=False,
)
async def get_customer_analytics(
    customer_id: uuid.UUID,
    session: DBSession,
    phone: str | None = Query(default=None, description="Optional customer phone to match vouchers and bookings"),
    _: OptionalUser = None,
) -> CustomerFullAnalyticsResponse:
    now_utc = datetime.now(timezone.utc)

    # ── 1. Query Loyalty Account & Transactions ──
    loyalty_stmt = select(LoyaltyAccount).where(LoyaltyAccount.customer_id == customer_id)
    loyalty_res = await session.execute(loyalty_stmt)
    loyalty_acc = loyalty_res.scalar_one_or_none()

    loyalty_txns_stmt = (
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.customer_id == customer_id)
        .order_by(LoyaltyTransaction.created_at.desc())
        .limit(10)
    )
    loyalty_txns_res = await session.execute(loyalty_txns_stmt)
    loyalty_txns = loyalty_txns_res.scalars().all()

    loyalty_data = CustomerLoyaltyAnalytics()
    if loyalty_acc:
        is_exp = (
            loyalty_acc.points_expire_at is not None
            and loyalty_acc.points_expire_at.astimezone(timezone.utc) <= now_utc
        )
        loyalty_data = CustomerLoyaltyAnalytics(
            is_enrolled=True,
            balance_points=loyalty_acc.balance_points,
            total_earned=loyalty_acc.total_earned,
            total_redeemed=loyalty_acc.total_redeemed,
            points_expire_at=loyalty_acc.points_expire_at,
            is_expired=is_exp,
            points_value_kwd=round(loyalty_acc.balance_points / 10.0, 3),
            recent_transactions=[
                LoyaltyTransactionSummary(
                    id=str(t.id),
                    transaction_type=t.transaction_type,
                    points=t.points,
                    booking_id=str(t.booking_id) if t.booking_id else None,
                    booking_number=t.booking_number,
                    description=t.description,
                    created_at=t.created_at,
                )
                for t in loyalty_txns
            ],
        )

    # ── 2. Query Gift Vouchers ──
    # Sent vouchers: sender_id == customer_id
    sent_stmt = (
        select(GiftVoucher)
        .where(GiftVoucher.sender_id == customer_id)
        .order_by(GiftVoucher.created_at.desc())
    )
    sent_res = await session.execute(sent_stmt)
    sent_vouchers = sent_res.scalars().all()

    # Received vouchers: recipient_id == customer_id OR recipient_phone == phone
    recv_conds = [GiftVoucher.recipient_id == customer_id]
    if phone and phone.strip():
        cp = phone.strip()
        recv_conds.append(GiftVoucher.recipient_phone == cp)
        if cp.startswith("+"):
            recv_conds.append(GiftVoucher.recipient_phone == cp[1:])
        else:
            recv_conds.append(GiftVoucher.recipient_phone == f"+{cp}")

    recv_stmt = (
        select(GiftVoucher)
        .where(or_(*recv_conds))
        .order_by(GiftVoucher.created_at.desc())
    )
    recv_res = await session.execute(recv_stmt)
    received_vouchers = recv_res.scalars().all()

    # Distinct check for received (avoid counting self-purchased twice if sender==recipient)
    sent_ids = {v.id for v in sent_vouchers}
    unique_received = [v for v in received_vouchers if v.id not in sent_ids]

    sent_spent = sum(
        float(v.total_amount or 0)
        for v in sent_vouchers
        if str(v.status).lower() not in ("cancelled", "expired")
    )

    all_cust_vouchers = list(sent_vouchers) + unique_received
    active_vouchers_count = sum(1 for v in all_cust_vouchers if str(v.status).lower() == "active")
    redeemed_vouchers_count = sum(1 for v in all_cust_vouchers if str(v.status).lower() == "redeemed")

    voucher_data = CustomerVoucherAnalytics(
        total_count=len(sent_vouchers) + len(unique_received),
        sent_count=len(sent_vouchers),
        received_count=len(unique_received),
        active_count=active_vouchers_count,
        redeemed_count=redeemed_vouchers_count,
        total_spent_on_vouchers=round(sent_spent, 3),
        currency="KWD",
        sent_vouchers=[
            VoucherSummaryItem(
                id=str(v.id),
                voucher_number=v.voucher_number,
                recipient_name=(v.recipient_data or {}).get("name") if isinstance(v.recipient_data, dict) else None,
                recipient_phone=v.recipient_phone,
                sender_name=(v.sender_data or {}).get("name") if isinstance(v.sender_data, dict) else None,
                total_amount=float(v.total_amount or 0),
                status=str(v.status or "created"),
                created_at=v.created_at,
                redeemed_at=v.redeemed_at,
            )
            for v in sent_vouchers[:10]
        ],
        received_vouchers=[
            VoucherSummaryItem(
                id=str(v.id),
                voucher_number=v.voucher_number,
                recipient_name=(v.recipient_data or {}).get("name") if isinstance(v.recipient_data, dict) else None,
                recipient_phone=v.recipient_phone,
                sender_name=(v.sender_data or {}).get("name") if isinstance(v.sender_data, dict) else None,
                total_amount=float(v.total_amount or 0),
                status=str(v.status or "created"),
                created_at=v.created_at,
                redeemed_at=v.redeemed_at,
            )
            for v in unique_received[:10]
        ],
    )

    # ── 3. Query Bookings ──
    booking_stmt = (
        select(Booking)
        .where(Booking.customer_id == customer_id)
        .order_by(Booking.appointment_start.desc())
    )
    booking_res = await session.execute(booking_stmt)
    bookings = booking_res.scalars().all()

    completed_bookings = [b for b in bookings if str(b.status).lower() == "completed"]
    cancelled_bookings = [b for b in bookings if str(b.status).lower() == "cancelled"]
    upcoming_bookings = [
        b for b in bookings
        if str(b.status).lower() in ("confirmed", "requested", "in_progress")
        and b.appointment_start
        and b.appointment_start.astimezone(timezone.utc) >= now_utc
    ]

    total_bookings_count = len(bookings)
    cancellation_rate = (
        round((len(cancelled_bookings) / total_bookings_count * 100.0), 1)
        if total_bookings_count > 0
        else 0.0
    )

    first_booking_date = bookings[-1].created_at if bookings else None
    last_visit_date = None
    days_since_last_visit = None
    if completed_bookings:
        last_visit_date = completed_bookings[0].appointment_start or completed_bookings[0].appointment_date
        if last_visit_date:
            days_since_last_visit = max(0, (now_utc - last_visit_date.astimezone(timezone.utc)).days)

    sales_data = CustomerSalesAndBookingsAnalytics(
        total_bookings=total_bookings_count,
        completed_bookings=len(completed_bookings),
        upcoming_bookings=len(upcoming_bookings),
        cancelled_bookings=len(cancelled_bookings),
        cancellation_rate_pct=cancellation_rate,
        first_booking_date=first_booking_date,
        last_visit_date=last_visit_date,
        days_since_last_visit=days_since_last_visit,
        recent_bookings=[
            BookingSummaryItem(
                id=str(b.id),
                booking_number=b.booking_number,
                invoice_number=b.invoice_number,
                service_name=(b.service_data or {}).get("name") if isinstance(b.service_data, dict) else None,
                branch_name=(b.branch_data or {}).get("name") if isinstance(b.branch_data, dict) else None,
                therapist_name=(
                    (b.therapist_data or {}).get("name") or (b.therapist_data or {}).get("full_name")
                    if isinstance(b.therapist_data, dict)
                    else None
                ),
                appointment_date=b.appointment_start or b.appointment_date,
                total_amount=float(b.total_amount or 0),
                status=str(b.status or "requested"),
                payment_status=str(b.payment_status or "pending"),
            )
            for b in bookings[:10]
        ],
    )

    # ── 4. Query Payments & Financial Totals ──
    pmt_stmt = (
        select(Payment)
        .where(Payment.customer_id == customer_id)
        .order_by(Payment.created_at.desc())
    )
    pmt_res = await session.execute(pmt_stmt)
    payments = pmt_res.scalars().all()

    # Sum spending from bookings (paid/completed)
    booking_spent = sum(
        float(b.total_amount or 0)
        for b in bookings
        if str(b.status).lower() == "completed" or str(b.payment_status).lower() in ("paid", "success")
    )
    total_refunded = sum(float(p.amount_refunded or 0) for p in payments)
    total_spent_combined = round(booking_spent + sent_spent, 3)
    net_rev = max(0.0, round(total_spent_combined - total_refunded, 3))
    total_orders_count = len(completed_bookings) + len(sent_vouchers)
    avg_order_val = (
        round(total_spent_combined / total_orders_count, 3)
        if total_orders_count > 0
        else 0.0
    )

    financial_data = CustomerFinancialAnalytics(
        total_spent=total_spent_combined,
        total_spent_bookings=round(booking_spent, 3),
        total_spent_vouchers=round(sent_spent, 3),
        total_refunded=round(total_refunded, 3),
        net_revenue=net_rev,
        average_order_value=avg_order_val,
        currency="KWD",
        total_transactions=len(payments) or total_orders_count,
    )

    # ── 5. Marketing & CRM Insights ──
    service_names = []
    branch_names = []
    therapist_names = []
    day_names = []
    time_slots = []
    total_minutes = 0

    for b in bookings:
        if str(b.status).lower() != "cancelled":
            s_name = (b.service_data or {}).get("name") if isinstance(b.service_data, dict) else None
            if s_name:
                service_names.append(s_name)

            br_name = (b.branch_data or {}).get("name") if isinstance(b.branch_data, dict) else None
            if br_name:
                branch_names.append(br_name)

            th_name = (
                (b.therapist_data or {}).get("name") or (b.therapist_data or {}).get("full_name")
                if isinstance(b.therapist_data, dict)
                else None
            )
            if th_name:
                therapist_names.append(th_name)

            appt_dt = b.appointment_start or b.appointment_date
            if appt_dt:
                day_names.append(appt_dt.strftime("%A"))
                hr = appt_dt.hour
                if hr < 12:
                    time_slots.append("Morning (Before 12:00)")
                elif hr < 17:
                    time_slots.append("Afternoon (12:00 - 17:00)")
                else:
                    time_slots.append("Evening (After 17:00)")

            total_minutes += (b.duration_minutes or 0) + (b.extra_minutes or 0)

    fav_service, fav_service_cnt = (None, 0)
    if service_names:
        most_common_s = Counter(service_names).most_common(1)[0]
        fav_service, fav_service_cnt = most_common_s[0], most_common_s[1]

    pref_branch = Counter(branch_names).most_common(1)[0][0] if branch_names else None
    pref_therapist = Counter(therapist_names).most_common(1)[0][0] if therapist_names else None
    pref_day = Counter(day_names).most_common(1)[0][0] if day_names else None
    pref_time = Counter(time_slots).most_common(1)[0][0] if time_slots else None

    # Customer Tier determination
    tier = "New Customer"
    if total_spent_combined >= 250 or len(completed_bookings) >= 6:
        tier = "VIP Elite"
    elif total_spent_combined >= 120 or len(completed_bookings) >= 3:
        tier = "Regular Loyal"
    elif len(completed_bookings) >= 1 or len(sent_vouchers) >= 1:
        tier = "Occasional"
    elif total_bookings_count > 0:
        tier = "New Lead"

    lifecycle = "Active"
    if days_since_last_visit is not None and days_since_last_visit > 90 and len(upcoming_bookings) == 0:
        lifecycle = "At Risk / Inactive"
    elif len(upcoming_bookings) > 0:
        lifecycle = "Upcoming Visit"

    marketing_data = CustomerMarketingAnalytics(
        customer_tier=tier,
        favorite_service=fav_service,
        favorite_service_count=fav_service_cnt,
        preferred_branch=pref_branch,
        preferred_therapist=pref_therapist,
        preferred_day_of_week=pref_day,
        preferred_time_slot=pref_time,
        total_treatment_hours=round(total_minutes / 60.0, 1),
        lifecycle_status=lifecycle,
    )

    return CustomerFullAnalyticsResponse(
        success=True,
        customer_id=str(customer_id),
        phone=phone,
        loyalty=loyalty_data,
        gift_vouchers=voucher_data,
        financials=financial_data,
        sales_and_bookings=sales_data,
        marketing_analytics=marketing_data,
    )
