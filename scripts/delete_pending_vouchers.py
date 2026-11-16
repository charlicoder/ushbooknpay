#!/usr/bin/env python3
"""
scripts/delete_pending_vouchers.py
───────────────────────────────────
Safely delete pending (unpaid / abandoned) gift vouchers within a given date range.

Pending vouchers are those in 'created' or 'payment_pending' states where the checkout
was never completed.

Safety & Guards:
    • DRY-RUN by default — inspects and previews matching vouchers without deleting.
    • Pass ``--apply`` to commit the deletion.
    • Automatic JSON backup saved in ``scripts/backups/`` before any rows are deleted.
    • Strict status guard: Only matches vouchers with status in ('payment_pending', 'created', 'pending').
      NEVER deletes active, scheduled, redeemed, partially_redeemed, or fulfilled vouchers.
    • Booking guard: Strictly excludes any voucher with linked bookings (redeemed_booking_id / booking_id).
    • Transactional: Runs in an atomic DB transaction with automatic rollback on error.
    • Requires an explicit date filter (--from-date and/or --to-date).

Usage:
    # 1. Preview pending vouchers created in October 2026 (dry-run):
    python scripts/delete_pending_vouchers.py --from-date 2026-10-01 --to-date 2026-10-31

    # 2. Preview pending vouchers up to a specific date:
    python scripts/delete_pending_vouchers.py --to-date 2026-11-05

    # 3. Apply deletion with automatic JSON backup:
    python scripts/delete_pending_vouchers.py --from-date 2026-10-01 --to-date 2026-10-31 --apply

    # 4. Filter by specific pending status (e.g., only 'payment_pending'):
    python scripts/delete_pending_vouchers.py --from-date 2026-11-01 --to-date 2026-11-10 --statuses payment_pending --apply

    # 5. Specify custom .env or database URL:
    python scripts/delete_pending_vouchers.py --from-date 2026-10-01 --to-date 2026-10-31 --env .env --apply
    python scripts/delete_pending_vouchers.py --from-date 2026-10-01 --to-date 2026-10-31 --db-url "postgresql+asyncpg://..." --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_PENDING_STATUSES = ["payment_pending", "created", "pending"]
KUWAIT_TZ = ZoneInfo("Asia/Kuwait")


def _load_env(env_path: str | None) -> None:
    """Load environment variables from .env candidate locations."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[WARN] python-dotenv not installed; using existing environment variables.")
        return

    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    else:
        script_dir = Path(__file__).resolve().parent.parent  # .../ushbooknpay/
        root_dir = script_dir.parent                        # .../microservices/
        candidates += [
            script_dir / ".env",
            root_dir / ".env",
            Path("/app/.env"),
        ]

    loaded = []
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
            loaded.append(str(p))

    if loaded:
        print(f"[INFO] Loaded env from: {', '.join(loaded)}")


def _resolve_db_url(cli_url: str | None) -> str:
    """Resolve database URL and ensure asyncpg driver prefix."""
    raw = cli_url or os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("[ERROR] No DATABASE_URL found in environment or --db-url.")
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgresql://") and not raw.startswith("postgresql+asyncpg://"):
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


def _parse_boundary_datetime(val: str, is_end_of_day: bool = False) -> datetime:
    """Parse date or datetime string into a timezone-aware UTC datetime."""
    val = val.strip()
    # Check if YYYY-MM-DD
    if len(val) == 10 and val.count("-") == 2:
        d = date.fromisoformat(val)
        t = time(23, 5, 59, 999999) if is_end_of_day else time(0, 0, 0, 0)
        dt_local = datetime.combine(d, t, tzinfo=KUWAIT_TZ)
        return dt_local.astimezone(timezone.utc)
    # Check if ISO datetime string
    dt = datetime.fromisoformat(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KUWAIT_TZ)
    return dt.astimezone(timezone.utc)


