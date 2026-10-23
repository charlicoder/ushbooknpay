#!/usr/bin/env python
"""
scripts/import_appointments_csv.py
──────────────────────────────────
Import a legacy "Appointment List" CSV export as bookings.

For every appointment row:
  * Qty/Service column is split on "," -> one booking per service line.
    All bookings of one row share the same client and the same start time.
  * Customer is resolved from ushauth by phone (country code included,
    default +965).  Unknown phones are created with NO password.
  * Service, arrangement (room) and price come from ushauth:
        price = ArrangementService.price override, else Service.base_price
  * Start time is floored to the 30-minute slot grid (14:40 -> 14:30,
    19:10 -> 19:00).  End = snapped start + service duration (our DB),
    which keeps every booking on the slot grid.
  * The CSV has no therapist: a qualified therapist of the branch is picked
    that is free for the slot (ushauth availability + local + DB overlap).
  * Booking is created through BookingService.create_booking() with
    status=confirmed, payment_status=pending, so the standard
    Booking.Created / Booking.Confirmed SQS events are produced.  ushnotice
    does the rest (appointment cache in ushauth, customer notifications).
    Events are buffered and published only AFTER the DB commit.
  * Re-runs are idempotent: idempotency_key = legacy-import:<ref>:<n>.

Usage (from the ushbooknpay folder, with its .env loaded):

    # 1) dry-run (default): read-only calls to ushauth, nothing is written/sent
    python -m scripts.import_appointments_csv scripts/data/appointment_list_2026-10-23.csv

    # 2) real import (creates customers, bookings, publishes SQS events)
    python -m scripts.import_appointments_csv scripts/data/appointment_list_2026-10-23.csv --execute

Options:
    --branch-name TEXT        Override branch name taken from the CSV group header.
    --country-code 965        Prefix used for local (8 digit) numbers.
    --service-map FILE.json   {"csv service name": "<service uuid | exact service name>"}
    --ignore-availability     Skip ushauth arrangement/therapist availability check
                              (only therapist DB overlap is enforced).
    --ushauth-url URL         Override ushauth HTTP base URL.
    --ushauth-db-url URL      Override ushauth PostgreSQL URL for direct DB fallback.
    --limit N                 Only process the first N rows (testing).
    --report FILE.csv         Result report (default scripts/data/import_report_<ts>.csv).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

KUWAIT_TZ = ZoneInfo("Asia/Kuwait")
SLOT_MINUTES = 30
STATUS_HEADERS = {"confirmed", "new", "requested", "cancelled", "completed", "arrived", "no show"}
REF_RE = re.compile(r"^A\d+$")
DATE_RE = re.compile(r"^[A-Za-z]+,\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})$")


# ── CSV parsing ───────────────────────────────────────────────────────────────

@dataclass
class CsvRow:
    ref: str
    day: date
    start: time
    end: time
    client: str
    services: list[str]
    qty: int
    legacy_user: str
    phone_raw: str
    legacy_status: str
    branch_name: str
    line_no: int


def snap_down(t: time) -> time:
    """Floor a time to the 30-minute slot grid (14:40 -> 14:30)."""
    return time(t.hour, (t.minute // SLOT_MINUTES) * SLOT_MINUTES)


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")[:2]
    return time(int(h), int(m))


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def english_part(service_cell_part: str) -> str:
    """'Stress Reles massage 60   ???? ...' -> 'Stress Reles massage 60'."""
    return clean_spaces(re.split(r"\s{2,}|\?|[\u0600-\u06FF]", service_cell_part.strip())[0])


def parse_csv(path: Path) -> list[CsvRow]:
    rows: list[CsvRow] = []
    current_day: date | None = None
    current_branch = ""
    current_status = ""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for line_no, cells in enumerate(csv.reader(fh), start=1):
            cells = [c.strip() for c in cells] + [""] * (14 - len(cells))
            first = cells[0]
            if not first:
                continue
            if REF_RE.match(first):
                if current_day is None:
                    raise ValueError(f"line {line_no}: appointment before any date header")
                parts = [p for p in (english_part(p) for p in cells[5].split(",")) if p]
                rows.append(
                    CsvRow(
                        ref=first,
                        day=current_day,
                        start=parse_hhmm(cells[2]),
                        end=parse_hhmm(cells[3]),
                        client=clean_spaces(cells[4]),
                        services=parts,
                        qty=int(cells[6] or len(parts) or 1),
                        legacy_user=cells[7],
                        phone_raw=cells[8],
                        legacy_status=current_status,
                        branch_name=current_branch,
                        line_no=line_no,
                    )
                )
                continue
            m = DATE_RE.match(first)
            if m:
                current_day = datetime.strptime(m.group(1), "%d %B %Y").date()
                continue
            if first.lower() in STATUS_HEADERS:
                current_status = first.lower()
                continue
            # branch header: only the name + trailing count cell are filled
            if all(not c for c in cells[1:13]) and cells[13].isdigit() and not first.lower().startswith(
                ("grand total", "appointment list")
            ):
                current_branch = first
    return rows


def normalise_phone(raw: str, country_code: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if raw.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 8:
        return f"+{country_code}{digits}"
    if digits.startswith(country_code) and len(digits) == len(country_code) + 8:
        return f"+{digits}"
    return f"+{digits}"


# ── ushauth access (HTTP client + Direct PostgreSQL fallback) ─────────────────

class _NoRedis:
    """USHAuthClient expects a redis client; the script does not need caching."""

    async def get(self, *_a: Any, **_k: Any) -> None:
        return None

    async def setex(self, *_a: Any, **_k: Any) -> None:
        return None


def _discover_ushauth_db_url(explicit_url: str | None = None) -> str | None:
    """Discover ushauth's DATABASE_URL from CLI arg, env var, or ../ushauth/.env."""
    from app.core.config import _normalize_docker_hostname

    if explicit_url:
        return _normalize_docker_hostname(explicit_url)
    env_val = os.environ.get("USHAUTH_DATABASE_URL")
    if env_val:
        return _normalize_docker_hostname(env_val)

    ushauth_env = Path(__file__).resolve().parents[2] / "ushauth" / ".env"
    if ushauth_env.exists():
        for line in ushauth_env.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("DATABASE_URL="):
                val = stripped.split("=", 1)[1].split("#", 1)[0].strip().strip("\"'")
                if val.startswith("postgresql://"):
                    val = val.replace("postgresql://", "postgresql+asyncpg://", 1)
                return _normalize_docker_hostname(val)
    return None


