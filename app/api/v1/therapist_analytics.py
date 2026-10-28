"""
app/api/v1/therapist_analytics.py
───────────────────────────────────
Unified Therapist Analytics Endpoint for USH Spa.

Provides a single aggregated call returning:
  - Sales Analytics: Gross sales, completed sales, cancellation/no-show rates, avg ticket value, monthly trends.
  - Marketing Analytics: Unique clients, repeat client rate/retention, top services performed, top branches, time-of-day preferences, top clients.
  - Financial Analytics: Gross revenue, collected revenue, pending revenue, add-ons/extra-time revenue, refunds, estimated commissions.
  - Performance Analytics: Total treatment hours, extra minutes, completion rate, punctuality/reliability, day-of-week distribution.
  - Recent Bookings: Detailed log of recent appointments.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.booking.domain.services_snapshot import (
    get_booking_services,
    joined_service_name,
    total_services_price,
)
from app.api.deps import DBSession, OptionalUser
from app.booking.infrastructure.models import Booking
from app.payment.infrastructure.models import Payment

router = APIRouter(prefix="/therapists", tags=["Therapist Analytics"])


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class TopServiceItem(BaseModel):
    service_id: str | None = None
    service_name: str
    booking_count: int = 0
    total_hours: float = 0.0
    total_revenue: float = 0.0
    share_pct: float = 0.0


class BranchPerformanceItem(BaseModel):
    branch_id: str | None = None
    branch_name: str
    booking_count: int = 0
    total_revenue: float = 0.0


class DayDistributionItem(BaseModel):
    day: str
    booking_count: int = 0
    revenue: float = 0.0


class TimeSlotItem(BaseModel):
    slot: str
    booking_count: int = 0
    percentage: float = 0.0


class TopClientItem(BaseModel):
    customer_id: str | None = None
    customer_name: str
    customer_phone: str | None = None
    visit_count: int = 0
    total_spent: float = 0.0
    last_visit: datetime | None = None


class MonthlyTrendItem(BaseModel):
    month: str  # e.g. "2026-10"
    booking_count: int = 0
    completed_count: int = 0
    revenue: float = 0.0


class TherapistBookingItem(BaseModel):
    id: str
    booking_number: str | None = None
    invoice_number: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    service_name: str | None = None
    branch_name: str | None = None
    appointment_date: datetime | None = None
    appointment_start: datetime | None = None
    duration_minutes: int = 0
    extra_minutes: int = 0
    total_amount: float = 0.0
    status: str
    payment_status: str
    booking_type: str | None = None


class TherapistSalesAnalytics(BaseModel):
    total_bookings: int = 0
    completed_bookings: int = 0
    upcoming_bookings: int = 0
    in_progress_bookings: int = 0
    cancelled_bookings: int = 0
    no_show_bookings: int = 0
    completion_rate_pct: float = 0.0
    cancellation_rate_pct: float = 0.0
    no_show_rate_pct: float = 0.0
    gross_sales: float = 0.0
    completed_sales: float = 0.0
    discount_total: float = 0.0
    net_sales: float = 0.0
    average_booking_value: float = 0.0
    currency: str = "KWD"
    monthly_trends: list[MonthlyTrendItem] = Field(default_factory=list)


class TherapistMarketingAnalytics(BaseModel):
    total_unique_clients: int = 0
    repeat_clients_count: int = 0
    repeat_client_rate_pct: float = 0.0
    single_visit_clients: int = 0
    top_services: list[TopServiceItem] = Field(default_factory=list)
    top_branches: list[BranchPerformanceItem] = Field(default_factory=list)
    client_time_preferences: list[TimeSlotItem] = Field(default_factory=list)
    home_service_count: int = 0
    spa_branch_count: int = 0
    top_clients: list[TopClientItem] = Field(default_factory=list)


class TherapistFinancialAnalytics(BaseModel):
    total_gross_revenue: float = 0.0
    completed_revenue: float = 0.0
    pending_revenue: float = 0.0
    addon_revenue: float = 0.0
    extra_minutes_revenue: float = 0.0
    refunded_amount: float = 0.0
    net_revenue: float = 0.0
    estimated_commission: float = 0.0  # standard incentive calculation (10%)
    commission_rate_pct: float = 10.0
    avg_revenue_per_hour: float = 0.0
    currency: str = "KWD"
    total_transactions: int = 0


class TherapistPerformanceAnalytics(BaseModel):
    total_treatment_hours: float = 0.0
    total_treatment_minutes: int = 0
    extra_treatment_minutes: int = 0
    addons_treatment_minutes: int = 0
    average_service_duration_minutes: float = 0.0
    completion_rate_pct: float = 0.0
    reliability_score_pct: float = 0.0  # 100 - (cancellation + no_show rate)
    busiest_day: str | None = None
    busiest_time_slot: str | None = None
    day_distribution: list[DayDistributionItem] = Field(default_factory=list)
    first_appointment_date: datetime | None = None
    latest_appointment_date: datetime | None = None


class TherapistFullAnalyticsResponse(BaseModel):
    success: bool = True
    therapist_id: str
    therapist_name: str | None = None
    sales: TherapistSalesAnalytics
    marketing: TherapistMarketingAnalytics
    financials: TherapistFinancialAnalytics
    performance: TherapistPerformanceAnalytics
    recent_bookings: list[TherapistBookingItem] = Field(default_factory=list)


# ── Unified Analytics Endpoint ─────────────────────────────────────────────────

@router.get(
    "/{therapist_id}/analytics/",
    response_model=TherapistFullAnalyticsResponse,
    summary="Get unified therapist analytics",
    description="Returns aggregated sales, marketing, finance, and performance analytics for a therapist in one call.",
)
@router.get(
    "/{therapist_id}/analytics",
    response_model=TherapistFullAnalyticsResponse,
    include_in_schema=False,
)
async def get_therapist_analytics(
    therapist_id: uuid.UUID,
    session: DBSession,
    _: OptionalUser = None,
) -> TherapistFullAnalyticsResponse:
    now_utc = datetime.now(timezone.utc)

    # ── 1. Query Bookings for this Therapist ──
    booking_stmt = (
        select(Booking)
        .where(
            or_(
                Booking.therapist_id == therapist_id,
                Booking.secondary_therapist_id == therapist_id,
            )
        )
        .order_by(Booking.appointment_start.desc().nullslast(), Booking.appointment_date.desc())
    )
    booking_res = await session.execute(booking_stmt)
    bookings = booking_res.scalars().all()

    # Extract therapist name if present
    therapist_name: str | None = None
    for b in bookings:
        # Use the snapshot that belongs to the requested therapist (primary or secondary)
        t_data = b.therapist_data if b.therapist_id == therapist_id else b.secondary_therapist_data
        if isinstance(t_data, dict):
            therapist_name = (
                t_data.get("name")
                or t_data.get("full_name")
                or f"{t_data.get('first_name', '')} {t_data.get('last_name', '')}".strip()
            )
            if therapist_name:
                break

    # ── 2. Query Payments & Refunds for Therapist's Bookings ──
    booking_ids = [b.id for b in bookings]
    payments: list[Payment] = []
    if booking_ids:
        pmt_stmt = select(Payment).where(Payment.booking_id.in_(booking_ids))
        pmt_res = await session.execute(pmt_stmt)
        payments = pmt_res.scalars().all()

    # Total refunds
    total_refunded = sum(float(p.amount_refunded or 0) for p in payments)

    # ── 3. Sales Categorization ──
    completed_b = [b for b in bookings if str(b.status).lower() == "completed"]
    cancelled_b = [b for b in bookings if str(b.status).lower() == "cancelled"]
    no_show_b = [b for b in bookings if str(b.status).lower() in ("no_show", "noshow")]
    in_progress_b = [b for b in bookings if str(b.status).lower() == "in_progress"]
    upcoming_b = [
        b for b in bookings
        if str(b.status).lower() in ("confirmed", "requested")
        and b.appointment_start
        and b.appointment_start.astimezone(timezone.utc) >= now_utc
    ]

    total_bookings_count = len(bookings)
    completed_count = len(completed_b)
    cancelled_count = len(cancelled_b)
    no_show_count = len(no_show_b)

    completion_rate = (
        round((completed_count / total_bookings_count * 100.0), 1)
        if total_bookings_count > 0 else 0.0
    )
    cancellation_rate = (
        round((cancelled_count / total_bookings_count * 100.0), 1)
        if total_bookings_count > 0 else 0.0
    )
    no_show_rate = (
        round((no_show_count / total_bookings_count * 100.0), 1)
        if total_bookings_count > 0 else 0.0
    )
    reliability_score = max(0.0, round(100.0 - (cancellation_rate + no_show_rate), 1))

    # Revenue & Pricing sums
    gross_sales = sum(float(b.total_amount or 0) for b in bookings if str(b.status).lower() != "cancelled")
    completed_sales = sum(float(b.total_amount or 0) for b in completed_b)
    discount_total = sum(float(b.discount or 0) for b in bookings if str(b.status).lower() != "cancelled")
    addon_rev = sum(float(b.addon_price or 0) for b in bookings if str(b.status).lower() != "cancelled")
    extra_time_rev = sum(float(b.price_for_extra_minutes or 0) for b in bookings if str(b.status).lower() != "cancelled")

    pending_rev = sum(
        float(b.total_amount or 0)
        for b in bookings
        if str(b.status).lower() != "cancelled" and str(b.payment_status).lower() in ("pending", "not_initiated", "partial")
    )
    net_rev = max(0.0, round(completed_sales - total_refunded, 3))
    avg_booking_val = (
        round(completed_sales / completed_count, 3)
        if completed_count > 0 else (round(gross_sales / total_bookings_count, 3) if total_bookings_count > 0 else 0.0)
    )

    # Monthly trends (last 12 months)
    monthly_dict: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "completed": 0, "revenue": 0.0})
    for b in bookings:
        dt = b.appointment_start or b.appointment_date or b.created_at
        if dt:
            m_key = dt.strftime("%Y-%m")
            monthly_dict[m_key]["count"] += 1
            if str(b.status).lower() == "completed":
                monthly_dict[m_key]["completed"] += 1
                monthly_dict[m_key]["revenue"] += float(b.total_amount or 0)

    sorted_months = sorted(monthly_dict.keys(), reverse=True)[:12]
    monthly_trends = [
        MonthlyTrendItem(
            month=m,
            booking_count=int(monthly_dict[m]["count"]),
            completed_count=int(monthly_dict[m]["completed"]),
            revenue=round(monthly_dict[m]["revenue"], 3),
        )
        for m in reversed(sorted_months)
    ]

    sales_data = TherapistSalesAnalytics(
        total_bookings=total_bookings_count,
        completed_bookings=completed_count,
        upcoming_bookings=len(upcoming_b),
        in_progress_bookings=len(in_progress_b),
        cancelled_bookings=cancelled_count,
        no_show_bookings=no_show_count,
        completion_rate_pct=completion_rate,
        cancellation_rate_pct=cancellation_rate,
        no_show_rate_pct=no_show_rate,
        gross_sales=round(gross_sales, 3),
        completed_sales=round(completed_sales, 3),
        discount_total=round(discount_total, 3),
        net_sales=round(net_rev, 3),
        average_booking_value=avg_booking_val,
        currency="KWD",
        monthly_trends=monthly_trends,
    )

    # ── 4. Marketing & Client Retention Analytics ──
    # Group bookings by client
    clients_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "Customer", "phone": None, "count": 0, "spent": 0.0, "last_visit": None}
    )

    service_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "Service", "count": 0, "minutes": 0, "revenue": 0.0}
    )

    branch_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "Branch", "count": 0, "revenue": 0.0}
    )

    time_slots_count: dict[str, int] = defaultdict(int)
    day_counts: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "revenue": 0.0})

    home_service_cnt = 0
    spa_branch_cnt = 0

    total_duration_minutes = 0
    extra_duration_minutes = 0
    addons_duration_minutes = 0

    for b in bookings:
        # Mode
        b_type = str(b.booking_type or "").lower()
        if "home" in b_type:
            home_service_cnt += 1
        else:
            spa_branch_cnt += 1

        is_not_cancelled = str(b.status).lower() != "cancelled"

        # Client intelligence
        c_id = str(b.customer_id) if b.customer_id else None
        c_name = None
        c_phone = None
        if isinstance(b.customer_data, dict):
            c_name = b.customer_data.get("name") or b.customer_data.get("full_name") or f"{b.customer_data.get('first_name', '')} {b.customer_data.get('last_name', '')}".strip()
            c_phone = b.customer_data.get("phone") or b.customer_data.get("phone_number") or b.customer_data.get("mobile")

        c_key = c_id or c_phone or c_name or "Unknown Client"
        clients_map[c_key]["name"] = c_name or clients_map[c_key]["name"]
        clients_map[c_key]["phone"] = c_phone or clients_map[c_key]["phone"]
        clients_map[c_key]["customer_id"] = c_id
        if is_not_cancelled:
            clients_map[c_key]["count"] += 1
            clients_map[c_key]["spent"] += float(b.total_amount or 0)
            appt_time = b.appointment_start or b.appointment_date
            if appt_time:
                prev_last = clients_map[c_key]["last_visit"]
                if not prev_last or appt_time > prev_last:
                    clients_map[c_key]["last_visit"] = appt_time

        # Service breakdown
        # (multi-service: every booked service is counted; the first one carries the
        #  extra minutes and the non-service remainder so booking totals stay exact)
        _entries = get_booking_services(b)
        if len(_entries) <= 1:
            _entries = [
                {
                    "service_id": str(b.service_id) if b.service_id else None,
                    "service_data": b.service_data if isinstance(b.service_data, dict) else {},
                    "duration_minutes": b.duration_minutes or 0,
                    "base_price": None,
                }
            ]
            _single = True
        else:
            _single = False
        _services_sum = float(total_services_price(_entries)) if not _single else 0.0
        for _idx, _e in enumerate(_entries):
            s_id = _e.get("service_id") or None
            _sd = _e.get("service_data") or {}
            s_name = _sd.get("name") or _sd.get("title") or "Service"
            s_key = s_id or s_name
            service_map[s_key]["name"] = s_name
            service_map[s_key]["service_id"] = s_id
            if is_not_cancelled:
                service_map[s_key]["count"] += 1
                if _single:
                    service_map[s_key]["minutes"] += (b.duration_minutes or 0) + (b.extra_minutes or 0)
                    service_map[s_key]["revenue"] += float(b.total_amount or 0)
                else:
                    service_map[s_key]["minutes"] += int(_e.get("duration_minutes") or 0) + (
                        (b.extra_minutes or 0) if _idx == 0 else 0
                    )
                    _rev = float(_e.get("base_price") or 0)
                    if _idx == 0:
                        _rev += float(b.total_amount or 0) - _services_sum
                    service_map[s_key]["revenue"] += _rev

        # Branch breakdown
        br_id = str(b.branch_id) if b.branch_id else None
        br_name = "Main Branch"
        if isinstance(b.branch_data, dict):
            br_name = b.branch_data.get("name") or "Main Branch"
        br_key = br_id or br_name
        branch_map[br_key]["name"] = br_name
        branch_map[br_key]["branch_id"] = br_id
        if is_not_cancelled:
            branch_map[br_key]["count"] += 1
            branch_map[br_key]["revenue"] += float(b.total_amount or 0)

        # Time slot and day of week
        appt_start = b.appointment_start or b.appointment_date
        if appt_start and is_not_cancelled:
            # Day
            day_name = appt_start.strftime("%A")
            day_counts[day_name]["count"] += 1
            day_counts[day_name]["revenue"] += float(b.total_amount or 0)

            # Slot
            hr = appt_start.hour
            if hr < 12:
                time_slots_count["Morning (Before 12:00)"] += 1
            elif hr < 17:
                time_slots_count["Afternoon (12:00 - 17:00)"] += 1
            else:
                time_slots_count["Evening (After 17:00)"] += 1

        # Durations (only for completed or confirmed non-cancelled bookings)
        if is_not_cancelled:
            total_duration_minutes += (b.duration_minutes or 0) + (b.extra_minutes or 0)
            extra_duration_minutes += (b.extra_minutes or 0)
            addons_duration_minutes += (b.addons_duration or 0)

    # Unique & repeat clients
    unique_clients = len(clients_map)
    repeat_clients = sum(1 for c in clients_map.values() if c["count"] > 1)
    single_visit = unique_clients - repeat_clients
    repeat_rate = round((repeat_clients / unique_clients * 100.0), 1) if unique_clients > 0 else 0.0

    # Top services
    top_services_list: list[TopServiceItem] = []
    tot_svc_count = sum(s["count"] for s in service_map.values())
    for s_key, s_val in sorted(service_map.items(), key=lambda x: x[1]["count"], reverse=True)[:10]:
        share = round((s_val["count"] / tot_svc_count * 100.0), 1) if tot_svc_count > 0 else 0.0
        top_services_list.append(
            TopServiceItem(
                service_id=s_val.get("service_id"),
                service_name=s_val["name"],
                booking_count=s_val["count"],
                total_hours=round(s_val["minutes"] / 60.0, 1),
                total_revenue=round(s_val["revenue"], 3),
                share_pct=share,
            )
        )

    # Top branches
    top_branches_list = [
        BranchPerformanceItem(
            branch_id=b_val.get("branch_id"),
            branch_name=b_val["name"],
            booking_count=b_val["count"],
            total_revenue=round(b_val["revenue"], 3),
        )
        for _, b_val in sorted(branch_map.items(), key=lambda x: x[1]["revenue"], reverse=True)
    ]

    # Time slots
    total_time_slots = sum(time_slots_count.values())
    client_time_preferences = [
        TimeSlotItem(
            slot=slot,
            booking_count=cnt,
            percentage=round(cnt / total_time_slots * 100.0, 1) if total_time_slots > 0 else 0.0,
        )
        for slot, cnt in sorted(time_slots_count.items(), key=lambda x: x[1], reverse=True)
    ]

    # Top clients
    top_clients_list: list[TopClientItem] = [
        TopClientItem(
            customer_id=c_val.get("customer_id"),
            customer_name=c_val["name"],
            customer_phone=c_val["phone"],
            visit_count=c_val["count"],
            total_spent=round(c_val["spent"], 3),
            last_visit=c_val["last_visit"],
        )
        for _, c_val in sorted(clients_map.items(), key=lambda x: (x[1]["count"], x[1]["spent"]), reverse=True)[:10]
    ]

    marketing_data = TherapistMarketingAnalytics(
        total_unique_clients=unique_clients,
        repeat_clients_count=repeat_clients,
        repeat_client_rate_pct=repeat_rate,
        single_visit_clients=single_visit,
        top_services=top_services_list,
        top_branches=top_branches_list,
        client_time_preferences=client_time_preferences,
        home_service_count=home_service_cnt,
        spa_branch_count=spa_branch_cnt,
        top_clients=top_clients_list,
    )

    # ── 5. Financial Analytics ──
    total_treatment_hours = round(total_duration_minutes / 60.0, 1)
    avg_rev_per_hour = (
        round(completed_sales / total_treatment_hours, 3)
        if total_treatment_hours > 0 else 0.0
    )
    # 10% standard therapist commission/incentive
    est_commission = round(net_rev * 0.10, 3)

    financial_data = TherapistFinancialAnalytics(
        total_gross_revenue=round(gross_sales, 3),
        completed_revenue=round(completed_sales, 3),
        pending_revenue=round(pending_rev, 3),
        addon_revenue=round(addon_rev, 3),
        extra_minutes_revenue=round(extra_time_rev, 3),
        refunded_amount=round(total_refunded, 3),
        net_revenue=round(net_rev, 3),
        estimated_commission=est_commission,
        commission_rate_pct=10.0,
        avg_revenue_per_hour=avg_rev_per_hour,
        currency="KWD",
        total_transactions=len(payments) or completed_count,
    )

    # ── 6. Performance Analytics ──
    days_order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    day_distribution_list = [
        DayDistributionItem(
            day=d,
            booking_count=int(day_counts[d]["count"]),
            revenue=round(day_counts[d]["revenue"], 3),
        )
        for d in days_order
        if d in day_counts
    ]

    busiest_d = max(day_counts.items(), key=lambda x: x[1]["count"])[0] if day_counts else None
    busiest_slot = client_time_preferences[0].slot if client_time_preferences else None
    avg_duration = round(total_duration_minutes / completed_count, 1) if completed_count > 0 else 0.0

    first_appt = bookings[-1].appointment_start or bookings[-1].appointment_date if bookings else None
    latest_appt = bookings[0].appointment_start or bookings[0].appointment_date if bookings else None

    performance_data = TherapistPerformanceAnalytics(
        total_treatment_hours=total_treatment_hours,
        total_treatment_minutes=total_duration_minutes,
        extra_treatment_minutes=extra_duration_minutes,
        addons_treatment_minutes=addons_duration_minutes,
        average_service_duration_minutes=avg_duration,
        completion_rate_pct=completion_rate,
        reliability_score_pct=reliability_score,
        busiest_day=busiest_d,
        busiest_time_slot=busiest_slot,
        day_distribution=day_distribution_list,
        first_appointment_date=first_appt,
        latest_appointment_date=latest_appt,
    )

    # ── 7. Recent Bookings List ──
    recent_bookings_list = [
        TherapistBookingItem(
            id=str(b.id),
            booking_number=b.booking_number,
            invoice_number=b.invoice_number,
            customer_name=(
                (b.customer_data or {}).get("name")
                or (b.customer_data or {}).get("full_name")
                or f"{(b.customer_data or {}).get('first_name', '')} {(b.customer_data or {}).get('last_name', '')}".strip()
                if isinstance(b.customer_data, dict) else None
            ),
            customer_phone=(
                (b.customer_data or {}).get("phone")
                or (b.customer_data or {}).get("phone_number")
                if isinstance(b.customer_data, dict) else None
            ),
            service_name=(
                joined_service_name(b) or None
            ),
            branch_name=(
                (b.branch_data or {}).get("name")
                if isinstance(b.branch_data, dict) else None
            ),
            appointment_date=b.appointment_date,
            appointment_start=b.appointment_start,
            duration_minutes=b.duration_minutes or 0,
            extra_minutes=b.extra_minutes or 0,
            total_amount=float(b.total_amount or 0),
            status=str(b.status or "requested"),
            payment_status=str(b.payment_status or "pending"),
            booking_type=b.booking_type,
        )
        for b in bookings[:20]
    ]

    return TherapistFullAnalyticsResponse(
        success=True,
        therapist_id=str(therapist_id),
        therapist_name=therapist_name,
        sales=sales_data,
        marketing=marketing_data,
        financials=financial_data,
        performance=performance_data,
        recent_bookings=recent_bookings_list,
    )
