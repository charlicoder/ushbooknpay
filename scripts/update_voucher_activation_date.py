#!/usr/bin/env python3
"""
scripts/update_voucher_activation_date.py
──────────────────────────────────────────
Update gift vouchers' activation date (activation_datetime) with their creation date (created_at).

By default, targets the 9 vouchers requested:
    5WP9EY, B078T5, E6QCIT, CX8AU6, R99D59, OILJ1G, HE6Z13, YN9LOL, S6DJN6

Safety:
    • DRY-RUN by default — shows all matching vouchers and previewed changes without writing.
    • Pass ``--apply`` to commit changes to the database.
    • Creates a JSON snapshot backup in ``scripts/backups/`` before updating.
    • Runs inside a single database transaction.

Usage:
    # 1. Preview changes (dry-run):
    python scripts/update_voucher_activation_date.py

    # 2. Apply changes:
    python scripts/update_voucher_activation_date.py --apply

    # 3. Target custom voucher codes:
    python scripts/update_voucher_activation_date.py --codes 5WP9EY B078T5 --apply

    # 4. Specify environment file or database URL:
    python scripts/update_voucher_activation_date.py --env .env --apply
    python scripts/update_voucher_activation_date.py --db-url "postgresql+asyncpg://..." --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_VOUCHER_CODES = [
    "5WP9EY",
    "B078T5",
    "E6QCIT",
    "CX8AU6",
    "R99D59",
    "OILJ1G",
    "HE6Z13",
    "YN9LOL",
    "S6DJN6",
]


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
    codes = [c.strip().upper() for c in (args.codes or DEFAULT_VOUCHER_CODES) if c.strip()]

    if not codes:
        print("[ERROR] No voucher codes specified.")
        return 1

    engine = create_async_engine(db_url, echo=False)

    try:
        async with engine.connect() as conn:
            # Fetch vouchers matching secret_code or voucher_number
            stmt = text("""
                SELECT
                    id,
                    secret_code,
                    voucher_number,
                    status,
                    created_at,
                    activation_datetime,
                    total_amount,
                    currency,
                    recipient_phone
                FROM gift_vouchers
                WHERE UPPER(secret_code) = ANY(:codes)
                   OR UPPER(voucher_number) = ANY(:codes)
                ORDER BY created_at ASC
            """)
            result = await conn.execute(stmt, {"codes": codes})
            rows = [dict(r._mapping) for r in result.fetchall()]

        print("\n" + "=" * 90)
        print(" UPDATE GIFT VOUCHER ACTIVATION DATE WITH CREATION DATE")
        print("=" * 90)
        print(f"Target codes requested: {len(codes)}")
        print(f"Matching vouchers found: {len(rows)}")

        if not rows:
            print("\n[WARN] No matching vouchers found in the database.")
            return 0

        # Print comparison table
        print("\n" + "-" * 90)
        print(f"{'Code':<10} {'Voucher #':<22} {'Status':<12} {'Created At (New Activation)':<30} {'Current Activation':<26}")
        print("-" * 90)

        backup_items = []
        update_ids = []

        for r in rows:
            code = r["secret_code"] or "—"
            v_num = r["voucher_number"] or "—"
            status = r["status"] or "—"
            created = r["created_at"]
            curr_act = r["activation_datetime"]

            created_str = created.strftime("%Y-%m-%d %H:%M:%S %Z") if created else "—"
            curr_act_str = curr_act.strftime("%Y-%m-%d %H:%M:%S %Z") if curr_act else "(NULL)"

            print(f"{code:<10} {v_num:<22} {status:<12} {created_str:<30} {curr_act_str:<26}")

            backup_items.append({
                "id": str(r["id"]),
                "secret_code": r["secret_code"],
                "voucher_number": r["voucher_number"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "old_activation_datetime": r["activation_datetime"].isoformat() if r["activation_datetime"] else None,
                "new_activation_datetime": r["created_at"].isoformat() if r["created_at"] else None,
            })
            update_ids.append(r["id"])

        print("-" * 90)

        # Check for any requested codes that were not found
        found_codes = {r["secret_code"].upper() for r in rows if r.get("secret_code")} | {
            r["voucher_number"].upper() for r in rows if r.get("voucher_number")
        }
        missing = [c for c in codes if c not in found_codes]
        if missing:
            print(f"\n[NOTICE] Codes not found in database ({len(missing)}): {', '.join(missing)}")

        if not args.apply:
            print("\n[DRY-RUN] No changes were written.")
            print("To execute this update in the database, re-run with:  --apply\n")
            return 0

        # Backup creation
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"backup_voucher_activation_update_{timestamp_str}.json"

        backup_file.write_text(json.dumps(backup_items, indent=2))
        print(f"\n[BACKUP] Saved snapshot of {len(backup_items)} vouchers to: {backup_file}")

        # Execute Update inside a transaction
        async with engine.begin() as conn:
            update_stmt = text("""
                UPDATE gift_vouchers
                SET activation_datetime = created_at,
                    updated_at = NOW()
                WHERE id = ANY(:ids)
            """)
            upd_res = await conn.execute(update_stmt, {"ids": update_ids})
            updated_count = upd_res.rowcount

        print(f"\n[SUCCESS] Updated activation_datetime = created_at for {updated_count} vouchers.")
        print("All target vouchers now have activation date matching their creation date.\n")
        return 0

    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Update gift vouchers' activation date with creation date (dry-run by default)."
    )
    ap.add_argument(
        "--codes",
        nargs="*",
        help="Voucher code(s) to update. Defaults to the 9 specified voucher codes if omitted.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to the database (without this flag, runs in dry-run preview mode).",
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
        help="Directory to store pre-change backup JSON (default: scripts/backups).",
    )
    args = ap.parse_args()
    _load_env(args.env)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