class DirectUshAuthDB:
    """Direct async SQLAlchemy adapter for ushauth's PostgreSQL database.

    Used automatically when the ushauth HTTP container is not running on the host
    machine, while local PostgreSQL is reachable.
    """

    def __init__(self, db_url: str) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        self.db_url = db_url
        self._engine = create_async_engine(db_url, pool_pre_ping=True)

    async def aclose(self) -> None:
        await self._engine.dispose()

    async def list_branches(self) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, name, name_en, name_ar, address_line1, "
                    "opening_time, closing_time, status "
                    "FROM branches WHERE status = 'active' ORDER BY sort_order, name"
                )
            )
            return [
                {
                    "id": str(r.id),
                    "name": r.name or r.name_en or "",
                    "name_en": r.name_en or r.name or "",
                    "name_ar": r.name_ar or "",
                    "address_line1": r.address_line1 or "",
                    "opening_time": str(r.opening_time) if r.opening_time else "09:00:00",
                    "closing_time": str(r.closing_time) if r.closing_time else "22:00:00",
                    "status": r.status,
                }
                for r in res
            ]

    async def list_services(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            rows = []
            if branch_id:
                res = await conn.execute(
                    text(
                        "SELECT DISTINCT s.id, s.name, s.name_en, s.name_ar, "
                        "s.duration_minutes, s.base_price, s.currency, "
                        "s.is_eligible_for_loyalty, s.loyalty_points, s.price_in_points, "
                        "s.status, s.sort_order "
                        "FROM services s "
                        "JOIN arrangement_services asvc ON asvc.service_id = s.id AND asvc.is_active = TRUE "
                        "JOIN service_arrangements sa ON sa.id = asvc.arrangement_id AND sa.status = 'active' "
                        "WHERE s.status = 'active' AND sa.branch_id = CAST(:bid AS uuid) "
                        "ORDER BY s.sort_order, s.name"
                    ),
                    {"bid": str(branch_id)},
                )
                rows = list(res)
            if not rows:
                res = await conn.execute(
                    text(
                        "SELECT id, name, name_en, name_ar, duration_minutes, base_price, "
                        "currency, is_eligible_for_loyalty, loyalty_points, price_in_points, "
                        "status, sort_order "
                        "FROM services WHERE status = 'active' ORDER BY sort_order, name"
                    )
                )
                rows = list(res)
            return [
                {
                    "id": str(r.id),
                    "name": r.name or r.name_en or "",
                    "name_en": r.name_en or r.name or "",
                    "name_ar": r.name_ar or "",
                    "duration_minutes": int(r.duration_minutes or 60),
                    "base_price": str(r.base_price if r.base_price is not None else "0.000"),
                    "currency": r.currency or "KWD",
                    "is_eligible_for_loyalty": bool(r.is_eligible_for_loyalty),
                    "loyalty_points": int(r.loyalty_points or 0),
                    "price_in_points": int(r.price_in_points or 0),
                    "status": r.status,
                }
                for r in rows
            ]

    async def get_service(self, service_id: str) -> dict[str, Any]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, name, name_en, name_ar, duration_minutes, base_price, "
                    "currency, is_eligible_for_loyalty, loyalty_points, price_in_points, status "
                    "FROM services WHERE id = CAST(:sid AS uuid)"
                ),
                {"sid": str(service_id)},
            )
            r = res.first()
            if not r:
                return {}
            return {
                "id": str(r.id),
                "name": r.name or r.name_en or "",
                "name_en": r.name_en or r.name or "",
                "name_ar": r.name_ar or "",
                "duration_minutes": int(r.duration_minutes or 60),
                "base_price": str(r.base_price if r.base_price is not None else "0.000"),
                "currency": r.currency or "KWD",
                "is_eligible_for_loyalty": bool(r.is_eligible_for_loyalty),
                "loyalty_points": int(r.loyalty_points or 0),
                "price_in_points": int(r.price_in_points or 0),
                "status": r.status,
            }

    async def get_service_arrangement(self, arrangement_id: str) -> dict[str, Any]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT id, name, name_en, name_ar, arrangement_type, branch_id, capacity, status "
                    "FROM service_arrangements WHERE id = CAST(:aid AS uuid)"
                ),
                {"aid": str(arrangement_id)},
            )
            r = res.first()
            if not r:
                return {}
            asvc_res = await conn.execute(
                text(
                    "SELECT service_id, price, currency, price_in_points, loyalty_points, is_active "
                    "FROM arrangement_services WHERE arrangement_id = CAST(:aid AS uuid) AND is_active = TRUE"
                ),
                {"aid": str(arrangement_id)},
            )
            return {
                "id": str(r.id),
                "name": r.name or r.name_en or "",
                "arrangement_type": r.arrangement_type or "",
                "branch_id": str(r.branch_id),
                "capacity": int(r.capacity or 1),
                "status": r.status,
                "arrangement_services": [
                    {
                        "service_id": str(a.service_id),
                        "price": str(a.price) if a.price is not None else None,
                        "currency": a.currency,
                        "price_in_points": a.price_in_points,
                        "loyalty_points": a.loyalty_points,
                    }
                    for a in asvc_res
                ],
            }

    async def _get_all_pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import text

        params = params or {}
        branch_id = params.get("branch_id")
        m = re.match(r"^/api/v1/services/([^/]+)/arrangements/?$", path)
        async with self._engine.connect() as conn:
            if m:
                service_id = m.group(1)
                q = (
                    "SELECT sa.id, sa.name, sa.name_en, sa.name_ar, sa.arrangement_type, "
                    "sa.branch_id, sa.capacity, sa.status, sa.sort_order, "
                    "asvc.price AS arrangement_price, asvc.currency AS arrangement_currency, "
                    "asvc.price_in_points AS arrangement_price_in_points, "
                    "asvc.loyalty_points AS arrangement_loyalty_points "
                    "FROM service_arrangements sa "
                    "JOIN arrangement_services asvc ON asvc.arrangement_id = sa.id "
                    "WHERE sa.status = 'active' AND asvc.is_active = TRUE "
                    "AND asvc.service_id = CAST(:sid AS uuid)"
                )
                bind: dict[str, Any] = {"sid": str(service_id)}
                if branch_id:
                    q += " AND sa.branch_id = CAST(:bid AS uuid)"
                    bind["bid"] = str(branch_id)
                q += " ORDER BY sa.sort_order, sa.name"
                res = await conn.execute(text(q), bind)
                return [
                    {
                        "id": str(r.id),
                        "name": r.name or r.name_en or "",
                        "name_en": r.name_en or r.name or "",
                        "name_ar": r.name_ar or "",
                        "arrangement_type": r.arrangement_type or "",
                        "branch_id": str(r.branch_id),
                        "capacity": int(r.capacity or 1),
                        "arrangement_price": str(r.arrangement_price) if r.arrangement_price is not None else None,
                        "arrangement_currency": r.arrangement_currency,
                        "arrangement_price_in_points": r.arrangement_price_in_points,
                        "arrangement_loyalty_points": r.arrangement_loyalty_points,
                    }
                    for r in res
                ]
            if path.rstrip("/") == "/api/v1/service-arrangements":
                q = (
                    "SELECT id, name, name_en, name_ar, arrangement_type, branch_id, capacity, status, sort_order "
                    "FROM service_arrangements WHERE status = 'active'"
                )
                bind = {}
                if branch_id:
                    q += " AND branch_id = CAST(:bid AS uuid)"
                    bind["bid"] = str(branch_id)
                q += " ORDER BY sort_order, name"
                res = await conn.execute(text(q), bind)
                return [
                    {
                        "id": str(r.id),
                        "name": r.name or r.name_en or "",
                        "name_en": r.name_en or r.name or "",
                        "name_ar": r.name_ar or "",
                        "arrangement_type": r.arrangement_type or "",
                        "branch_id": str(r.branch_id),
                        "capacity": int(r.capacity or 1),
                        "arrangement_price": None,
                        "arrangement_currency": None,
                        "arrangement_price_in_points": None,
                        "arrangement_loyalty_points": None,
                    }
                    for r in res
                ]
            if path.rstrip("/") == "/api/v1/addons":
                res = await conn.execute(
                    text(
                        "SELECT id, name, name_en, name_ar, price, currency, duration_minutes, is_active "
                        "FROM addon_services WHERE is_active = TRUE ORDER BY name"
                    )
                )
                return [
                    {
                        "id": str(r.id),
                        "name": r.name or r.name_en or "",
                        "name_en": r.name_en or r.name or "",
                        "name_ar": r.name_ar or "",
                        "price": str(r.price if r.price is not None else "0.000"),
                        "currency": r.currency or "KWD",
                        "duration_minutes": int(r.duration_minutes or 15),
                        "is_active": bool(r.is_active),
                    }
                    for r in res
                ]
        return []

    async def list_therapists(
        self, branch_id: str | None = None, service_id: str | None = None
    ) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            q = (
                "SELECT DISTINCT t.id, u.first_name, u.last_name, u.phone_number, "
                "t.branch_id, t.status, t.available_for_home_service, t.created_at "
                "FROM therapists t "
                "JOIN employees e ON e.id = t.employee_id "
                "JOIN users u ON u.id = e.user_id "
            )
            bind: dict[str, Any] = {}
            if service_id:
                q += "JOIN therapist_services ts ON ts.therapist_id = t.id AND ts.is_active = TRUE "
            q += "WHERE t.status = 'active' AND e.status = 'active'"
            if branch_id:
                q += " AND t.branch_id = CAST(:bid AS uuid)"
                bind["bid"] = str(branch_id)
            if service_id:
                q += " AND ts.service_id = CAST(:sid AS uuid)"
                bind["sid"] = str(service_id)
            q += " ORDER BY t.created_at"
            res = await conn.execute(text(q), bind)
            return [
                {
                    "id": str(r.id),
                    "first_name": r.first_name or "",
                    "last_name": r.last_name or "",
                    "phone_number": r.phone_number or "",
                    "branch_id": str(r.branch_id),
                    "status": r.status,
                    "available_for_home_service": bool(r.available_for_home_service),
                }
                for r in res
            ]

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        from sqlalchemy import text

        params = params or {}
        if path.rstrip("/") == "/api/v1/customers":
            search = str(params.get("search") or "").strip().lstrip("+")
            async with self._engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT u.id AS user_id, c.id AS customer_id, u.phone_number, "
                        "u.first_name, u.last_name, u.email, u.language_preference "
                        "FROM customers c JOIN users u ON u.id = c.user_id "
                        "WHERE u.phone_number LIKE :pat"
                    ),
                    {"pat": f"%{search}%"},
                )
                return {
                    "data": [
                        {
                            "id": str(r.user_id),
                            "customer_id": str(r.customer_id),
                            "phone_number": r.phone_number,
                            "name": clean_spaces(f"{r.first_name or ''} {r.last_name or ''}"),
                            "email": r.email or "",
                            "language_preference": r.language_preference or "ar",
                        }
                        for r in res
                    ]
                }
        return {"data": []}

    async def get_or_create_customer(
        self, *, phone_number: str, full_name: str, without_password: bool = True
    ) -> dict[str, Any]:
        from sqlalchemy import text

        parts = clean_spaces(full_name).split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    "SELECT u.id AS user_id, c.id AS customer_id, u.phone_number, "
                    "u.first_name, u.last_name, u.email, u.language_preference "
                    "FROM users u LEFT JOIN customers c ON c.user_id = u.id "
                    "WHERE u.phone_number = :phone LIMIT 1"
                ),
                {"phone": phone_number},
            )
            row = res.first()
            if row:
                user_id = str(row.user_id)
                cust_id = str(row.customer_id) if row.customer_id else None
                if not cust_id:
                    cust_id = str(uuid.uuid4())
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        text(
                            "INSERT INTO customers "
                            "(id, user_id, notification_channel, is_mobile_verified, "
                            "is_whatsapp_verified, account_status, is_loyalty_enrolled, created_at, updated_at) "
                            "VALUES (CAST(:cid AS uuid), CAST(:uid AS uuid), 'whatsapp', TRUE, FALSE, "
                            "'active', FALSE, :now, :now)"
                        ),
                        {"cid": cust_id, "uid": user_id, "now": now},
                    )
                existing_name = clean_spaces(f"{row.first_name or ''} {row.last_name or ''}")
                return {
                    "id": user_id,
                    "customer_id": cust_id,
                    "name": existing_name or clean_spaces(full_name),
                    "phone_number": row.phone_number,
                    "email": row.email or None,
                    "language_preference": row.language_preference or "ar",
                    "created": False,
                }

            user_id = str(uuid.uuid4())
            cust_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            await conn.execute(
                text(
                    "INSERT INTO users "
                    "(id, password, is_superuser, phone_number, email, first_name, last_name, "
                    "user_type, language_preference, is_active, is_email_verified, is_staff, created_at, updated_at) "
                    "VALUES (CAST(:uid AS uuid), :pwd, FALSE, :phone, NULL, :fn, :ln, "
                    "'customer', 'ar', TRUE, FALSE, FALSE, :now, :now)"
                ),
                {
                    "uid": user_id,
                    "pwd": "!" if without_password else "!",
                    "phone": phone_number,
                    "fn": first_name,
                    "ln": last_name,
                    "now": now,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO customers "
                    "(id, user_id, notification_channel, is_mobile_verified, "
                    "is_whatsapp_verified, account_status, is_loyalty_enrolled, created_at, updated_at) "
                    "VALUES (CAST(:cid AS uuid), CAST(:uid AS uuid), 'whatsapp', TRUE, FALSE, "
                    "'active', FALSE, :now, :now)"
                ),
                {"cid": cust_id, "uid": user_id, "now": now},
            )
            return {
                "id": user_id,
                "customer_id": cust_id,
                "name": clean_spaces(full_name),
                "phone_number": phone_number,
                "email": None,
                "language_preference": "ar",
                "created": True,
            }

    async def check_appointment_availability(
        self,
        *,
        service_arrangement_id: str,
        appointment_date: str,
        appointment_time: str,
        duration: int,
        therapist_ids: list[str],
    ) -> dict[str, Any]:
        from sqlalchemy import text

        day = datetime.strptime(appointment_date, "%Y-%m-%d").date()
        t_parts = [int(x) for x in appointment_time.split(":")]
        start_t = time(t_parts[0], t_parts[1], t_parts[2] if len(t_parts) > 2 else 0)
        start_dt = datetime.combine(day, start_t, tzinfo=KUWAIT_TZ)
        end_dt = start_dt + timedelta(minutes=int(duration))

        async with self._engine.connect() as conn:
            arr_res = await conn.execute(
                text("SELECT capacity FROM service_arrangements WHERE id = CAST(:aid AS uuid)"),
                {"aid": str(service_arrangement_id)},
            )
            arr_row = arr_res.first()
            if not arr_row:
                return {"available": "no", "reason": "arrangement_not_found"}
            capacity = int(arr_row.capacity or 1)

            overlap_res = await conn.execute(
                text(
                    "SELECT COUNT(*) AS cnt FROM ush_spa_appointment_cache "
                    "WHERE servicearrangement_id = CAST(:aid AS uuid) "
                    "AND appointment_date = :day "
                    "AND status IN ('requested', 'confirmed', 'rescheduled', 'payment_pending', 'hold') "
                    "AND appointment_start < :end_dt AND appointment_end > :start_dt"
                ),
                {
                    "aid": str(service_arrangement_id),
                    "day": day,
                    "start_dt": start_dt,
                    "end_dt": end_dt,
                },
            )
            if int(overlap_res.scalar() or 0) >= capacity:
                return {"available": "no", "reason": "arrangement_at_capacity"}

            for tid in therapist_ids:
                t_res = await conn.execute(
                    text(
                        "SELECT COUNT(*) AS cnt FROM ush_spa_appointment_cache "
                        "WHERE therapist_id = CAST(:tid AS uuid) "
                        "AND appointment_date = :day "
                        "AND status IN ('requested', 'confirmed', 'rescheduled', 'payment_pending', 'hold') "
                        "AND appointment_start < :end_dt AND appointment_end > :start_dt"
                    ),
                    {"tid": str(tid), "day": day, "start_dt": start_dt, "end_dt": end_dt},
                )
                if int(t_res.scalar() or 0) == 0:
                    return {"available": "yes", "therapist_id": str(tid)}

        return {"available": "no", "reason": "no_therapist_available"}

    async def sync_appointment_cache(
        self,
        *,
        booking_id: str,
        service_arrangement_id: str,
        service_arrangement_name: str,
        branch_id: str,
        branch_name: str,
        therapist_id: str,
        therapist_name: str,
        customer_id: str,
        customer_name: str,
        service_id: str,
        service_name: str,
        appointment_start: datetime,
        appointment_end: datetime,
        duration_minutes: int,
        status: str = "confirmed",
        payment_status: str = "pending",
    ) -> None:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        async with self._engine.begin() as conn:
            existing = await conn.execute(
                text("SELECT id FROM ush_spa_appointment_cache WHERE bookings_id = CAST(:bid AS uuid)"),
                {"bid": str(booking_id)},
            )
            if existing.first():
                return
            await conn.execute(
                text(
                    "INSERT INTO ush_spa_appointment_cache "
                    "(id, servicearrangement_id, bookings_id, branch_id, branch_name, "
                    "service_arrangement_name, therapist_id, therapist_name, customer_id, customer_name, "
                    "service_id, service_name, booking_type, payment_status, appointment_date, "
                    "appointment_start, appointment_end, duration_minutes, status, created_at, updated_at) "
                    "VALUES (CAST(:id AS uuid), CAST(:aid AS uuid), CAST(:bid AS uuid), CAST(:brid AS uuid), :brname, "
                    ":aname, CAST(:tid AS uuid), :tname, CAST(:cid AS uuid), :cname, "
                    "CAST(:sid AS uuid), :sname, 'branch_service', :pstatus, :adate, "
                    ":astart, :aend, :dur, :status, :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "aid": str(service_arrangement_id),
                    "bid": str(booking_id),
                    "brid": str(branch_id),
                    "brname": branch_name,
                    "aname": service_arrangement_name,
                    "tid": str(therapist_id),
                    "tname": therapist_name,
                    "cid": str(customer_id),
                    "cname": customer_name,
                    "sid": str(service_id),
                    "sname": service_name,
                    "pstatus": payment_status,
                    "adate": appointment_start.date(),
                    "astart": appointment_start,
                    "aend": appointment_end,
                    "dur": int(duration_minutes),
                    "status": status,
                    "now": now,
                },
            )


