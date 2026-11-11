#!/usr/bin/env python3
"""
scripts/remove_booking_therapist.py
───────────────────────────────────
Remove ONE therapist from a booking that was created with two therapists by mistake
(e.g. a single-room session that shows "NIKAY … + JINKY …").

What it fixes
─────────────
ushbooknpay DB  → ``bookings`` row
    • secondary_therapist_id / secondary_therapist_data
      (or, when the PRIMARY therapist is the one removed, the secondary is promoted
       to primary and the secondary columns are cleared)
    • the same change inside every ``sessions[*]`` entry
      → this is what the Booking Details "Session" line and the Print Receipt read.

ushauth DB      → ``ush_spa_appointment_cache``
    • deletes the cache row(s) of the removed therapist for this booking
      → this is what the Therapist Schedule page draws (one card per therapist).
      A DB trigger recomputes ``ush_spa_daily_slot_ledger`` automatically, so the
      removed therapist's time becomes free again.
    • if the primary was removed, the promoted therapist's row loses the
      ``is_secondary_therapist`` flag.

Safety
──────
    • DRY-RUN by default — nothing is written unless you pass ``--apply``.
    • With ``--apply`` the old values are first saved to a JSON backup file.
    • Each database is changed inside a single transaction.

Usage
─────
    # 1. Preview (removes the SECONDARY therapist by default)
    python scripts/remove_booking_therapist.py --booking-number BOK/2026/11/000211

    # 2. Preview removing a specific therapist (UUID or part of the name)
    python scripts/remove_booking_therapist.py --booking-number BOK/2026/11/000211 \\
        --remove-therapist "JINKY"

    # 3. Apply
    python scripts/remove_booking_therapist.py --booking-number BOK/2026/11/000211 \\
        --remove-therapist "JINKY" --apply

Environment (read from ``--env`` / ushbooknpay/.env / process env):
    DATABASE_URL            ushbooknpay database
    USHAUTH_DATABASE_URL    ushauth database (or pass --ushauth-db-url)

Run it against the SAME environment as the screenshots (dev / prod): point the two
URLs at that environment's databases.

Requirements: sqlalchemy[asyncio], asyncpg, python-dotenv (all in the ushbooknpay venv).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


# ── env helpers ───────────────────────────────────────────────────────────────

def _load_env(env_path: str | None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[WARN] python-dotenv not installed; using existing environment variables.")
        return
    root = Path(__file__).resolve().parent.parent
    candidates = [Path(env_path)] if env_path else [root / ".env", Path("/app/.env")]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
            print(f"[INFO] Loaded env from: {p}")


def _async_url(url: str) -> str:
    """Normalise any postgres URL to the asyncpg driver SQLAlchemy needs."""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def _mask(url: str) -> str:
    import re
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


def _guess_ushauth_url(bnp_url: str) -> str:
    """Find the ushauth DB without extra config: sibling ushauth/.env, else swap the DB name."""
    sibling = Path(__file__).resolve().parent.parent.parent / "ushauth" / ".env"
    if sibling.exists():
        for line in sibling.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("\"'")
    if "ushbooknpay" in bnp_url:  # same server, naming convention ushbooknpay_* → ushauth_*
        return bnp_url.replace("ushbooknpay", "ushauth")
    return ""


# ── data helpers ──────────────────────────────────────────────────────────────

def _name_of(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for k in ("full_name", "name", "therapist_name"):
        if data.get(k):
            return str(data[k])
    return ""


def _same(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and str(a).lower() == str(b).lower()


def _collect_therapists(booking: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, str]:
    """All distinct therapists mentioned on the booking and its sessions: {id(lower): name}."""
    found: dict[str, str] = {}

    def add(tid: Any, data: Any) -> None:
        if not tid:
            return
        key = str(tid).lower()
        name = _name_of(data)
        if key not in found or (name and not found[key]):
            found[key] = name

    add(booking["therapist_id"], booking["therapist_data"])
    add(booking["secondary_therapist_id"], booking["secondary_therapist_data"])
    for s in sessions:
        add(s.get("therapist_id"), s.get("therapist_data"))
        add(s.get("secondary_therapist_id"), s.get("secondary_therapist_data"))
    return found


def _resolve_target(booking: dict[str, Any], sessions: list[dict[str, Any]], needle: str | None) -> tuple[str, str]:
    """Return (therapist_id, name) of the therapist to remove."""
    found = _collect_therapists(booking, sessions)
    if not needle:
        # default: the secondary — prefer the session level (booking level may hold bad data)
        for s in sessions:
            sid = s.get("secondary_therapist_id")
            if sid and not _same(sid, s.get("therapist_id")):
                return str(sid), found.get(str(sid).lower(), "")
        sid = booking["secondary_therapist_id"]
        if sid and not _same(sid, booking["therapist_id"]):
            return str(sid), found.get(str(sid).lower(), "")
        raise SystemExit("[ERROR] Booking has no secondary therapist — nothing to remove.")
    n = needle.strip().lower()
    for tid, name in found.items():
        if n == tid or (n and n in name.lower()):
            return tid, name
    listing = "\n".join(f"        {name or '(no name)'}  ({tid})" for tid, name in found.items())
    raise SystemExit(f"[ERROR] '{needle}' matches no therapist on this booking. Found:\n{listing}")


def _strip(entry: dict[str, Any], remove_id: str) -> bool:
    """
    Remove ``remove_id`` from a booking/session-like dict in place.
    Returns True when something changed.
    """
    changed = False
    # bad data guard: a "secondary" that is just the primary again
    if _same(entry.get("secondary_therapist_id"), entry.get("therapist_id")) and not _same(
        entry.get("therapist_id"), remove_id
    ):
        entry["secondary_therapist_id"] = None
        if "secondary_therapist_data" in entry:
            entry["secondary_therapist_data"] = None
        changed = True
    if _same(entry.get("secondary_therapist_id"), remove_id):
        entry["secondary_therapist_id"] = None
        if "secondary_therapist_data" in entry:
            entry["secondary_therapist_data"] = None
        changed = True
    if _same(entry.get("therapist_id"), remove_id):
        # promote the secondary (if any) to primary
        entry["therapist_id"] = entry.get("secondary_therapist_id")
        entry["therapist_data"] = entry.get("secondary_therapist_data")
        entry["secondary_therapist_id"] = None
        if "secondary_therapist_data" in entry:
            entry["secondary_therapist_data"] = None
        changed = True
    return changed


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


# ── main ──────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    bnp_url = _async_url(args.db_url or os.environ.get("DATABASE_URL", ""))
    if not bnp_url:
        raise SystemExit("[ERROR] ushbooknpay DATABASE_URL is missing (--db-url).")
    auth_url = _async_url(
        args.ushauth_db_url or os.environ.get("USHAUTH_DATABASE_URL", "") or _guess_ushauth_url(bnp_url)
    )
    if not auth_url:
        print("[WARN] ushauth DB URL not found — the Therapist Schedule cache will NOT be updated. "
              "Pass --ushauth-db-url.")
    else:
        print(f"[INFO] ushauth DB: {_mask(auth_url)}")

    bnp = create_async_engine(bnp_url, echo=False)
    auth = create_async_engine(auth_url, echo=False) if auth_url else None
    try:
        # ── 1. load booking ──────────────────────────────────────────────────
        async with bnp.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT id, booking_number, status, therapist_id, therapist_data,
                               secondary_therapist_id, secondary_therapist_data, sessions
                        FROM bookings
                        WHERE booking_number = :n OR id::text = :n
                        """
                    ),
                    {"n": args.booking_number},
                )
            ).mappings().first()
        if not row:
            raise SystemExit(f"[ERROR] Booking '{args.booking_number}' not found in this database.")
        booking = dict(row)
        booking_id = str(booking["id"])

        print("\n══ BEFORE ═══════════════════════════════════════════════════════")
        print(f"Booking   : {booking['booking_number']}  ({booking_id})  status={booking['status']}")
        print(f"Primary   : {_name_of(booking['therapist_data'])}  ({booking['therapist_id']})")
        print(f"Secondary : {_name_of(booking['secondary_therapist_data'])}  ({booking['secondary_therapist_id']})")
        sessions = copy.deepcopy(booking["sessions"]) if isinstance(booking["sessions"], list) else []
        for i, s in enumerate(sessions):
            print(
                f"  session {i + 1}: {_name_of(s.get('therapist_data'))} + "
                f"{_name_of(s.get('secondary_therapist_data'))}  (id={s.get('session_id')})"
            )

        remove_id, remove_name = _resolve_target(booking, sessions, args.remove_therapist)
        print(f"\n→ Therapist to REMOVE: {remove_name}  ({remove_id})")

        # ── 2. compute new booking values ────────────────────────────────────
        new_b = {
            "therapist_id": booking["therapist_id"],
            "therapist_data": copy.deepcopy(booking["therapist_data"]),
            "secondary_therapist_id": booking["secondary_therapist_id"],
            "secondary_therapist_data": copy.deepcopy(booking["secondary_therapist_data"]),
        }
        promoted_id: str | None = None
        if _same(new_b["therapist_id"], remove_id):
            promoted_id = str(new_b["secondary_therapist_id"]) if new_b["secondary_therapist_id"] else None
            if not promoted_id or _same(promoted_id, remove_id):
                raise SystemExit("[ERROR] Cannot remove the only therapist of the booking.")
        _strip(new_b, remove_id)
        new_sessions = copy.deepcopy(sessions)
        for s in new_sessions:
            _strip(s, remove_id)

        print("\n══ AFTER (planned) ══════════════════════════════════════════════")
        print(f"Primary   : {_name_of(new_b['therapist_data'])}  ({new_b['therapist_id']})")
        print(f"Secondary : {_name_of(new_b['secondary_therapist_data'])}  ({new_b['secondary_therapist_id']})")
        for i, s in enumerate(new_sessions):
            print(
                f"  session {i + 1}: {_name_of(s.get('therapist_data'))} + "
                f"{_name_of(s.get('secondary_therapist_data'))}"
            )

        # ── 3. ushauth cache rows ────────────────────────────────────────────
        cache_rows: list[dict[str, Any]] = []
        if auth is not None:
            async with auth.connect() as conn:
                cache_rows = [
                    dict(r)
                    for r in (
                        await conn.execute(
                            text(
                                """
                                SELECT id, therapist_id, therapist_name, is_secondary_therapist,
                                       status, appointment_start, appointment_end
                                FROM ush_spa_appointment_cache
                                WHERE bookings_id = :b
                                ORDER BY is_secondary_therapist, created_at
                                """
                            ),
                            {"b": booking_id},
                        )
                    ).mappings().all()
                ]
            print("\n── ushauth appointment cache rows for this booking ──")
            for r in cache_rows:
                mark = "  ← DELETE" if _same(r["therapist_id"], remove_id) else ""
                print(
                    f"  {r['id']}  {r['therapist_name']:<32} secondary={r['is_secondary_therapist']!s:<5} "
                    f"{r['status']:<10} {r['appointment_start']:%Y-%m-%d %H:%M}{mark}"
                )
            if not any(_same(r["therapist_id"], remove_id) for r in cache_rows):
                print("  (no cache row for the removed therapist — schedule page is already clean)")

        if not args.apply:
            print("\n[DRY-RUN] Nothing was written. Re-run with --apply to make the change.")
            return 0

        # ── 4. backup ────────────────────────────────────────────────────────
        backup = Path(args.backup_dir) / (
            f"backup_{booking['booking_number'].replace('/', '_')}_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(
            json.dumps(
                {
                    "booking": _jsonable({k: booking[k] for k in (
                        "id", "booking_number", "therapist_id", "therapist_data",
                        "secondary_therapist_id", "secondary_therapist_data", "sessions")}),
                    "removed_therapist": {"id": remove_id, "name": remove_name},
                    "cache_rows": _jsonable(cache_rows),
                },
                indent=2,
            )
        )
        print(f"\n[BACKUP] {backup}")

        # ── 5. write ushbooknpay ─────────────────────────────────────────────
        async with bnp.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE bookings
                       SET therapist_id = :tid,
                           therapist_data = CAST(:tdata AS JSONB),
                           secondary_therapist_id = :sid,
                           secondary_therapist_data = CAST(:sdata AS JSONB),
                           sessions = CAST(:sessions AS JSONB),
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {
                    "tid": new_b["therapist_id"],
                    "tdata": json.dumps(new_b["therapist_data"], default=str),
                    "sid": new_b["secondary_therapist_id"],
                    "sdata": json.dumps(new_b["secondary_therapist_data"], default=str),
                    "sessions": json.dumps(new_sessions, default=str),
                    "id": booking["id"],
                },
            )
        print("[OK] ushbooknpay: booking + sessions updated (receipt / details now show one therapist).")

        # ── 6. write ushauth ─────────────────────────────────────────────────
        if auth is not None:
            async with auth.begin() as conn:
                res = await conn.execute(
                    text("DELETE FROM ush_spa_appointment_cache WHERE bookings_id = :b AND therapist_id = :t"),
                    {"b": booking_id, "t": remove_id},
                )
                if promoted_id:
                    await conn.execute(
                        text(
                            "UPDATE ush_spa_appointment_cache SET is_secondary_therapist = false "
                            "WHERE bookings_id = :b AND therapist_id = :t"
                        ),
                        {"b": booking_id, "t": promoted_id},
                    )
            print(f"[OK] ushauth: deleted {res.rowcount} cache row(s) — therapist schedule page is fixed.")

        print("\nDone. Refresh the Booking Details / Therapist Schedule pages (hard refresh if cached).")
        return 0
    finally:
        await bnp.dispose()
        if auth is not None:
            await auth.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description="Remove one therapist from a booking (dry-run by default).")
    ap.add_argument("--booking-number", required=True, help="e.g. BOK/2026/11/000211 (or the booking UUID)")
    ap.add_argument("--remove-therapist", help="Therapist UUID or part of the name. Default: the secondary therapist.")
    ap.add_argument("--apply", action="store_true", help="Write the changes (otherwise dry-run).")
    ap.add_argument("--db-url", help="ushbooknpay database URL (default: env DATABASE_URL).")
    ap.add_argument("--ushauth-db-url", help="ushauth database URL (default: env USHAUTH_DATABASE_URL).")
    ap.add_argument("--env", help="Path to a .env file to load.")
    ap.add_argument("--backup-dir", default="scripts/backups", help="Where to save the pre-change backup JSON.")
    args = ap.parse_args()
    _load_env(args.env)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

