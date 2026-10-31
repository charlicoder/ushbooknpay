#!/usr/bin/env python
"""
scripts/import_appointments_csv.py
──────────────────────────────────
Import a legacy "Appointment List" CSV export as bookings.

For every appointment row ONE booking is created (multi-service):
  * All service names in the row are resolved:
      - JACUZZI, SAUNA, EAR CANDLING, KASAT HWA (and aliases VENTUSA / VENTOSA)
        are treated as addons and placed in the booking's `addons` list.
      - Everything else is a regular service in the booking's `services` list.
  * If the SAME regular service name appears TWICE → couple/double room booking.
      - For "Japanese Detox" (or any service whose name contains "japanese"):
        two separate "Japanese Room" arrangements are used (one per therapist),
        both rows stored as primary (is_secondary_therapist=False).
        The branch is expected to have ≥ 2 Japanese Room arrangements.
      - For all other services: a room with capacity ≥ 2 (or whose name
        contains "couple"/"double") is REQUIRED; import fails if none exists
        (no fallback to single rooms).  Two therapists are assigned.
  * Customer is resolved from ushauth by phone (country code included,
    default +965).  Unknown phones are created with NO password.
  * Service, arrangement (room) and price come from ushauth:
        price = ArrangementService.price override, else Service.base_price
  * Start time is floored to the 30-minute slot grid (14:40 → 14:30).
    End = snapped start + sum of regular service durations.
  * The CSV has no therapist: a qualified therapist of the branch is picked
    that is free for the slot (ushauth availability + local + DB overlap).
  * Booking is created with status=confirmed, payment_status=pending.
    SQS events are SUPPRESSED (no SMS/WhatsApp/Email sent to customers).
  * `created_by_user` is set to the "User" column value from the CSV row.
  * Re-runs are idempotent: idempotency_key = legacy-import:<ref>.

Usage (from the ushbooknpay folder, with its .env loaded):

    # 1) dry-run (default): read-only calls to ushauth, nothing is written/sent
    python -m scripts.import_appointments_csv scripts/data/appointment_list.csv

    # 2) real import (creates customers, bookings; SQS events suppressed)
    python -m scripts.import_appointments_csv scripts/data/appointment_list.csv --execute

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
from collections import Counter
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

# Addon keyword matching: these service names are treated as addons, not services.
# After alias normalisation (ventusa/ventosa → kasat hwa) all names below apply.
ADDON_KEYWORDS: frozenset[str] = frozenset({
    "jacuzzi",
    "sauna",
    "ear candling",
    "kasat hwa",
    "ventusa",   # mapped → kasat hwa via _TYPO_MAP but also checked raw
    "ventosa",   # same
})


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
    """Floor a time to the 30-minute slot grid (14:40 → 14:30)."""
    return time(t.hour, (t.minute // SLOT_MINUTES) * SLOT_MINUTES)


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")[:2]
    return time(int(h), int(m))


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def english_part(service_cell_part: str) -> str:
    """'Stress Reles massage 60   ???? ...' → 'Stress Reles massage 60'."""
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
    """Discover ushauth's DATABASE_URL from CLI arg, env var, ../ushauth/.env, or app settings."""
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

    # In docker container or same host, derive from ushbooknpay's DATABASE_URL
    try:
        from app.core.config import get_settings
        st = get_settings()
        if getattr(st, "DATABASE_URL", None) and "ushbooknpay" in st.DATABASE_URL:
            derived = st.DATABASE_URL.replace("ushbooknpay", "ushauth")
            return _normalize_docker_hostname(derived)
    except Exception:
        pass

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
        service_arrangement_id: str | None,
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
        services_payload: list[dict] | None = None,
        secondary_therapist_id: str | None = None,
        secondary_therapist_name: str = "",
        _force_new_primary: bool = False,
    ) -> None:
        """
        Upsert the primary appointment-cache row, then reconcile multi-service
        rows and the secondary-therapist row (for couple/double rooms).

        When ``_force_new_primary=True`` the method always INSERTs a new primary
        row without first checking for an existing one (used for the second
        Japanese Room row in a dual Japanese Detox booking).

        Uses raw SQL so it works even when the ushauth HTTP container is not
        running (DirectUshAuthDB direct-DB path).
        """
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        appt_date_dt = appointment_start

        async with self._engine.begin() as conn:

            # ── 1. Upsert primary row ────────────────────────────────────────
            aid_val = str(service_arrangement_id) if service_arrangement_id else None
            brid_val = str(branch_id) if branch_id else None
            cid_val = str(customer_id) if customer_id else None
            sid_val = str(service_id) if service_id else None
            tid_val = str(therapist_id) if therapist_id else None

            _INSERT_PRIMARY = text(
                "INSERT INTO ush_spa_appointment_cache "
                "(id, servicearrangement_id, bookings_id, branch_id, branch_name, "
                "service_arrangement_name, therapist_id, therapist_name, customer_id, customer_name, "
                "service_id, service_name, booking_type, payment_status, appointment_date, "
                "appointment_start, appointment_end, duration_minutes, status, "
                "is_secondary_therapist, created_at, updated_at) "
                "VALUES ("
                "  CAST(:id AS uuid),"
                "  CAST(:aid AS uuid),"
                "  CAST(:bid AS uuid), CAST(:brid AS uuid), :brname,"
                "  :aname, CAST(:tid AS uuid), :tname, CAST(:cid AS uuid), :cname,"
                "  CAST(:sid AS uuid), :sname, 'branch', :pstatus, :adate,"
                "  :astart, :aend, :dur, :status,"
                "  FALSE, :now, :now"
                ")"
            )
            _INSERT_PARAMS = {
                "id": str(uuid.uuid4()),
                "aid": aid_val,
                "bid": str(booking_id),
                "brid": brid_val,
                "brname": branch_name or "",
                "aname": service_arrangement_name or "",
                "tid": tid_val,
                "tname": therapist_name or "",
                "cid": cid_val,
                "cname": customer_name or "",
                "sid": sid_val,
                "sname": service_name or "",
                "pstatus": payment_status,
                "adate": appt_date_dt,
                "astart": appointment_start,
                "aend": appointment_end,
                "dur": int(duration_minutes),
                "status": status,
                "now": now,
            }

            if _force_new_primary:
                # Always INSERT a fresh primary row (e.g. second Japanese Room therapist)
                await conn.execute(_INSERT_PRIMARY, _INSERT_PARAMS)
            else:
                existing_rows = await conn.execute(
                    text(
                        "SELECT id, servicearrangement_id FROM ush_spa_appointment_cache "
                        "WHERE bookings_id = CAST(:bid AS uuid) AND is_secondary_therapist = FALSE"
                    ),
                    {"bid": str(booking_id)},
                )
                primary_rows = list(existing_rows)

                if not primary_rows:
                    # INSERT the primary row
                    await conn.execute(_INSERT_PRIMARY, _INSERT_PARAMS)
                else:
                    # UPDATE existing primary row(s) — keep only the first, delete extras
                    first_id = str(primary_rows[0][0])
                    for extra in primary_rows[1:]:
                        await conn.execute(
                            text("DELETE FROM ush_spa_appointment_cache WHERE id = CAST(:rid AS uuid)"),
                            {"rid": str(extra[0])},
                        )
                    await conn.execute(
                        text(
                            "UPDATE ush_spa_appointment_cache SET "
                            "  servicearrangement_id = CAST(:aid AS uuid),"
                            "  branch_id = CAST(:brid AS uuid), branch_name = :brname,"
                            "  service_arrangement_name = :aname,"
                            "  therapist_id = CAST(:tid AS uuid), therapist_name = :tname,"
                            "  customer_id = CAST(:cid AS uuid), customer_name = :cname,"
                            "  service_id = CAST(:sid AS uuid), service_name = :sname,"
                            "  booking_type = 'branch', payment_status = :pstatus,"
                            "  appointment_date = :adate, appointment_start = :astart,"
                            "  appointment_end = :aend, duration_minutes = :dur,"
                            "  status = :status, updated_at = :now "
                            "WHERE id = CAST(:rid AS uuid)"
                        ),
                        {
                            "rid": first_id,
                            "aid": aid_val,
                            "brid": brid_val,
                            "brname": branch_name or "",
                            "aname": service_arrangement_name or "",
                            "tid": tid_val,
                            "tname": therapist_name or "",
                            "cid": cid_val,
                            "cname": customer_name or "",
                            "sid": sid_val,
                            "sname": service_name or "",
                            "pstatus": payment_status,
                            "adate": appt_date_dt,
                            "astart": appointment_start,
                            "aend": appointment_end,
                            "dur": int(duration_minutes),
                            "status": status,
                            "now": now,
                        },
                    )

            # ── 2. Multi-service extra rows (one per distinct arrangement) ───
            # For multi-service bookings the cache needs one row per arrangement.
            # We reconcile by deleting stale non-primary rows and creating new ones.
            if services_payload:
                # Collect distinct arrangements beyond the primary
                seen_arr: set[str] = set()
                if service_arrangement_id:
                    seen_arr.add(str(service_arrangement_id))
                extra_arrs: list[dict] = []
                for svc_entry in services_payload:
                    arr_id = svc_entry.get("service_arrangement_id")
                    svc_id_entry = svc_entry.get("service_id")
                    svc_name_entry = svc_entry.get("service_name", "")
                    arr_name_entry = svc_entry.get("service_arrangement_name", "")
                    if arr_id and str(arr_id) not in seen_arr:
                        seen_arr.add(str(arr_id))
                        extra_arrs.append({
                            "arr_id": str(arr_id),
                            "arr_name": arr_name_entry,
                            "svc_id": str(svc_id_entry) if svc_id_entry else str(service_id or ""),
                            "svc_name": svc_name_entry or service_name,
                        })

                # Delete extra non-secondary rows that are no longer needed
                existing_extra = await conn.execute(
                    text(
                        "SELECT id FROM ush_spa_appointment_cache "
                        "WHERE bookings_id = CAST(:bid AS uuid) "
                        "  AND is_secondary_therapist = FALSE "
                        "  AND servicearrangement_id IS NOT NULL "
                        "  AND (:prim_aid IS NULL OR servicearrangement_id != CAST(:prim_aid AS uuid))"
                    ),
                    {
                        "bid": str(booking_id),
                        "prim_aid": aid_val,
                    },
                )
                extra_existing_ids = [str(r[0]) for r in existing_extra]
                for eid in extra_existing_ids:
                    await conn.execute(
                        text("DELETE FROM ush_spa_appointment_cache WHERE id = CAST(:rid AS uuid)"),
                        {"rid": eid},
                    )

                # Insert fresh extra-arrangement rows
                for ea in extra_arrs:
                    await conn.execute(
                        text(
                            "INSERT INTO ush_spa_appointment_cache "
                            "(id, servicearrangement_id, bookings_id, branch_id, branch_name, "
                            "service_arrangement_name, therapist_id, therapist_name, customer_id, customer_name, "
                            "service_id, service_name, booking_type, payment_status, appointment_date, "
                            "appointment_start, appointment_end, duration_minutes, status, "
                            "is_secondary_therapist, created_at, updated_at) "
                            "VALUES ("
                            "  CAST(:id AS uuid),"
                            "  CAST(:aid AS uuid),"
                            "  CAST(:bid AS uuid), CAST(:brid AS uuid), :brname,"
                            "  :aname, CAST(:tid AS uuid), :tname, CAST(:cid AS uuid), :cname,"
                            "  CAST(:sid AS uuid), :sname, 'branch', :pstatus, :adate,"
                            "  :astart, :aend, :dur, :status,"
                            "  FALSE, :now, :now"
                            ")"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "aid": str(ea["arr_id"]) if ea.get("arr_id") else None,
                            "bid": str(booking_id),
                            "brid": brid_val,
                            "brname": branch_name or "",
                            "aname": ea.get("arr_name") or "",
                            "tid": tid_val,
                            "tname": therapist_name or "",
                            "cid": cid_val,
                            "cname": customer_name or "",
                            "sid": str(ea["svc_id"]) if ea.get("svc_id") else None,
                            "sname": ea.get("svc_name") or "",
                            "pstatus": payment_status,
                            "adate": appt_date_dt,
                            "astart": appointment_start,
                            "aend": appointment_end,
                            "dur": int(duration_minutes),
                            "status": status,
                            "now": now,
                        },
                    )

            # ── 3. Secondary-therapist row (couple/double rooms) ─────────────
            existing_secondary = await conn.execute(
                text(
                    "SELECT id FROM ush_spa_appointment_cache "
                    "WHERE bookings_id = CAST(:bid AS uuid) AND is_secondary_therapist = TRUE"
                ),
                {"bid": str(booking_id)},
            )
            secondary_rows = [str(r[0]) for r in existing_secondary]

            if secondary_therapist_id:
                sec_tid_val = str(secondary_therapist_id)
                if not secondary_rows:
                    await conn.execute(
                        text(
                            "INSERT INTO ush_spa_appointment_cache "
                            "(id, servicearrangement_id, bookings_id, branch_id, branch_name, "
                            "service_arrangement_name, therapist_id, therapist_name, customer_id, customer_name, "
                            "service_id, service_name, booking_type, payment_status, appointment_date, "
                            "appointment_start, appointment_end, duration_minutes, status, "
                            "is_secondary_therapist, created_at, updated_at) "
                            "VALUES ("
                            "  CAST(:id AS uuid), NULL,"
                            "  CAST(:bid AS uuid), CAST(:brid AS uuid), :brname,"
                            "  '', CAST(:tid AS uuid), :tname, CAST(:cid AS uuid), :cname,"
                            "  CAST(:sid AS uuid), :sname, 'branch', :pstatus, :adate,"
                            "  :astart, :aend, :dur, :status,"
                            "  TRUE, :now, :now"
                            ")"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "bid": str(booking_id),
                            "brid": brid_val,
                            "brname": branch_name or "",
                            "tid": sec_tid_val,
                            "tname": secondary_therapist_name or "",
                            "cid": cid_val,
                            "cname": customer_name or "",
                            "sid": sid_val,
                            "sname": service_name or "",
                            "pstatus": payment_status,
                            "adate": appt_date_dt,
                            "astart": appointment_start,
                            "aend": appointment_end,
                            "dur": int(duration_minutes),
                            "status": status,
                            "now": now,
                        },
                    )
                else:
                    # Update existing secondary row
                    await conn.execute(
                        text(
                            "UPDATE ush_spa_appointment_cache SET "
                            "  therapist_id = CAST(:tid AS uuid), therapist_name = :tname,"
                            "  branch_id = CAST(:brid AS uuid), branch_name = :brname,"
                            "  customer_id = CAST(:cid AS uuid), customer_name = :cname,"
                            "  service_id = CAST(:sid AS uuid), service_name = :sname,"
                            "  payment_status = :pstatus, appointment_date = :adate,"
                            "  appointment_start = :astart, appointment_end = :aend,"
                            "  duration_minutes = :dur, status = :status, updated_at = :now "
                            "WHERE id = CAST(:rid AS uuid)"
                        ),
                        {
                            "rid": secondary_rows[0],
                            "tid": sec_tid_val,
                            "tname": secondary_therapist_name or "",
                            "brid": brid_val,
                            "brname": branch_name or "",
                            "cid": cid_val,
                            "cname": customer_name or "",
                            "sid": sid_val,
                            "sname": service_name or "",
                            "pstatus": payment_status,
                            "adate": appt_date_dt,
                            "astart": appointment_start,
                            "aend": appointment_end,
                            "dur": int(duration_minutes),
                            "status": status,
                            "now": now,
                        },
                    )
                    # Remove any surplus secondary rows
                    for stale_id in secondary_rows[1:]:
                        await conn.execute(
                            text("DELETE FROM ush_spa_appointment_cache WHERE id = CAST(:rid AS uuid)"),
                            {"rid": stale_id},
                        )
            else:
                # No secondary therapist — clean up any leftover rows
                for stale_id in secondary_rows:
                    await conn.execute(
                        text("DELETE FROM ush_spa_appointment_cache WHERE id = CAST(:rid AS uuid)"),
                        {"rid": stale_id},
                    )

    async def create_appointment_cache(
        self,
        *,
        therapist_id: uuid.UUID | str,
        booking_id: uuid.UUID | str,
        appointment_date: date | str,
        appointment_time: time | str,
        service_arrangement_id: uuid.UUID | str | None = None,
        branch_id: uuid.UUID | str | None = None,
        branch_name: str | None = None,
        customer_id: uuid.UUID | str | None = None,
        customer_name: str | None = None,
        service_id: uuid.UUID | str | None = None,
        service_name: str | None = None,
        service_arrangement_name: str | None = None,
        therapist_name: str | None = None,
        booking_type: str = "branch",
        payment_status: str = "unpaid",
        duration: int = 60,
        status: str = "confirmed",
        services: list[dict[str, Any]] | None = None,
        secondary_therapist_id: uuid.UUID | str | None = None,
        secondary_therapist_name: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Direct DB implementation of create_appointment_cache matching USHAuthClient signature."""
        if isinstance(appointment_date, str):
            d = datetime.strptime(appointment_date, "%Y-%m-%d").date()
        else:
            d = appointment_date

        if isinstance(appointment_time, str):
            t_parts = [int(x) for x in appointment_time.split(":")]
            t = time(t_parts[0], t_parts[1], t_parts[2] if len(t_parts) > 2 else 0)
        else:
            t = appointment_time

        start_dt = datetime.combine(d, t, tzinfo=KUWAIT_TZ)
        end_dt = start_dt + timedelta(minutes=int(duration))

        await self.sync_appointment_cache(
            booking_id=str(booking_id),
            service_arrangement_id=str(service_arrangement_id) if service_arrangement_id else None,
            service_arrangement_name=service_arrangement_name or "",
            branch_id=str(branch_id) if branch_id else "",
            branch_name=branch_name or "",
            therapist_id=str(therapist_id),
            therapist_name=therapist_name or "",
            customer_id=str(customer_id) if customer_id else "",
            customer_name=customer_name or "",
            service_id=str(service_id) if service_id else "",
            service_name=service_name or "",
            appointment_start=start_dt,
            appointment_end=end_dt,
            duration_minutes=int(duration),
            status=status,
            payment_status=payment_status,
            services_payload=services,
            secondary_therapist_id=str(secondary_therapist_id) if secondary_therapist_id else None,
            secondary_therapist_name=secondary_therapist_name or "",
        )
        return {"success": True, "booking_id": str(booking_id)}


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


def _is_addon_by_name(csv_name: str) -> bool:
    """Return True if the CSV service name is a known addon keyword."""
    alias = _norm_service_alias(csv_name)
    raw = _norm(english_part(csv_name))
    for kw in ADDON_KEYWORDS:
        if kw in alias or kw in raw or alias in kw:
            return True
    return False


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
    service: str        # comma-joined resolved service names
    phone: str
    client: str
    status: str         # created | planned | skipped | failed
    booking_number: str = ""
    start: str = ""
    end: str = ""
    therapist: str = ""
    secondary_therapist: str = ""
    arrangement: str = ""
    price: str = ""
    is_couple: bool = False
    addons: str = ""    # comma-joined addon names
    reason: str = ""


async def sync_appointment_cache_entry(
    *,
    ushauth: Any,
    direct_db: DirectUshAuthDB | None,
    settings: Any,
    booking_id: str | uuid.UUID,
    service_arrangement_id: str | uuid.UUID | None,
    service_arrangement_name: str,
    branch_id: str | uuid.UUID | None,
    branch_name: str,
    therapist_id: str | uuid.UUID,
    therapist_name: str,
    customer_id: str | uuid.UUID | None,
    customer_name: str,
    service_id: str | uuid.UUID | None,
    service_name: str,
    appointment_start: datetime,
    appointment_end: datetime,
    duration_minutes: int,
    status: str = "confirmed",
    payment_status: str = "pending",
    services_payload: list[dict[str, Any]] | None = None,
    secondary_therapist_id: str | uuid.UUID | None = None,
    secondary_therapist_name: str = "",
) -> None:
    """
    Ensure the appointment cache record(s) exist in ushauth for a booking.

    Attempts ushauth.create_appointment_cache() via HTTP (using proper
    inter-service authentication headers), falling back to DirectUshAuthDB
    direct database queries if HTTP is unreachable or fails.
    """
    clean_services = None
    if services_payload and len(services_payload) > 1:
        clean_services = []
        for s in services_payload:
            if not isinstance(s, dict):
                continue
            s_svc_id = s.get("service_id")
            s_arr_id = s.get("service_arrangement_id")
            clean_services.append({
                "service_id": str(s_svc_id) if s_svc_id else None,
                "service_name": str(s.get("service_name") or (s.get("service_data") or {}).get("name") or "") or "",
                "service_arrangement_id": str(s_arr_id) if s_arr_id else None,
                "service_arrangement_name": str(s.get("service_arrangement_name") or (s.get("service_arrangement_data") or {}).get("arrangement_name") or "") or "",
                "duration_minutes": int(s.get("duration_minutes") or 0),
            })

    appt_date_str = appointment_start.strftime("%Y-%m-%d")
    appt_time_str = appointment_start.strftime("%H:%M:%S")
    sec_tid_str = str(secondary_therapist_id) if secondary_therapist_id else None
    sec_tname_str = secondary_therapist_name if sec_tid_str else None

    # 1. If direct_db is already active, write directly to PostgreSQL
    if direct_db is not None:
        await direct_db.sync_appointment_cache(
            booking_id=str(booking_id),
            service_arrangement_id=str(service_arrangement_id) if service_arrangement_id else None,
            service_arrangement_name=service_arrangement_name or "",
            branch_id=str(branch_id) if branch_id else "",
            branch_name=branch_name or "",
            therapist_id=str(therapist_id),
            therapist_name=therapist_name or "",
            customer_id=str(customer_id) if customer_id else "",
            customer_name=customer_name or "",
            service_id=str(service_id) if service_id else "",
            service_name=service_name or "",
            appointment_start=appointment_start,
            appointment_end=appointment_end,
            duration_minutes=int(duration_minutes),
            status=status,
            payment_status=payment_status,
            services_payload=clean_services,
            secondary_therapist_id=sec_tid_str,
            secondary_therapist_name=sec_tname_str or "",
        )
        return

    # 2. Try HTTP API with USHAuthClient (includes authentication headers)
    http_exc: Exception | None = None
    if hasattr(ushauth, "create_appointment_cache"):
        try:
            await ushauth.create_appointment_cache(
                therapist_id=str(therapist_id),
                booking_id=str(booking_id),
                appointment_date=appt_date_str,
                appointment_time=appt_time_str,
                service_arrangement_id=str(service_arrangement_id) if service_arrangement_id else None,
                branch_id=str(branch_id) if branch_id else None,
                branch_name=branch_name or None,
                customer_id=str(customer_id) if customer_id else None,
                customer_name=customer_name or None,
                service_id=str(service_id) if service_id else None,
                service_name=service_name or None,
                service_arrangement_name=service_arrangement_name or None,
                therapist_name=therapist_name or None,
                booking_type="branch",
                payment_status=payment_status,
                duration=int(duration_minutes),
                status=status,
                services=clean_services,
                secondary_therapist_id=sec_tid_str,
                secondary_therapist_name=sec_tname_str,
            )
            return
        except Exception as exc:
            http_exc = exc
            print(f"  [cache] ushauth HTTP create_appointment_cache ({exc}); falling back to direct DB...")

    # 3. Fallback to direct DB if HTTP failed or unavailable
    db_url = _discover_ushauth_db_url()
    if db_url:
        temp_db = DirectUshAuthDB(db_url)
        try:
            await temp_db.sync_appointment_cache(
                booking_id=str(booking_id),
                service_arrangement_id=str(service_arrangement_id) if service_arrangement_id else None,
                service_arrangement_name=service_arrangement_name or "",
                branch_id=str(branch_id) if branch_id else "",
                branch_name=branch_name or "",
                therapist_id=str(therapist_id),
                therapist_name=therapist_name or "",
                customer_id=str(customer_id) if customer_id else "",
                customer_name=customer_name or "",
                service_id=str(service_id) if service_id else "",
                service_name=service_name or "",
                appointment_start=appointment_start,
                appointment_end=appointment_end,
                duration_minutes=int(duration_minutes),
                status=status,
                payment_status=payment_status,
                services_payload=clean_services,
                secondary_therapist_id=sec_tid_str,
                secondary_therapist_name=sec_tname_str or "",
            )
            return
        finally:
            await temp_db.aclose()

    if http_exc:
        raise http_exc
    raise RuntimeError("Could not sync appointment_cache: HTTP failed and no direct DB fallback available.")


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
            # Non-connection HTTP response (or auth error) means server is reachable at this URL
            return client, None

    db_url = _discover_ushauth_db_url(ushauth_db_url)
    if db_url:
        print(f"[info] ushauth HTTP API unreachable ({ candidate_urls[0] }); using direct ushauth DB fallback.")
        direct_db = DirectUshAuthDB(db_url)
        return direct_db, direct_db

    if last_exc:
        raise last_exc
    raise RuntimeError("Unable to connect to ushauth via HTTP or database.")


# ── helpers ───────────────────────────────────────────────────────────────────

def _tname(t: dict[str, Any]) -> str:
    return clean_spaces(f"{t.get('first_name', '')} {t.get('last_name', '')}")


def _is_japanese_service(csv_name: str, svc_obj: dict[str, Any] | None = None) -> bool:
    """Return True if this service is 'Japanese Detox' (or similar).

    When the same Japanese service appears twice in a CSV row, two *separate*
    Japanese Room arrangements are used (one per therapist, each
    is_secondary_therapist=False), NOT a single couple/double room.
    """
    combined = " ".join([
        csv_name,
        (svc_obj or {}).get("name") or "",
        (svc_obj or {}).get("name_en") or "",
    ])
    return "japanese" in _norm(combined)


async def _pick_two_therapists(
    pool: list[dict[str, Any]],
    slot: tuple[datetime, datetime],
    ledger: Ledger,
    *,
    ushauth: Any,
    aid: str,
    start: datetime,
    duration: int,
    ignore_availability: bool,
    relax: bool,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Pick two DIFFERENT therapists from pool both free for the slot.
    Returns (primary, secondary) or None if not enough free therapists.
    """
    if relax or ignore_availability:
        cands = sorted(pool, key=lambda t: ledger.minutes(str(t["id"])))
    else:
        cands = sorted(
            (t for t in pool if not ledger.therapist_busy(str(t["id"]), slot)),
            key=lambda t: ledger.minutes(str(t["id"])),
        )

    if len(cands) < 2:
        return None

    if ignore_availability or relax:
        return cands[0], cands[1]

    # Check DB availability for pairs
    for i, t1 in enumerate(cands):
        avail1 = await ushauth.check_appointment_availability(
            service_arrangement_id=aid,
            appointment_date=start.strftime("%Y-%m-%d"),
            appointment_time=start.strftime("%H:%M:%S"),
            duration=duration,
            therapist_ids=[str(t1["id"])],
        )
        if avail1.get("available") != "yes":
            continue
        for t2 in cands[i + 1:]:
            avail2 = await ushauth.check_appointment_availability(
                service_arrangement_id=aid,
                appointment_date=start.strftime("%Y-%m-%d"),
                appointment_time=start.strftime("%H:%M:%S"),
                duration=duration,
                therapist_ids=[str(t2["id"])],
            )
            if avail2.get("available") == "yes":
                return t1, t2
    return None


async def _pick_japanese_dual_therapists(
    pool: list[dict[str, Any]],
    japanese_arrs: list[dict[str, Any]],
    slot: tuple[datetime, datetime],
    ledger: Ledger,
    *,
    ushauth: Any,
    start: datetime,
    duration: int,
    ignore_availability: bool,
    relax: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """Pick 2 therapists + 2 Japanese Rooms for a dual Japanese Detox booking.

    Each therapist gets their *own* separate Japanese Room arrangement.
    Returns (therapist1, therapist2, arr1, arr2) or None if not enough
    rooms/therapists are available.

    appointment_cache rows for this booking will both have
    is_secondary_therapist=False (unlike a couple room where therapist2
    carries is_secondary_therapist=True).
    """
    # Find ≥2 Japanese Room arrangements with capacity still available
    free_arrs: list[dict[str, Any]] = []
    for a in japanese_arrs:
        cap = int(a.get("capacity") or 1)
        current_load = ledger.arrangement_load(str(a["id"]), slot)
        if relax or ignore_availability or current_load < cap:
            free_arrs.append(a)
        if len(free_arrs) >= 2:
            break

    if len(free_arrs) < 2:
        if relax:
            # Force-use first two available (even if at capacity)
            free_arrs = japanese_arrs[:2]
        if len(free_arrs) < 2:
            return None

    arr1, arr2 = free_arrs[0], free_arrs[1]

    # Pick two free therapists
    if relax or ignore_availability:
        cands = sorted(pool, key=lambda t: ledger.minutes(str(t["id"])))
    else:
        cands = sorted(
            (t for t in pool if not ledger.therapist_busy(str(t["id"]), slot)),
            key=lambda t: ledger.minutes(str(t["id"])),
        )

    if len(cands) < 2:
        return None

    if ignore_availability or relax:
        return cands[0], cands[1], arr1, arr2

    # Verify ushauth availability for each therapist against their respective room
    chosen1: dict[str, Any] | None = None
    chosen2: dict[str, Any] | None = None
    for i, t1 in enumerate(cands):
        avail1 = await ushauth.check_appointment_availability(
            service_arrangement_id=str(arr1["id"]),
            appointment_date=start.strftime("%Y-%m-%d"),
            appointment_time=start.strftime("%H:%M:%S"),
            duration=duration,
            therapist_ids=[str(t1["id"])],
        )
        if avail1.get("available") != "yes":
            continue
        chosen1 = t1
        for t2 in cands[i + 1:]:
            avail2 = await ushauth.check_appointment_availability(
                service_arrangement_id=str(arr2["id"]),
                appointment_date=start.strftime("%Y-%m-%d"),
                appointment_time=start.strftime("%H:%M:%S"),
                duration=duration,
                therapist_ids=[str(t2["id"])],
            )
            if avail2.get("available") == "yes":
                chosen2 = t2
                break
        if chosen1 and chosen2:
            return chosen1, chosen2, arr1, arr2
    return None


# ── main import logic ─────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    # imports that need the app settings/.env are done lazily
    from app.booking.application.services import BookingService
    from app.booking.domain.services_snapshot import build_service_entry
    from app.booking.domain.value_objects import BookingStatus, PaymentStatus, PricingBreakdown
    from app.clients.ushauth import get_or_create_customer
    from app.core.config import get_settings
    from app.core.database import dispose_engine, get_session_factory
    from app.core.exceptions import DoubleBookingError
    from app.events.sqs_client import get_sqs_client

    class BufferedBookingService(BookingService):
        """Collect events; the script intentionally does NOT publish them (no notifications)."""

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
    sqs = get_sqs_client()  # noqa: F841  (kept for future; events are suppressed below)
    factory = get_session_factory() if args.execute else None

    try:
        ushauth, direct_db = await _connect_ushauth(
            http=http,
            settings=settings,
            ushauth_url=args.ushauth_url,
            ushauth_db_url=args.ushauth_db_url,
        )
        # Capture the resolved base URL so we can call create-appointment-cache
        # even when ushauth is reachable via HTTP (not direct DB).
        _resolved_ushauth_url: str = (
            getattr(settings, "USHAUTH_DIRECT_URL", None)
            or getattr(settings, "ushauth_base_url", None)
            or args.ushauth_url
            or "http://localhost:8002"
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
            tag = " [ADDON]" if (s and s.get("is_addon")) or _is_addon_by_name(n) else ""
            print(f"  {n!r:45} -> " + (f"{s['name']}{tag} | {s.get('duration_minutes')} min | base {s.get('base_price')}"
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

        # 2. process rows — ONE booking per CSV row
        for row_idx, row in enumerate(rows, 1):
            phone = normalise_phone(row.phone_raw, args.country_code)
            print(f"\n[{row_idx}/{len(rows)}] {row.ref}  {row.day} {row.start}-{row.end}  "
                  f"client={row.client!r}  services={row.services!r}")

            # ── split into real services vs addons ─────────────────────────
            real_svcs: list[tuple[str, dict[str, Any]]] = []   # (csv_name, svc_obj)
            addon_svcs: list[tuple[str, dict[str, Any]]] = []  # (csv_name, svc_obj)
            skipped_names: list[str] = []

            for sname in row.services:
                svc = resolved.get(sname)
                if not svc:
                    if args.skip_unresolved:
                        skipped_names.append(sname)
                        continue
                    # Can't proceed without at least one resolvable service
                    continue
                # Mark as addon if DB flag set OR name matches addon keywords
                if svc.get("is_addon") or _is_addon_by_name(sname):
                    addon_svcs.append((sname, svc))
                else:
                    real_svcs.append((sname, svc))

            if not real_svcs and not addon_svcs:
                results.append(Result(row.ref, 1, ", ".join(row.services), phone, row.client, "failed",
                                      reason="no services resolved"))
                continue

            # If ALL resolved are addons and nothing is a regular service, treat them as services
            # (edge case: booking with only addon-type services)
            if not real_svcs and addon_svcs:
                real_svcs = addon_svcs
                addon_svcs = []

            # ── detect couple room: same service name appears ≥ 2 times ──
            real_name_counts = Counter(sname for sname, _ in real_svcs)
            is_couple = any(cnt >= 2 for cnt in real_name_counts.values())

            # Deduplicate real_svcs for couple room (keep one entry per unique service)
            if is_couple:
                seen_real: set[str] = set()
                deduped_real: list[tuple[str, dict[str, Any]]] = []
                for sname, svc in real_svcs:
                    if sname not in seen_real:
                        seen_real.add(sname)
                        deduped_real.append((sname, svc))
                real_svcs = deduped_real

            primary_svc = real_svcs[0][1]  # first real service drives arrangement/therapist pool

            # ── compute total duration for slot ────────────────────────────
            total_dur = sum(int(svc.get("duration_minutes") or 60) for _, svc in real_svcs)
            addon_dur = sum(int(svc.get("duration_minutes") or 15) for _, svc in addon_svcs)

            start_local = datetime.combine(row.day, snap_down(row.start), tzinfo=KUWAIT_TZ)
            end_local = start_local + timedelta(minutes=total_dur)
            slot = (start_local, end_local)

            idempotency_key = f"legacy-import:{row.ref}"

            # ── result placeholder ─────────────────────────────────────────
            all_svc_names = ", ".join(svc.get("name", sname) for sname, svc in real_svcs)
            all_addon_names = ", ".join(svc.get("name", sname) for sname, svc in addon_svcs)
            res = Result(
                ref=row.ref,
                index=row_idx,
                service=all_svc_names,
                phone=phone,
                client=row.client,
                status="failed",
                is_couple=is_couple,
                addons=all_addon_names,
                start=start_local.isoformat(),
                end=end_local.isoformat(),
            )
            results.append(res)

            # ── customer lookup/creation ───────────────────────────────────
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
                    customer = {
                        "id": str(hit[0]["id"]) if hit and hit[0].get("id") else str(uuid_placeholder(phone)),
                        "name": row.client,
                        "phone_number": phone,
                        "created": not hit,
                    }
            except Exception as exc:  # noqa: BLE001
                res.reason = f"customer: {exc}"
                continue

            cust_data = {
                "id": str(customer["id"]),
                "first_name": (customer.get("name") or row.client).split(" ", 1)[0],
                "last_name": (customer.get("name") or row.client).split(" ", 1)[1]
                if " " in (customer.get("name") or row.client) else "",
                "phone_number": customer.get("phone_number") or phone,
                "email": customer.get("email") or "",
                "language_preference": customer.get("language_preference") or "ar",
            }

            # ── check idempotency (execute mode) ───────────────────────────
            if args.execute:
                async with factory() as s:  # type: ignore[misc]
                    existing = await BookingService(s, settings, ushauth)._repo.get_by_idempotency_key(idempotency_key)
                if existing:
                    res.booking_number = existing.booking_number or ""
                    # Ensure appointment cache is created/synced for this existing booking
                    try:
                        ex_services = getattr(existing, "services", None) or []
                        ex_cache_services = [
                            {
                                "service_id": str(s.get("service_id") or "") or None,
                                "service_name": str(s.get("service_name") or (s.get("service_data") or {}).get("name") or "") or None,
                                "service_arrangement_id": str(s.get("service_arrangement_id") or "") or None,
                                "service_arrangement_name": str(s.get("service_arrangement_name") or (s.get("service_arrangement_data") or {}).get("arrangement_name") or "") or None,
                                "duration_minutes": int(s.get("duration_minutes") or 0),
                            }
                            for s in ex_services
                            if isinstance(s, dict)
                        ] if len(ex_services) > 1 else None

                        ex_tdata = existing.therapist_data or {}
                        ex_tname = ex_tdata.get("name") or ex_tdata.get("therapist_name") or ""
                        ex_sec_tdata = existing.secondary_therapist_data or {}
                        ex_sec_tname = ex_sec_tdata.get("name") or ex_sec_tdata.get("therapist_name") or ""
                        ex_adata = existing.service_arrangement_data or {}
                        ex_aname = ex_adata.get("room_name") or ex_adata.get("arrangement_name") or ""
                        ex_sdata = existing.service_data or {}
                        ex_sname = ex_sdata.get("name") or ""
                        ex_cdata = existing.customer_data or {}
                        ex_cname = ex_cdata.get("name") or row.client
                        ex_bdata = existing.branch_data or {}
                        ex_brname = ex_bdata.get("name") or branch.get("name", "")

                        await sync_appointment_cache_entry(
                            ushauth=ushauth,
                            direct_db=direct_db,
                            settings=settings,
                            booking_id=existing.id,
                            service_arrangement_id=existing.service_arrangement_id,
                            service_arrangement_name=ex_aname,
                            branch_id=existing.branch_id,
                            branch_name=ex_brname,
                            therapist_id=existing.therapist_id,
                            therapist_name=ex_tname,
                            customer_id=existing.customer_id,
                            customer_name=ex_cname,
                            service_id=existing.service_id,
                            service_name=ex_sname,
                            appointment_start=existing.appointment_start,
                            appointment_end=existing.appointment_end,
                            duration_minutes=existing.duration_minutes or total_dur,
                            status="confirmed",
                            payment_status="pending",
                            services_payload=ex_cache_services,
                            secondary_therapist_id=existing.secondary_therapist_id,
                            secondary_therapist_name=ex_sec_tname,
                        )
                        res.status = "skipped"
                        res.reason = "already imported (appointment_cache synced)"
                        print(f"  [cache] appointment_cache synced for existing booking {existing.booking_number} ({row.ref})")
                    except Exception as cache_err:
                        res.status = "skipped"
                        res.reason = f"already imported (cache sync error: {cache_err})"
                        print(f"  [cache] ERROR syncing appointment_cache for {existing.booking_number}: {cache_err}")
                    continue

            # ── find arrangement ───────────────────────────────────────────
            arrs = await catalog.arrangements(str(primary_svc["id"]), is_addon=False)
            pool = await catalog.therapists(str(primary_svc["id"]), is_addon=False)

            if not arrs or not pool:
                res.reason = "no arrangement" if not arrs else "no therapist for service"
                continue

            # ── detect booking pattern for duplicate services ───────────────
            # is_japanese_couple: same Japanese Detox service × 2 → use 2
            # separate Japanese Rooms (one per therapist, NOT a couple room).
            # is_couple (non-japanese): must use a room with capacity ≥ 2.
            is_japanese_couple = is_couple and _is_japanese_service(
                real_svcs[0][0], real_svcs[0][1]
            )

            if is_japanese_couple:
                # Japanese Detox × 2: collect all "Japanese Room" arrangements
                japanese_arrs = [
                    a for a in arrs
                    if "japanese" in _norm(a.get("name", ""))
                ]
                if not japanese_arrs:
                    res.reason = (
                        "no Japanese Room arrangement found for Japanese Detox dual booking "
                        "(need ≥ 2 arrangements whose name contains 'japanese')"
                    )
                    continue
                # We need at least 2 Japanese Rooms
                if len(japanese_arrs) < 2 and not args.ignore_availability:
                    res.reason = (
                        f"only {len(japanese_arrs)} Japanese Room arrangement(s) found; "
                        "need ≥ 2 for dual Japanese Detox booking"
                    )
                    continue
                arrs_to_try = japanese_arrs          # used for ledger / dry-run only
                japanese_mode = True
            elif is_couple:
                # Standard couple room: MUST use arrangement with capacity ≥ 2
                # (or name contains "couple"/"double"). No fallback to single rooms.
                couple_arrs = [
                    a for a in arrs
                    if int(a.get("capacity") or 1) >= 2
                    or "couple" in _norm(a.get("name", ""))
                    or "double" in _norm(a.get("name", ""))
                ]
                if not couple_arrs:
                    res.reason = (
                        "no couple/double room available for duplicate-service booking "
                        "(need arrangement with capacity≥2 or name containing 'couple'/'double')"
                    )
                    continue
                arrs_to_try = couple_arrs
                japanese_mode = False
            else:
                arrs_to_try = arrs
                japanese_mode = False

            placed = False
            for relax_room_cap in (False, True):
                if placed:
                    break

                # ── Japanese dual-room path ────────────────────────────────
                if japanese_mode:
                    result = await _pick_japanese_dual_therapists(
                        pool, arrs_to_try, slot, ledger,
                        ushauth=ushauth,
                        start=start_local,
                        duration=total_dur,
                        ignore_availability=args.ignore_availability,
                        relax=relax_room_cap,
                    )
                    if result is None:
                        continue  # try relaxed next iteration
                    chosen1, chosen2, arr1_j, arr2_j = result
                    tid1, tname1 = str(chosen1["id"]), _tname(chosen1)
                    tid2, tname2 = str(chosen2["id"]), _tname(chosen2)
                    # Use the first Japanese Room as the "primary" arrangement for
                    # the booking record; the second room is captured in cache only.
                    arr = arr1_j
                    aid = str(arr1_j["id"])
                    aid2_j = str(arr2_j["id"])   # second Japanese Room (cache only)

                    # ── pricing ────────────────────────────────────────────
                    primary_price = Decimal(str(
                        arr["arrangement_price"] if arr.get("arrangement_price") is not None
                        else primary_svc.get("base_price") or "0"
                    ))
                    total_price = Decimal("0")
                    for _, svc_i in real_svcs:
                        svc_arrs_i = await catalog.arrangements(str(svc_i["id"]), is_addon=False)
                        matched_arr = next(
                            (a for a in svc_arrs_i if str(a["id"]) == aid),
                            svc_arrs_i[0] if svc_arrs_i else None,
                        )
                        p = Decimal(str(
                            matched_arr.get("arrangement_price") or svc_i.get("base_price") or "0"
                        )) if matched_arr else Decimal(str(svc_i.get("base_price") or "0"))
                        total_price += p
                    addon_price = sum(
                        Decimal(str(svc.get("base_price") or "0")) for _, svc in addon_svcs
                    )
                    grand_total = total_price + addon_price
                    currency = arr.get("arrangement_currency") or primary_svc.get("currency") or "KWD"
                    eff_loyalty_pts = int(arr.get("arrangement_loyalty_points") or primary_svc.get("loyalty_points") or 0)
                    eff_price_in_pts = int(arr.get("arrangement_price_in_points") or primary_svc.get("price_in_points") or 0)

                    # ── build services payload ─────────────────────────────
                    services_payload: list[dict] = []
                    for pos_i, (sname_i, svc_i) in enumerate(real_svcs):
                        svc_arrs_i = await catalog.arrangements(str(svc_i["id"]), is_addon=False)
                        matched_arr_i = next(
                            (a for a in svc_arrs_i if str(a["id"]) == aid),
                            svc_arrs_i[0] if svc_arrs_i else arr,
                        )
                        arr_price_i = Decimal(str(
                            matched_arr_i.get("arrangement_price") or svc_i.get("base_price") or "0"
                        ))
                        services_payload.append(
                            build_service_entry(
                                service_id=uuid.UUID(str(svc_i["id"])),
                                service_data={
                                    "id": str(svc_i["id"]),
                                    "name": svc_i["name"],
                                    "base_price": str(svc_i.get("base_price") or "0"),
                                    "base_duration": int(svc_i.get("duration_minutes") or 60),
                                    "is_eligible_for_loyalty": bool(svc_i.get("is_eligible_for_loyalty")),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                service_arrangement_id=uuid.UUID(str(matched_arr_i["id"])),
                                service_arrangement_data={
                                    "id": str(matched_arr_i["id"]),
                                    "arrangement_type": matched_arr_i.get("arrangement_type", ""),
                                    "room_name": matched_arr_i.get("name", ""),
                                    "arrangement_name": matched_arr_i.get("name", ""),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                base_price=arr_price_i,
                                duration_minutes=int(svc_i.get("duration_minutes") or 60),
                                position=pos_i,
                            )
                        )

                    # ── build addons payload ───────────────────────────────
                    addons_payload: list[dict] = [
                        {
                            "id": str(svc.get("id")),
                            "name": svc.get("name", sname),
                            "price": str(svc.get("base_price") or "0"),
                            "duration_minutes": int(svc.get("duration_minutes") or 15),
                        }
                        for sname, svc in addon_svcs
                    ]

                    if not args.execute:
                        ledger.add(tid1, aid, slot)
                        ledger.add(tid2, aid2_j, slot)
                        res.status = "planned"
                        res.therapist = tname1
                        res.secondary_therapist = tname2
                        res.arrangement = f"{arr1_j.get('name', '')} / {arr2_j.get('name', '')}"
                        res.price = str(grand_total)
                        placed = True
                        break

                    # Execute: create booking (secondary therapist recorded on booking)
                    try:
                        async with factory() as s:  # type: ignore[misc]
                            bsvc = BufferedBookingService(s, settings, ushauth)
                            booking = await bsvc.create_booking(
                                customer_id=_uuid(cust_data["id"]),
                                customer_data=cust_data,
                                branch_id=_uuid(branch_id),
                                branch_data={
                                    "id": branch_id,
                                    "name": branch.get("name", ""),
                                    "branch_name": branch.get("name", ""),
                                    "address": branch.get("address_line1", "") or "",
                                    "branch_address": branch.get("address_line1", "") or "",
                                },
                                services=services_payload,
                                service_id=_uuid(str(primary_svc["id"])),
                                service_data={
                                    "id": str(primary_svc["id"]),
                                    "name": primary_svc["name"],
                                    "base_price": str(primary_svc.get("base_price") or "0"),
                                    "base_duration": int(primary_svc.get("duration_minutes") or 60),
                                    "is_eligible_for_loyalty": bool(primary_svc.get("is_eligible_for_loyalty")),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                service_arrangement_id=_uuid(aid),
                                service_arrangement_data={
                                    "id": aid,
                                    "arrangement_type": arr.get("arrangement_type", ""),
                                    "room_name": arr.get("name", ""),
                                    "arrangement_name": arr.get("name", ""),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                therapist_id=_uuid(tid1),
                                therapist_data={"id": tid1, "name": tname1, "therapist_name": tname1},
                                secondary_therapist_id=_uuid(tid2),
                                secondary_therapist_data={"id": tid2, "name": tname2, "therapist_name": tname2},
                                appointment_start=start_local,
                                appointment_end=end_local,
                                duration_minutes=int(primary_svc.get("duration_minutes") or 60),
                                extra_minutes=0,
                                total_duration=total_dur + addon_dur,
                                addons_duration=addon_dur,
                                base_price=Decimal(str(primary_svc.get("base_price") or "0")),
                                pricing=PricingBreakdown(
                                    arrangement_price=total_price,
                                    price_for_extra_minutes=Decimal("0"),
                                    addon_price=addon_price,
                                    discount=Decimal("0"),
                                    tax=Decimal("0"),
                                    fees=Decimal("0"),
                                    total=grand_total,
                                    currency=currency,
                                ),
                                addons=addons_payload,
                                customer_notes=f"Imported from legacy appointment {row.ref}",
                                idempotency_key=idempotency_key,
                                booking_type="branch_service",
                                payment_type="service",
                                status=BookingStatus.CONFIRMED.value,
                                payment_status=PaymentStatus.PENDING.value,
                                created_by_user=row.legacy_user or "legacy-import",
                                created_by_user_data={
                                    "id": row.legacy_user or "legacy-import",
                                    "role": "admin",
                                    "user_type": "admin",
                                    "legacy_user": row.legacy_user,
                                    "legacy_reference": row.ref,
                                    "legacy_status": row.legacy_status,
                                    "import_source": Path(args.csv).name,
                                },
                                source="ushdesk",
                            )
                            await s.commit()

                        suppressed = len(bsvc.buffered_events)
                        if suppressed:
                            print(f"  [suppressed {suppressed} SQS event(s) for {row.ref} — no notifications sent]")

                    except DoubleBookingError as dbe:
                        ledger.therapist.setdefault(tid1, []).append(slot)
                        ledger.therapist.setdefault(tid2, []).append(slot)
                        res.reason = f"double booking conflict: {dbe}"
                        continue

                    # Sync appointment cache: two PRIMARY rows (one per Japanese Room)
                    cache_services = [
                        {
                            "service_id": str(svc_e.get("service_id") or "") or None,
                            "service_name": (svc_e.get("service_data") or {}).get("name", ""),
                            "service_arrangement_id": str(svc_e.get("service_arrangement_id") or "") or None,
                            "service_arrangement_name": (svc_e.get("service_arrangement_data") or {}).get("arrangement_name", ""),
                            "duration_minutes": svc_e.get("duration_minutes", 0),
                        }
                        for svc_e in services_payload
                    ]
                    try:
                        # Row 1 — Therapist 1 in Japanese Room 1 (is_secondary=False)
                        await sync_appointment_cache_entry(
                            ushauth=ushauth,
                            direct_db=direct_db,
                            settings=settings,
                            booking_id=booking.id,
                            service_arrangement_id=aid,
                            service_arrangement_name=arr1_j.get("name", ""),
                            branch_id=branch_id,
                            branch_name=branch.get("name", ""),
                            therapist_id=tid1,
                            therapist_name=tname1,
                            customer_id=cust_data["id"],
                            customer_name=row.client,
                            service_id=str(primary_svc["id"]),
                            service_name=primary_svc["name"],
                            appointment_start=start_local,
                            appointment_end=end_local,
                            duration_minutes=total_dur,
                            status=BookingStatus.CONFIRMED.value,
                            payment_status=PaymentStatus.PENDING.value,
                            services_payload=cache_services if len(cache_services) > 1 else None,
                            # No secondary_therapist_id here — we write a full separate primary row below
                            secondary_therapist_id=None,
                            secondary_therapist_name="",
                        )
                        # Row 2 — Therapist 2 in Japanese Room 2 (also is_secondary=False)
                        # We use sync_appointment_cache directly on the DB to write a second
                        # independent primary row for the same booking.
                        if direct_db is not None:
                            await direct_db.sync_appointment_cache(
                                booking_id=str(booking.id),
                                service_arrangement_id=aid2_j,
                                service_arrangement_name=arr2_j.get("name", ""),
                                branch_id=str(branch_id),
                                branch_name=branch.get("name", ""),
                                therapist_id=tid2,
                                therapist_name=tname2,
                                customer_id=str(cust_data["id"]),
                                customer_name=row.client,
                                service_id=str(primary_svc["id"]),
                                service_name=primary_svc["name"],
                                appointment_start=start_local,
                                appointment_end=end_local,
                                duration_minutes=total_dur,
                                status=BookingStatus.CONFIRMED.value,
                                payment_status=PaymentStatus.PENDING.value,
                                services_payload=None,
                                secondary_therapist_id=None,
                                secondary_therapist_name="",
                                _force_new_primary=True,  # skip upsert — insert fresh row
                            )
                        print(f"  [cache] appointment_cache synced (dual Japanese rooms) for booking {booking.booking_number}")
                    except Exception as cache_exc:  # noqa: BLE001
                        print(f"  [cache] WARNING: appointment_cache sync failed for {row.ref}: {cache_exc}")

                    ledger.add(tid1, aid, slot)
                    ledger.add(tid2, aid2_j, slot)

                    res.status = "created"
                    res.booking_number = booking.booking_number or ""
                    res.therapist = tname1
                    res.secondary_therapist = tname2
                    res.arrangement = f"{arr1_j.get('name', '')} / {arr2_j.get('name', '')}"
                    res.price = str(grand_total)
                    placed = True
                    break  # out of relax_room_cap loop

                # ── Standard arrangement iteration (couple room or single) ──
                arr_iter = (
                    arrs_to_try
                    if not relax_room_cap
                    else sorted(arrs_to_try, key=lambda a: ledger.arrangement_load(str(a["id"]), slot))
                )
                for arr in arr_iter:
                    aid = str(arr["id"])
                    cap = int(arr.get("capacity") or 1)
                    if not relax_room_cap and not args.ignore_availability:
                        required_cap = 2 if is_couple else 1
                        if ledger.arrangement_load(aid, slot) + required_cap > cap:
                            continue

                    # ── pick therapist(s) ──────────────────────────────────
                    if is_couple:
                        therapist_pair = await _pick_two_therapists(
                            pool, slot, ledger,
                            ushauth=ushauth,
                            aid=aid,
                            start=start_local,
                            duration=total_dur,
                            ignore_availability=args.ignore_availability,
                            relax=relax_room_cap,
                        )
                        if therapist_pair is None:
                            continue
                        chosen1, chosen2 = therapist_pair
                        tid1, tname1 = str(chosen1["id"]), _tname(chosen1)
                        tid2, tname2 = str(chosen2["id"]), _tname(chosen2)
                    else:
                        # Single therapist path (with DoubleBookingError retry loop)
                        cands = sorted(
                            (t for t in pool if not ledger.therapist_busy(str(t["id"]), slot)),
                            key=lambda t: ledger.minutes(str(t["id"])),
                        )
                        chosen1 = None
                        while cands:
                            if args.ignore_availability or relax_room_cap:
                                chosen1 = cands[0]
                                break
                            avail = await ushauth.check_appointment_availability(
                                service_arrangement_id=aid,
                                appointment_date=start_local.strftime("%Y-%m-%d"),
                                appointment_time=start_local.strftime("%H:%M:%S"),
                                duration=total_dur,
                                therapist_ids=[str(t["id"]) for t in cands],
                            )
                            if avail.get("available") != "yes":
                                break
                            chosen1 = next((t for t in cands if str(t["id"]) == str(avail["therapist_id"])), None)
                            if chosen1 is not None:
                                break
                            cands.pop(0)
                        if chosen1 is None:
                            continue
                        tid1, tname1 = str(chosen1["id"]), _tname(chosen1)
                        tid2, tname2 = None, None

                    # ── pricing ────────────────────────────────────────────
                    primary_price = Decimal(str(
                        arr["arrangement_price"] if arr.get("arrangement_price") is not None
                        else primary_svc.get("base_price") or "0"
                    ))
                    # For multi-service: sum prices across all real services using their arrangements
                    total_price = Decimal("0")
                    for _, svc_i in real_svcs:
                        svc_arrs_i = await catalog.arrangements(str(svc_i["id"]), is_addon=False)
                        # Find matching arrangement (prefer same arr, else first)
                        matched_arr = next((a for a in svc_arrs_i if str(a["id"]) == aid), svc_arrs_i[0] if svc_arrs_i else None)
                        if matched_arr:
                            p = Decimal(str(matched_arr.get("arrangement_price") or svc_i.get("base_price") or "0"))
                        else:
                            p = Decimal(str(svc_i.get("base_price") or "0"))
                        total_price += p
                    addon_price = sum(
                        Decimal(str(svc.get("base_price") or "0")) for _, svc in addon_svcs
                    )
                    grand_total = total_price + addon_price
                    currency = arr.get("arrangement_currency") or primary_svc.get("currency") or "KWD"
                    eff_loyalty_pts = int(arr.get("arrangement_loyalty_points") or primary_svc.get("loyalty_points") or 0)
                    eff_price_in_pts = int(arr.get("arrangement_price_in_points") or primary_svc.get("price_in_points") or 0)

                    # ── build services payload (multi-service) ─────────────
                    services_payload: list[dict] = []
                    for pos_i, (sname_i, svc_i) in enumerate(real_svcs):
                        svc_arrs_i = await catalog.arrangements(str(svc_i["id"]), is_addon=False)
                        matched_arr_i = next((a for a in svc_arrs_i if str(a["id"]) == aid), svc_arrs_i[0] if svc_arrs_i else arr)
                        arr_price_i = Decimal(str(
                            matched_arr_i.get("arrangement_price") or svc_i.get("base_price") or "0"
                        ))
                        services_payload.append(
                            build_service_entry(
                                service_id=uuid.UUID(str(svc_i["id"])),
                                service_data={
                                    "id": str(svc_i["id"]),
                                    "name": svc_i["name"],
                                    "base_price": str(svc_i.get("base_price") or "0"),
                                    "base_duration": int(svc_i.get("duration_minutes") or 60),
                                    "is_eligible_for_loyalty": bool(svc_i.get("is_eligible_for_loyalty")),
                                    "loyalty_points": int(arr.get("arrangement_loyalty_points") or svc_i.get("loyalty_points") or 0),
                                    "price_in_points": int(arr.get("arrangement_price_in_points") or svc_i.get("price_in_points") or 0),
                                },
                                service_arrangement_id=uuid.UUID(str(matched_arr_i["id"])),
                                service_arrangement_data={
                                    "id": str(matched_arr_i["id"]),
                                    "arrangement_type": matched_arr_i.get("arrangement_type", ""),
                                    "room_name": matched_arr_i.get("name", ""),
                                    "arrangement_name": matched_arr_i.get("name", ""),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                base_price=arr_price_i,
                                duration_minutes=int(svc_i.get("duration_minutes") or 60),
                                position=pos_i,
                            )
                        )

                    # ── build addons payload ────────────────────────────────
                    addons_payload: list[dict] = [
                        {
                            "id": str(svc.get("id")),
                            "name": svc.get("name", sname),
                            "price": str(svc.get("base_price") or "0"),
                            "duration_minutes": int(svc.get("duration_minutes") or 15),
                        }
                        for sname, svc in addon_svcs
                    ]

                    if not args.execute:
                        # Dry-run: record planned result, update ledger
                        ledger.add(tid1, aid, slot)
                        if tid2:
                            ledger.add(tid2, aid, slot)
                        res.status = "planned"
                        res.therapist = tname1
                        res.secondary_therapist = tname2 or ""
                        res.arrangement = arr.get("name", "")
                        res.price = str(grand_total)
                        placed = True
                        break

                    # Execute mode: create booking
                    try:
                        async with factory() as s:  # type: ignore[misc]
                            bsvc = BufferedBookingService(s, settings, ushauth)
                            booking = await bsvc.create_booking(
                                customer_id=_uuid(cust_data["id"]),
                                customer_data=cust_data,
                                branch_id=_uuid(branch_id),
                                branch_data={
                                    "id": branch_id,
                                    "name": branch.get("name", ""),
                                    "branch_name": branch.get("name", ""),
                                    "address": branch.get("address_line1", "") or "",
                                    "branch_address": branch.get("address_line1", "") or "",
                                },
                                services=services_payload,
                                # Legacy single-service columns derived from primary (required by create_booking)
                                service_id=_uuid(str(primary_svc["id"])),
                                service_data={
                                    "id": str(primary_svc["id"]),
                                    "name": primary_svc["name"],
                                    "base_price": str(primary_svc.get("base_price") or "0"),
                                    "base_duration": int(primary_svc.get("duration_minutes") or 60),
                                    "is_eligible_for_loyalty": bool(primary_svc.get("is_eligible_for_loyalty")),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                service_arrangement_id=_uuid(aid),
                                service_arrangement_data={
                                    "id": aid,
                                    "arrangement_type": arr.get("arrangement_type", ""),
                                    "room_name": arr.get("name", ""),
                                    "arrangement_name": arr.get("name", ""),
                                    "loyalty_points": eff_loyalty_pts,
                                    "price_in_points": eff_price_in_pts,
                                },
                                therapist_id=_uuid(tid1),
                                therapist_data={"id": tid1, "name": tname1, "therapist_name": tname1},
                                secondary_therapist_id=_uuid(tid2) if tid2 else None,
                                secondary_therapist_data=(
                                    {"id": tid2, "name": tname2, "therapist_name": tname2} if tid2 else None
                                ),
                                appointment_start=start_local,
                                appointment_end=end_local,
                                duration_minutes=int(primary_svc.get("duration_minutes") or 60),
                                extra_minutes=0,
                                total_duration=total_dur + addon_dur,
                                addons_duration=addon_dur,
                                base_price=Decimal(str(primary_svc.get("base_price") or "0")),
                                pricing=PricingBreakdown(
                                    arrangement_price=total_price,
                                    price_for_extra_minutes=Decimal("0"),
                                    addon_price=addon_price,
                                    discount=Decimal("0"),
                                    tax=Decimal("0"),
                                    fees=Decimal("0"),
                                    total=grand_total,
                                    currency=currency,
                                ),
                                addons=addons_payload,
                                customer_notes=f"Imported from legacy appointment {row.ref}",
                                idempotency_key=idempotency_key,
                                booking_type="branch_service",
                                payment_type="service",
                                status=BookingStatus.CONFIRMED.value,
                                payment_status=PaymentStatus.PENDING.value,
                                created_by_user=row.legacy_user or "legacy-import",
                                created_by_user_data={
                                    "id": row.legacy_user or "legacy-import",
                                    "role": "admin",
                                    "user_type": "admin",
                                    "legacy_user": row.legacy_user,
                                    "legacy_reference": row.ref,
                                    "legacy_status": row.legacy_status,
                                    "import_source": Path(args.csv).name,
                                },
                                source="ushdesk",
                            )
                            await s.commit()

                        # ── suppress SQS events (no customer notifications) ─
                        suppressed = len(bsvc.buffered_events)
                        if suppressed:
                            print(f"  [suppressed {suppressed} SQS event(s) for {row.ref} — no notifications sent]")

                    except DoubleBookingError as dbe:
                        # Mark conflicting therapist(s) as busy and retry outer loop
                        ledger.therapist.setdefault(tid1, []).append(slot)
                        if tid2:
                            ledger.therapist.setdefault(tid2, []).append(slot)
                        res.reason = f"double booking conflict: {dbe}"
                        continue

                    # ── sync appointment cache ─────────────────────────────
                    cache_services = [
                        {
                            "service_id": str(svc_e.get("service_id") or "") or None,
                            "service_name": (svc_e.get("service_data") or {}).get("name", ""),
                            "service_arrangement_id": str(svc_e.get("service_arrangement_id") or "") or None,
                            "service_arrangement_name": (svc_e.get("service_arrangement_data") or {}).get("arrangement_name", ""),
                            "duration_minutes": svc_e.get("duration_minutes", 0),
                        }
                        for svc_e in services_payload
                    ]

                    try:
                        await sync_appointment_cache_entry(
                            ushauth=ushauth,
                            direct_db=direct_db,
                            settings=settings,
                            booking_id=booking.id,
                            service_arrangement_id=aid,
                            service_arrangement_name=arr.get("name", ""),
                            branch_id=branch_id,
                            branch_name=branch.get("name", ""),
                            therapist_id=tid1,
                            therapist_name=tname1,
                            customer_id=cust_data["id"],
                            customer_name=row.client,
                            service_id=str(primary_svc["id"]),
                            service_name=primary_svc["name"],
                            appointment_start=start_local,
                            appointment_end=end_local,
                            duration_minutes=total_dur,
                            status=BookingStatus.CONFIRMED.value,
                            payment_status=PaymentStatus.PENDING.value,
                            services_payload=cache_services if len(cache_services) > 1 else None,
                            secondary_therapist_id=tid2,
                            secondary_therapist_name=tname2 or "",
                        )
                        print(f"  [cache] appointment_cache synced for booking {booking.booking_number}")
                    except Exception as cache_exc:  # noqa: BLE001
                        # Cache sync failure is non-fatal — booking was already committed.
                        print(f"  [cache] WARNING: appointment_cache sync failed for {row.ref}: {cache_exc}")

                    ledger.add(tid1, aid, slot)
                    if tid2:
                        ledger.add(tid2, aid, slot)

                    res.status = "created"
                    res.booking_number = booking.booking_number or ""
                    res.therapist = tname1
                    res.secondary_therapist = tname2 or ""
                    res.arrangement = arr.get("name", "")
                    res.price = str(grand_total)
                    placed = True
                    break

                if placed:
                    break

            if not placed and res.status == "failed" and not res.reason:
                res.reason = "no free therapist/arrangement for the slot (use --ignore-availability to force)"

        # 3. report
        report = Path(args.report or f"scripts/data/import_report_{datetime.now():%Y%m%d_%H%M%S}.csv")
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "ref", "n", "client", "phone", "services", "addons",
                "is_couple", "start", "end",
                "therapist", "secondary_therapist", "arrangement",
                "price", "status", "booking_number", "reason",
            ])
            for r in results:
                w.writerow([
                    r.ref, r.index, r.client, r.phone,
                    r.service, r.addons, r.is_couple,
                    r.start, r.end,
                    r.therapist, r.secondary_therapist, r.arrangement,
                    r.price, r.status, r.booking_number, r.reason,
                ])
        by_status: dict[str, int] = {}
        for r in results:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        total = sum(Decimal(r.price) for r in results if r.price)
        print(f"\nDone. {len(results)} bookings: {by_status} | total value {total} KWD")
        print(f"Report: {report}")
        return 0 if not by_status.get("failed") else 3
    finally:
        await http.aclose()
        if direct_db is not None:
            await direct_db.aclose()
        if args.execute:
            await dispose_engine()


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def uuid_placeholder(phone: str) -> uuid.UUID:
    """Deterministic fake id used only in dry-run output (never persisted)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dry-run:{phone}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv")
    p.add_argument("--execute", action="store_true",
                   help="write to DB (SQS events/notifications are always suppressed for imported bookings)")
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