def _norm(value: str) -> str:
    return clean_spaces(value).lower()


def _nums(value: str) -> list[str]:
    return re.findall(r"\d+", value)


_TYPO_MAP = (
    (r"\breles\b", "release"),
    (r"\bherbel\b", "herbal"),
    (r"\bmessage\b", "massage"),
    (r"\bventusa\b", "kasat hwa"),
    (r"\bventosa\b", "kasat hwa"),
)


def _norm_service_alias(value: str) -> str:
    s = _norm(english_part(value))
    for pat, repl in _TYPO_MAP:
        s = re.sub(pat, repl, s)
    return clean_spaces(s)


def _strip_numbers(value: str) -> str:
    return clean_spaces(re.sub(r"\b\d+\b", "", value))


def _strip_parens(value: str) -> str:
    return clean_spaces(re.sub(r"\([^)]*\)", "", value))



class Catalog:
    """Cached lookups of branch / services / arrangements / therapists in ushauth."""

    def __init__(self, ushauth: Any) -> None:
        self.u = ushauth
        self.branch: dict[str, Any] = {}
        self.services: list[dict[str, Any]] = []
        self._arr: dict[str, list[dict[str, Any]]] = {}
        self._ther: dict[str, list[dict[str, Any]]] = {}
        self._all_arr: list[dict[str, Any]] | None = None
        self._all_ther: list[dict[str, Any]] | None = None

    async def load_branch(self, name: str) -> dict[str, Any]:
        branches = await self.u.list_branches()
        want = _norm(name)
        want_core = clean_spaces(re.sub(r"\b(ush|spa|branch)\b", "", want))

        def _branch_score(b: dict[str, Any]) -> float:
            bname = _norm(b.get("name", ""))
            bcore = clean_spaces(re.sub(r"\b(ush|spa|branch)\b", "", bname))
            if bname == want or (want_core and bcore == want_core):
                return 1.0
            if want_core and (want_core in bcore or bcore in want_core):
                return 0.9
            return max(
                SequenceMatcher(None, want, bname).ratio(),
                SequenceMatcher(None, want_core, bcore).ratio() if want_core and bcore else 0.0,
            )

        ranked = sorted(branches, key=_branch_score, reverse=True)
        pick = ranked[0] if ranked else None
        if not pick or _branch_score(pick) < 0.6:
            raise SystemExit(
                f"Branch '{name}' not found in ushauth. Available: {[b.get('name') for b in branches]}"
            )
        self.branch = pick
        self.services = await self.u.list_services(str(pick["id"]))
        if not self.services:
            self.services = await self.u.list_services(None)
        try:
            addons = await self.u._get_all_pages("/api/v1/addons/")
            existing_ids = {str(s["id"]) for s in self.services}
            for a in addons:
                aid = str(a["id"])
                if aid not in existing_ids:
                    self.services.append(
                        {
                            "id": aid,
                            "name": a.get("name") or a.get("name_en") or "",
                            "name_en": a.get("name_en") or a.get("name") or "",
                            "name_ar": a.get("name_ar") or "",
                            "duration_minutes": int(a.get("duration_minutes") or 15),
                            "base_price": str(a.get("price") or a.get("base_price") or "0.000"),
                            "currency": a.get("currency") or "KWD",
                            "is_eligible_for_loyalty": False,
                            "loyalty_points": 0,
                            "price_in_points": 0,
                            "is_addon": True,
                        }
                    )
        except Exception:
            pass
        return pick

    def resolve_service(self, csv_name: str, overrides: dict[str, str]) -> dict[str, Any] | None:
        key = _norm(csv_name)
        if key in overrides:
            target = overrides[key]
            for s in self.services:
                if (
                    str(s["id"]) == target
                    or _norm(s.get("name", "")) == _norm(target)
                    or _norm(english_part(s.get("name_en") or s.get("name") or "")) == _norm(target)
                ):
                    return s
            return None

        key_alias = _norm_service_alias(csv_name)
        key_no_num = _strip_numbers(key_alias)
        key_nums = _nums(key)

        for s in self.services:
            raw_name = s.get("name_en") or s.get("name") or ""
            candidates = {
                _norm(s.get("name", "")),
                _norm(s.get("name_en", "")),
                _norm(english_part(raw_name)),
                _norm_service_alias(raw_name),
                _norm_service_alias(_strip_parens(raw_name)),
            }
            if key in candidates or key_alias in candidates:
                return s

        best, best_score = None, 0.0
        key_words = {w for w in key_no_num.split() if w not in {"massage", "message", "service", "the"}}
        for s in self.services:
            raw_name = s.get("name_en") or s.get("name") or ""
            sname = _norm(english_part(raw_name))
            sname_alias = _norm_service_alias(raw_name)
            sname_noparens = _norm_service_alias(_strip_parens(raw_name))
            s_nums = _nums(sname)
            s_dur = str(int(s.get("duration_minutes") or 0))

            if key_nums and s_nums and key_nums != s_nums:
                continue  # "massage 60" must never match "massage 90" when DB names carry durations

            dur_bonus = 0.02 if (key_nums and s_dur == key_nums[0]) else 0.0
            score = max(
                SequenceMatcher(None, key, sname).ratio(),
                SequenceMatcher(None, key_alias, sname_alias).ratio(),
                SequenceMatcher(None, key_alias, sname_noparens).ratio(),
                SequenceMatcher(None, key_no_num, _strip_numbers(sname_alias)).ratio(),
                SequenceMatcher(None, key_no_num, _strip_numbers(sname_noparens)).ratio(),
            ) + dur_bonus

            s_words = {w for w in _strip_numbers(sname_noparens).split() if w not in {"massage", "message", "service", "the"}}
            if key_words and key_words.issubset(s_words):
                score = max(score, 0.90 + dur_bonus)

            if score > best_score:
                best, best_score = s, score
        return best if best_score >= 0.75 else None

    async def all_branch_arrangements(self) -> list[dict[str, Any]]:
        if self._all_arr is None:
            self._all_arr = await self.u._get_all_pages(
                "/api/v1/service-arrangements/",
                {"branch_id": str(self.branch["id"])},
            )
        return self._all_arr

    async def all_branch_therapists(self) -> list[dict[str, Any]]:
        if self._all_ther is None:
            self._all_ther = await self.u.list_therapists(
                branch_id=str(self.branch["id"]), service_id=None
            )
        return self._all_ther

    async def arrangements(self, service_id: str, *, is_addon: bool = False) -> list[dict[str, Any]]:
        if service_id not in self._arr:
            all_arr = await self.all_branch_arrangements()
            if is_addon:
                # Prefer non-massage-room arrangements first so add-ons (Jacuzzi / Kasat Hwa)
                # do not consume primary massage rooms before massages in the same slot.
                items = sorted(
                    all_arr,
                    key=lambda a: (1 if "deluxe" in _norm(a.get("name", "")) else 0),
                )
            else:
                primary = await self.u._get_all_pages(
                    f"/api/v1/services/{service_id}/arrangements/",
                    {"branch_id": str(self.branch["id"])},
                )
                seen = {str(a["id"]) for a in primary}
                items = list(primary) + [a for a in all_arr if str(a["id"]) not in seen]
            self._arr[service_id] = items
        return self._arr[service_id]

    async def therapists(self, service_id: str, *, is_addon: bool = False) -> list[dict[str, Any]]:
        if service_id not in self._ther:
            all_ther = await self.all_branch_therapists()
            if is_addon:
                items = list(all_ther)
            else:
                primary = await self.u.list_therapists(
                    branch_id=str(self.branch["id"]), service_id=service_id
                )
                seen = {str(t["id"]) for t in primary}
                items = list(primary) + [t for t in all_ther if str(t["id"]) not in seen]
            self._ther[service_id] = items
        return self._ther[service_id]