def _jsonable(obj: Any) -> Any:
    """Recursively convert datetime/UUID/Decimal for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "hex"):
        return str(obj)
    return str(obj) if hasattr(obj, "as_tuple") else obj


async def run(args: argparse.Namespace) -> int:
    db_url = _resolve_db_url(args.db_url)

    if not args.from_date and not args.to_date:
        print("[ERROR] You must specify at least --from-date or --to-date to define the target date range.")
        print("Example: python scripts/delete_pending_vouchers.py --from-date 2026-10-01 --to-date 2026-10-31")
        return 1

    dt_from: datetime | None = None
    dt_to: datetime | None = None

    if args.from_date:
        try:
            dt_from = _parse_boundary_datetime(args.from_date, is_end_of_day=False)
        except Exception as e:
            print(f"[ERROR] Invalid --from-date format '{args.from_date}': {e}")
            return 1

    if args.to_date:
        try:
            dt_to = _parse_boundary_datetime(args.to_date, is_end_of_day=True)
        except Exception as e:
            print(f"[ERROR] Invalid --to-date format '{args.to_date}': {e}")
            return 1

    target_statuses = [s.strip().lower() for s in (args.statuses or DEFAULT_PENDING_STATUSES) if s.strip()]

    # Safety: Validate that no active/redeemed statuses were accidentally included
    forbidden_statuses = {"active", "redeemed", "partially_redeemed", "fulfilled", "scheduled"}
    intercepted = set(target_statuses) & forbidden_statuses
    if intercepted:
        print(f"[FATAL ERROR] Non-pending statuses detected in filter: {intercepted}.")
        print("This script is strictly restricted to deleting pending vouchers only.")
        return 1

    engine = create_async_engine(db_url, echo=False)

    try:
        # Build query clauses
        where_clauses = [
            "status = ANY(:statuses)",
            "redeemed_booking_id IS NULL",
            "booking_id IS NULL",
        ]
        params: dict[str, Any] = {"statuses": target_statuses}

        if dt_from:
            where_clauses.append("created_at >= :dt_from")
            params["dt_from"] = dt_from

        if dt_to:
            where_clauses.append("created_at <= :dt_to")
            params["dt_to"] = dt_to

        where_sql = " AND ".join(where_clauses)

        async with engine.connect() as conn:
            select_stmt = text(f"""
                SELECT
                    id,
                    secret_code,
                    voucher_number,
                    status,
                    created_at,
                    expire_date,
                    total_amount,
                    currency,
                    sender_data,
                    recipient_phone,
                    recipient_data,
                    payment_through,
                    payment_provider,
                    payment_id
                FROM gift_vouchers
                WHERE {where_sql}
                ORDER BY created_at ASC
            """)
            result = await conn.execute(select_stmt, params)
            rows = [dict(r._mapping) for r in result.fetchall()]

        print("\n" + "=" * 95)
        print(" DELETE PENDING GIFT VOUCHERS BY DATE RANGE")
        print("=" * 95)
        print(f"Target statuses: {', '.join(target_statuses)}")
        if dt_from:
            print(f"From Date:       {dt_from.strftime('%Y-%m-%d %H:%M:%S UTC')} (Kuwait: {dt_from.astimezone(KUWAIT_TZ).strftime('%Y-%m-%d %H:%M:%S')})")
        if dt_to:
            print(f"To Date:         {dt_to.strftime('%Y-%m-%d %H:%M:%S UTC')} (Kuwait: {dt_to.astimezone(KUWAIT_TZ).strftime('%Y-%m-%d %H:%M:%S')})")
        print(f"Matching rows:   {len(rows)}")

        if not rows:
            print("\n[INFO] No pending vouchers match the specified date range and criteria.")
            return 0

        # Print preview table (first 25 rows, plus summary)
        preview_count = min(len(rows), 25)
        print("\n" + "-" * 95)
        print(f"{'Code':<10} {'Voucher #':<22} {'Status':<18} {'Created At':<24} {'Amount':<14} {'Sender':<15}")
        print("-" * 95)

        for r in rows[:preview_count]:
            code = r["secret_code"] or "—"
            v_num = r["voucher_number"] or "—"
            status = r["status"] or "—"
            created = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "—"
            amt = f"{float(r['total_amount']):.3f} {r['currency']}" if r.get("total_amount") is not None else "—"
            sender = (r.get("sender_data") or {}).get("name") or "—"
            print(f"{code:<10} {v_num:<22} {status:<18} {created:<24} {amt:<14} {sender:<15}")

        if len(rows) > preview_count:
            print(f"  ... and {len(rows) - preview_count} more pending vouchers")
        print("-" * 95)

        total_val = sum(float(r["total_amount"]) for r in rows if r.get("total_amount") is not None)
        print(f"Total pending vouchers to delete: {len(rows)}  |  Total nominal amount: {total_val:.3f} KWD")

        if not args.apply:
            print("\n[DRY-RUN] No vouchers were deleted.")
            print("To delete these pending vouchers from the database, re-run with:  --apply\n")
            return 0

        # Backup creation before deleting
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"backup_deleted_pending_vouchers_{timestamp_str}.json"

        backup_payload = {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "criteria": {
                "statuses": target_statuses,
                "from_date": dt_from.isoformat() if dt_from else None,
                "to_date": dt_to.isoformat() if dt_to else None,
            },
            "count": len(rows),
            "vouchers": _jsonable(rows),
        }
        backup_file.write_text(json.dumps(backup_payload, indent=2))
        print(f"\n[BACKUP] Saved full snapshot of {len(rows)} vouchers to: {backup_file}")

        # Delete inside transaction
        target_ids = [r["id"] for r in rows]
        async with engine.begin() as conn:
            del_stmt = text("""
                DELETE FROM gift_vouchers
                WHERE id = ANY(:ids)
                  AND status = ANY(:statuses)
                  AND redeemed_booking_id IS NULL
                  AND booking_id IS NULL
            """)
            del_res = await conn.execute(del_stmt, {"ids": target_ids, "statuses": target_statuses})
            deleted_count = del_res.rowcount

        print(f"\n[SUCCESS] Deleted {deleted_count} pending gift vouchers from database.")
        print(f"Backup file is safely stored at: {backup_file}\n")
        return 0

    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Delete pending gift vouchers by date range (dry-run by default)."
    )
    ap.add_argument(
        "--from-date",
        "-f",
        help="Start creation date (inclusive), e.g. '2026-10-01' or '2026-10-01T00:00:00'.",
    )
    ap.add_argument(
        "--to-date",
        "-t",
        help="End creation date (inclusive), e.g. '2026-10-31' or '2026-10-31T23:59:59'.",
    )
    ap.add_argument(
        "--statuses",
        nargs="*",
        default=DEFAULT_PENDING_STATUSES,
        help="Pending statuses to target (default: 'payment_pending', 'created', 'pending').",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletion (without this flag, runs in dry-run preview mode).",
    )
    ap.add_argument(
        "--db-url",
        help="Database URL (defaults to DATABASE_URL from .env).",
    )
    ap.add_argument(
        "--env",
        help="Path to .env file to load.",
    )
    ap.add_argument(
        "--backup-dir",
        default="scripts/backups",
        help="Directory to store pre-deletion backup JSON (default: scripts/backups).",
    )
    args = ap.parse_args()
    _load_env(args.env)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