# ── placement bookkeeping (local view of what this run already assigned) ──────

def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and a[1] > b[0]


@dataclass
class Ledger:
    therapist: dict[str, list[tuple[datetime, datetime]]] = field(default_factory=dict)
    arrangement: dict[str, list[tuple[datetime, datetime]]] = field(default_factory=dict)

    def therapist_busy(self, tid: str, slot: tuple[datetime, datetime]) -> bool:
        return any(_overlap(slot, s) for s in self.therapist.get(tid, []))

    def arrangement_load(self, aid: str, slot: tuple[datetime, datetime]) -> int:
        return sum(1 for s in self.arrangement.get(aid, []) if _overlap(slot, s))

    def minutes(self, tid: str) -> float:
        return sum((e - s).total_seconds() / 60 for s, e in self.therapist.get(tid, []))

    def add(self, tid: str, aid: str, slot: tuple[datetime, datetime]) -> None:
        self.therapist.setdefault(tid, []).append(slot)
        self.arrangement.setdefault(aid, []).append(slot)


@dataclass
class Result:
    ref: str
    index: int
    service: str
    phone: str
    client: str
    status: str  # created | planned | skipped | failed
    booking_number: str = ""
    start: str = ""
    end: str = ""
    therapist: str = ""
    arrangement: str = ""
    price: str = ""
    reason: str = ""


async def _connect_ushauth(
    *,
    http: httpx.AsyncClient,
    settings: Any,
    ushauth_url: str | None,
    ushauth_db_url: str | None,
) -> tuple[Any, DirectUshAuthDB | None]:
    """Return a working ushauth client (HTTP preferred, falling back to DirectUshAuthDB)."""
    from app.core.config import _normalize_docker_hostname
    from app.integrations.ushauth_client import USHAuthClient

    candidate_urls: list[str] = []
    if ushauth_url:
        candidate_urls.append(_normalize_docker_hostname(ushauth_url).rstrip("/"))
    else:
        primary = _normalize_docker_hostname(settings.ushauth_base_url).rstrip("/")
        candidate_urls.append(primary)
        for fallback_url in ("http://localhost:8002", "http://localhost:8000/uauth", "http://localhost:8000"):
            if fallback_url not in candidate_urls:
                candidate_urls.append(fallback_url)

    last_exc: Exception | None = None
    for url in candidate_urls:
        settings.USHAUTH_DIRECT_URL = url
        client = USHAuthClient(http_client=http, redis_client=_NoRedis(), settings=settings)
        try:
            await client.list_branches()
            return client, None
        except httpx.ConnectError as exc:
            last_exc = exc
            continue
        except Exception:
            # Non-connection HTTP response ( or auth error ) means server is reachable at this URL
            return client, None

    db_url = _discover_ushauth_db_url(ushauth_db_url)
    if db_url:
        print(f"[info] ushauth HTTP API unreachable ({ candidate_urls[0] }); using direct ushauth DB fallback.")
        direct_db = DirectUshAuthDB(db_url)
        return direct_db, direct_db

    if last_exc:
        raise last_exc
    raise RuntimeError("Unable to connect to ushauth via HTTP or database.")


# ── main import logic ─────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    # imports that need the app settings/.env are done lazily
    from app.booking.application.services import BookingService
    from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
    from app.clients.ushauth import get_or_create_customer
    from app.core.config import get_settings
    from app.core.database import dispose_engine, get_session_factory
    from app.core.exceptions import DoubleBookingError
    from app.events.sqs_client import get_sqs_client

    class BufferedBookingService(BookingService):
        """Collect events; the script publishes them after the DB commit."""

        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.buffered_events: list[Any] = []

        async def _enqueue_event(self, event: object) -> None:  # noqa: D401
            self.buffered_events.append(event)

    settings = get_settings()
    rows = parse_csv(Path(args.csv))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No appointment rows found in CSV.")
        return 1

    overrides = {}
    if args.service_map:
        overrides = {_norm(k): v for k, v in json.loads(Path(args.service_map).read_text()).items()}

    http = httpx.AsyncClient(headers={"Accept-Language": "en"}, timeout=settings.GATEWAY_TIMEOUT)
    direct_db: DirectUshAuthDB | None = None
    results: list[Result] = []
    ledger = Ledger()
    sqs = get_sqs_client()
    factory = get_session_factory() if args.execute else None

    try:
        ushauth, direct_db = await _connect_ushauth(
            http=http,
            settings=settings,
            ushauth_url=args.ushauth_url,
            ushauth_db_url=args.ushauth_db_url,
        )
        catalog = Catalog(ushauth)

        branch_name = args.branch_name or rows[0].branch_name
        branch = await catalog.load_branch(branch_name)
        branch_id = str(branch["id"])
        all_arrs = await catalog.all_branch_arrangements()
        all_thers = await catalog.all_branch_therapists()
        print(f"Branch: {branch.get('name')} ({branch_id}) | rows: {len(rows)} | "
              f"mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
        print("Arrangements in branch: " + ", ".join(
            f"{a.get('name')} (cap={a.get('capacity')})" for a in all_arrs
        ))
        print(f"Therapists in branch: {len(all_thers)}")

        # 1. resolve every CSV service name up-front
        names = sorted({n for r in rows for n in r.services})
        resolved: dict[str, dict[str, Any] | None] = {n: catalog.resolve_service(n, overrides) for n in names}
        print("\nService mapping:")
        for n, s in resolved.items():
            print(f"  {n!r:45} -> " + (f"{s['name']} | {s.get('duration_minutes')} min | base {s.get('base_price')}"
                                       if s else "!! NOT FOUND !!"))
        missing = [n for n, s in resolved.items() if not s]
        if missing and not args.skip_unresolved:
            avail_svcs = [
                f"{s.get('name')} ({s.get('duration_minutes')} min, {s.get('base_price')})"
                for s in catalog.services
            ]
            print(f"\nAvailable services in ushauth ({len(avail_svcs)}):")
            for asvc in avail_svcs:
                print(f"  * {asvc}")
            try:
                addons = await ushauth._get_all_pages("/api/v1/addons/")
                if addons:
                    print(f"\nAvailable addons in ushauth ({len(addons)}):")
                    for a in addons:
                        print(f"  * {a.get('name')} ({a.get('duration_minutes')} min, {a.get('price')})")
                all_svcs = await ushauth._get("/api/v1/services/slim/", {"status": "all", "all": "true"})
                inactive = [s for s in all_svcs.get("data", []) if s.get("status") != "active"]
                if inactive:
                    print(f"\nInactive/draft services in ushauth ({len(inactive)}):")
                    for s in inactive:
                        print(f"  * {s.get('name')} [status={s.get('status')}] ({s.get('duration_minutes')} min, {s.get('base_price')})")
            except Exception:
                pass
            print(f"\nUnresolved services: {missing}\nFix with --service-map or use --skip-unresolved.")
            return 2

        # 2. process rows
        for row in rows:
            phone = normalise_phone(row.phone_raw, args.country_code)
            if len(row.services) != row.qty:
                print(f"  ! {row.ref}: Qty={row.qty} but {len(row.services)} services listed; using the list")

            # customer ---------------------------------------------------------
            customer: dict[str, Any] | None = None
            try:
                if args.execute:
                    if direct_db is not None:
                        customer = await direct_db.get_or_create_customer(
                            phone_number=phone, full_name=row.client, without_password=True
                        )
                    else:
                        customer = await get_or_create_customer(
                            phone_number=phone, full_name=row.client, without_password=True, settings=settings
                        )
                else:
                    found = await ushauth._get("/api/v1/customers/", {"search": phone.lstrip("+")})
                    hit = [c for c in found.get("data", [])
                           if (c.get("phone_number") or (c.get("user") or {}).get("phone_number")) == phone]
                    # dry-run: never create; report whether the customer would be created
                    customer = {
                        "id": str(hit[0]["id"]) if hit and hit[0].get("id") else str(uuid_placeholder(phone)),
                        "name": row.client,
                        "phone_number": phone,
                        "created": not hit,
                    }
            except Exception as exc:  # noqa: BLE001
                for i, sname in enumerate(row.services, 1):
                    results.append(Result(row.ref, i, sname, phone, row.client, "failed",
                                          reason=f"customer: {exc}"))
                continue

            start_local = datetime.combine(row.day, snap_down(row.start), tzinfo=KUWAIT_TZ)
            cust_data = {
                "id": str(customer["id"]),
                "first_name": (customer.get("name") or row.client).split(" ", 1)[0],
                "last_name": (customer.get("name") or row.client).split(" ", 1)[1]
                if " " in (customer.get("name") or row.client) else "",
                "phone_number": customer.get("phone_number") or phone,
                "email": customer.get("email") or "",
                "language_preference": customer.get("language_preference") or "ar",
            }

            for i, sname in enumerate(row.services, 1):
                res = Result(row.ref, i, sname, phone, row.client, "failed")
                results.append(res)
                svc = resolved.get(sname)
                if not svc:
                    res.reason = "service not resolved"
                    continue
                key = f"legacy-import:{row.ref}:{i}"

                duration = int(svc.get("duration_minutes") or 60)
                start = start_local
                end = start + timedelta(minutes=duration)
                res.start, res.end, res.service = start.isoformat(), end.isoformat(), svc["name"]
                slot = (start, end)
                if len(row.services) == 1:
                    csv_end = datetime.combine(row.day, snap_down(row.end), tzinfo=KUWAIT_TZ)
                    if csv_end != end:
                        res.reason = f"note: CSV end {csv_end:%H:%M} (snapped) != start+duration {end:%H:%M}"

                if args.execute:
                    async with factory() as s:  # type: ignore[misc]
                        existing = await BookingService(s, settings, ushauth)._repo.get_by_idempotency_key(key)
                    if existing:
                        res.status, res.booking_number = "skipped", existing.booking_number or ""
                        res.reason = "already imported"
                        continue

                is_addon = bool(svc.get("is_addon"))
                arrs = await catalog.arrangements(str(svc["id"]), is_addon=is_addon)
                pool = await catalog.therapists(str(svc["id"]), is_addon=is_addon)
                if not arrs or not pool:
                    res.reason = "no arrangement" if not arrs else "no therapist for service"
                    continue

                placed = False
                for relax_room_cap in (False, True):
                    if placed or (relax_room_cap and not arrs):
                        break
                    arr_iter = (
                        arrs
                        if not relax_room_cap
                        else sorted(arrs, key=lambda a: ledger.arrangement_load(str(a["id"]), slot))
                    )
                    for arr in arr_iter:
                        aid = str(arr["id"])
                        cap = int(arr.get("capacity") or 1)
                        if not relax_room_cap and not args.ignore_availability and ledger.arrangement_load(aid, slot) >= cap:
                            continue
                        cands = sorted(
                            (t for t in pool if not ledger.therapist_busy(str(t["id"]), slot)),
                            key=lambda t: ledger.minutes(str(t["id"])),
                        )
                        while cands:
                            if args.ignore_availability or relax_room_cap:
                                chosen = cands[0]
                            else:
                                avail = await ushauth.check_appointment_availability(
                                    service_arrangement_id=aid,
                                    appointment_date=start.strftime("%Y-%m-%d"),
                                    appointment_time=start.strftime("%H:%M:%S"),
                                    duration=duration,
                                    therapist_ids=[str(t["id"]) for t in cands],
                                )
                                if avail.get("available") != "yes":
                                    break
                                chosen = next((t for t in cands if str(t["id"]) == str(avail["therapist_id"])), None)
                                if chosen is None:
                                    break
                            tid = str(chosen["id"])
                            tname = clean_spaces(f"{chosen.get('first_name', '')} {chosen.get('last_name', '')}")
                            price = Decimal(str(arr["arrangement_price"] if arr.get("arrangement_price") is not None
                                                else svc.get("base_price") or "0"))
                            currency = arr.get("arrangement_currency") or svc.get("currency") or "KWD"
                            eff_loyalty_pts = int(arr.get("arrangement_loyalty_points")
                                                  or svc.get("loyalty_points") or 0)
                            eff_price_in_pts = int(arr.get("arrangement_price_in_points")
                                                   or svc.get("price_in_points") or 0)

                            if not args.execute:
                                ledger.add(tid, aid, slot)
                                res.status = "planned"
                                res.therapist, res.arrangement, res.price = tname, arr.get("name", ""), f"{price}"
                                placed = True
                                break

                            try:
                                async with factory() as s:  # type: ignore[misc]
                                    bsvc = BufferedBookingService(s, settings, ushauth)
                                    booking = await bsvc.create_booking(
                                        customer_id=_uuid(cust_data["id"]),
                                        customer_data=cust_data,
                                        branch_id=_uuid(branch_id),
                                        branch_data={"id": branch_id, "name": branch.get("name", ""),
                                                     "branch_name": branch.get("name", ""),
                                                     "address": branch.get("address_line1", "") or "",
                                                     "branch_address": branch.get("address_line1", "") or ""},
                                        service_id=_uuid(str(svc["id"])),
                                        service_data={
                                            "id": str(svc["id"]), "name": svc["name"],
                                            "base_price": str(svc.get("base_price") or "0"),
                                            "base_duration": duration,
                                            "is_eligible_for_loyalty": bool(svc.get("is_eligible_for_loyalty")),
                                            "loyalty_points": eff_loyalty_pts,
                                            "price_in_points": eff_price_in_pts,
                                        },
                                        service_arrangement_id=_uuid(aid),
                                        service_arrangement_data={
                                            "id": aid, "arrangement_type": arr.get("arrangement_type", ""),
                                            "room_name": arr.get("name", ""), "arrangement_name": arr.get("name", ""),
                                            "loyalty_points": eff_loyalty_pts,
                                            "price_in_points": eff_price_in_pts,
                                        },
                                        therapist_id=_uuid(tid),
                                        therapist_data={"id": tid, "name": tname, "therapist_name": tname},
                                        appointment_start=start,
                                        appointment_end=end,
                                        duration_minutes=duration,
                                        extra_minutes=0,
                                        total_duration=duration,
                                        addons_duration=0,
                                        base_price=Decimal(str(svc.get("base_price") or "0")),
                                        pricing=PricingBreakdown(
                                            arrangement_price=price, price_for_extra_minutes=Decimal("0"),
                                            addon_price=Decimal("0"), discount=Decimal("0"), tax=Decimal("0"),
                                            fees=Decimal("0"), total=price, currency=currency,
                                        ),
                                        addons=[],
                                        customer_notes=f"Imported from legacy appointment {row.ref}",
                                        idempotency_key=key,
                                        booking_type="branch_service",
                                        payment_type="service",
                                        status=BookingStatus.CONFIRMED.value,
                                        payment_status=PaymentStatus.PENDING.value,
                                        created_by_user="legacy-import",
                                        created_by_user_data={
                                            "id": "legacy-import", "role": "admin", "user_type": "admin",
                                            "legacy_user": row.legacy_user, "legacy_reference": row.ref,
                                            "legacy_status": row.legacy_status,
                                            "import_source": Path(args.csv).name,
                                        },
                                        source="ushdesk",
                                    )
                                    await s.commit()
                                events = bsvc.buffered_events
                            except DoubleBookingError:
                                ledger.therapist.setdefault(tid, []).append(slot)  # DB says busy
                                cands = [t for t in cands if str(t["id"]) != tid]
                                continue

                            if direct_db is not None:
                                await direct_db.sync_appointment_cache(
                                    booking_id=str(booking.id),
                                    service_arrangement_id=aid,
                                    service_arrangement_name=arr.get("name", ""),
                                    branch_id=branch_id,
                                    branch_name=branch.get("name", ""),
                                    therapist_id=tid,
                                    therapist_name=tname,
                                    customer_id=cust_data["id"],
                                    customer_name=row.client,
                                    service_id=str(svc["id"]),
                                    service_name=svc["name"],
                                    appointment_start=start,
                                    appointment_end=end,
                                    duration_minutes=duration,
                                    status=BookingStatus.CONFIRMED.value,
                                    payment_status=PaymentStatus.PENDING.value,
                                )

                            ledger.add(tid, aid, slot)
                            failed_events = 0
                            for ev in events:
                                out = await sqs.publish_event(ev)
                                if isinstance(out, dict) and "error" in out:
                                    failed_events += 1
                            res.status, res.booking_number = "created", booking.booking_number or ""
                            res.therapist, res.arrangement, res.price = tname, arr.get("name", ""), f"{price}"
                            if failed_events:
                                res.reason = f"{failed_events} SQS event(s) failed to publish (see logs)"
                            placed = True
                            break
                        if placed:
                            break

                if not placed:
                    res.reason = "no free therapist/arrangement for the slot (use --ignore-availability to force)"

        # 3. report
        report = Path(args.report or f"scripts/data/import_report_{datetime.now():%Y%m%d_%H%M%S}.csv")
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ref", "n", "client", "phone", "service", "start", "end", "therapist",
                        "arrangement", "price", "status", "booking_number", "reason"])
            for r in results:
                w.writerow([r.ref, r.index, r.client, r.phone, r.service, r.start, r.end, r.therapist,
                            r.arrangement, r.price, r.status, r.booking_number, r.reason])
        by_status: dict[str, int] = {}
        for r in results:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        total = sum(Decimal(r.price) for r in results if r.price)
        print(f"\nDone. {len(results)} booking lines: {by_status} | total value {total} KWD")
        print(f"Report: {report}")
        return 0 if not by_status.get("failed") else 3
    finally:
        await http.aclose()
        if direct_db is not None:
            await direct_db.aclose()
        if args.execute:
            await dispose_engine()


def _uuid(value: str):
    return uuid.UUID(str(value))


def uuid_placeholder(phone: str):
    """Deterministic fake id used only in dry-run output (never persisted)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dry-run:{phone}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv")
    p.add_argument("--execute", action="store_true", help="write to DB / publish SQS (default: dry-run)")
    p.add_argument("--branch-name")
    p.add_argument("--country-code", default="965")
    p.add_argument("--service-map")
    p.add_argument("--skip-unresolved", action="store_true")
    p.add_argument("--ignore-availability", action="store_true")
    p.add_argument("--ushauth-url", help="Override ushauth HTTP base URL")
    p.add_argument("--ushauth-db-url", help="Override ushauth PostgreSQL URL for direct DB fallback")
    p.add_argument("--limit", type=int)
    p.add_argument("--report")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
