#!/usr/bin/env python3
"""
scripts/repair_voucher_records.py
──────────────────────────────────
Repair missing invoice and payment records for a paid gift voucher.

When a voucher.active event is processed by ushnotice, it should:
  1. Create an invoice in ushanr  (POST /api/v1/internal/invoices/from-source/)
  2. Link the invoice to the payment in ushbooknpay  (POST /api/v1/payments/link-invoice/)
  3. Create a payment record in ushbooknpay  (POST /api/v1/payments/)

If any of these steps failed (or the event was never processed), this script
replays them idempotently for the given voucher number.

Strategy:
  - Reads (voucher data, existing payment check) go **direct to the DB** because
    the ushbooknpay list APIs require a full JWT that the script cannot obtain.
  - Writes (invoice creation, payment creation) go **via HTTP** to preserve service
    boundaries and leverage server-side idempotency & business logic.

Usage (from inside the ushbooknpay container or with DB access):
    # Dry-run — shows what would be created, no writes:
    python scripts/repair_voucher_records.py --voucher-number VOU/2026/10/000147 --dry-run

    # Live run:
    python scripts/repair_voucher_records.py --voucher-number VOU/2026/10/000147

    # With a specific .env:
    python scripts/repair_voucher_records.py --voucher-number VOU/2026/10/000147 --env /app/.env

Requirements (all present in ushbooknpay container):
    asyncpg, sqlalchemy, httpx, python-dotenv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


# ── Env loading ────────────────────────────────────────────────────────────────

def _load_env(env_path: str | None) -> None:
    """Load .env file(s) into os.environ."""
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
        root_dir   = script_dir.parent                        # .../microservices/
        candidates += [
            script_dir / ".env",
            root_dir / "ushnotice" / ".env",
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
    else:
        print("[WARN] No .env file found; using existing environment variables.")


def _require_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise SystemExit(f"[ERROR] Missing required env var: {key}")
    return v


# ── DB helpers (read-only) ─────────────────────────────────────────────────────

async def _db_fetch_voucher(db_url: str, voucher_number: str) -> dict[str, Any] | None:
    """Fetch a gift voucher row directly from the ushbooknpay DB."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT
                        id, voucher_number, status,
                        sender_id, sender_data,
                        recipient_id, recipient_phone, recipient_data,
                        service_id, service_data,
                        branch_id, branch_data,
                        service_arrangement_id, service_arrangement_data,
                        total_amount, total_duration, currency,
                        addons, extra_time, price_for_extra_time,
                        payment_id, payment_data, payment_provider, payment_through,
                        payment_url,
                        created_by, created_at, expire_date
                    FROM gift_vouchers
                    WHERE voucher_number = :num
                """),
                {"num": voucher_number},
            )
            row = result.mappings().first()
            return dict(row) if row else None
    finally:
        await engine.dispose()


async def _db_fetch_payment(db_url: str, voucher_id: str) -> dict[str, Any] | None:
    """Fetch an existing payment record for a voucher from the ushbooknpay DB."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT id, payment_number, invoice_number,
                           customer_id, voucher_id, voucher_number,
                           status, payment_for, payment_provider, payment_through,
                           total_amount, currency, payment_data, created_at
                    FROM payments
                    WHERE voucher_id = :vid
                    LIMIT 1
                """),
                {"vid": voucher_id},
            )
            row = result.mappings().first()
            return dict(row) if row else None
    finally:
        await engine.dispose()


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _post(
    url: str,
    payload: dict,
    *,
    headers: dict,
    timeout: float = 30.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        print(f"  [DRY-RUN] POST {url}")
        print(f"  Payload:\n{json.dumps(payload, indent=4, default=str)}")
        return {"__dry_run": True}
    import httpx
    r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    if not r.is_success:
        raise RuntimeError(
            f"POST {url} → HTTP {r.status_code}\n"
            f"Response: {json.dumps(body, indent=2, default=str)[:2000]}"
        )
    return body


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _decimal(v: Any) -> str:
    try:
        return str(Decimal(str(v)).quantize(Decimal("0.001")))
    except Exception:
        return "0.000"


# ── Main logic ─────────────────────────────────────────────────────────────────

async def run(voucher_number: str, *, dry_run: bool) -> None:

    # ── Config ────────────────────────────────────────────────────────────────
    db_url = (
        os.environ.get("DATABASE_URL", "")
        or os.environ.get("ASYNC_DATABASE_URL", "")
        or os.environ.get("DB_URL", "")
    ).strip()
    if not db_url:
        raise SystemExit(
            "[ERROR] No database URL found. "
            "Set DATABASE_URL, ASYNC_DATABASE_URL, or DB_URL in your .env file."
        )
    # Ensure async driver
    if "postgresql://" in db_url and "postgresql+asyncpg://" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

    booknpay_url  = (
        os.environ.get("USHBOOKNPAY_INTERNAL_URL", "")
        or os.environ.get("API_GATEWAY_BASE_URL", "http://localhost:8000")
    ).rstrip("/")
    booknpay_path = os.environ.get("USHBOOKNPAY_BASE_PATH", "/booknpay").rstrip("/")
    ushanr_url    = _require_env("USHANR_BASE_URL").rstrip("/")
    ushanr_key    = _require_env("USHANR_INTERNAL_API_KEY")
    company_id    = _require_env("USHANR_COMPANY_ID")
    journal_id    = _require_env("USHANR_AR_JOURNAL_ID")
    revenue_acct  = _require_env("USHANR_REVENUE_ACCOUNT_ID")
    ushspa_token  = os.environ.get("USHSPA_TOKEN", "").strip()

    booknpay_base = f"{booknpay_url}{booknpay_path}"

    # ushbooknpay internal endpoints accept X-USHSPA-TOKEN (no JWT required)
    booknpay_headers = {
        "Content-Type": "application/json",
        "X-USHSPA-TOKEN": ushspa_token,
        "X-App-Token": ushspa_token,
    }
    ushanr_headers = {
        "Content-Type": "application/json",
        "X-Internal-Key": ushanr_key,
        "X-App-Token": ushspa_token,
        "X-Service": "repair-script",
    }

    print("=" * 66)
    print(f"  Voucher repair: {voucher_number}")
    print(f"  Mode          : {'DRY-RUN (no writes)' if dry_run else 'LIVE'}")
    print(f"  ushbooknpay   : {booknpay_base}")
    print(f"  ushanr        : {ushanr_url}")
    print("=" * 66)

    # ── Step 1: Fetch voucher from DB ────────────────────────────────────────
    print(f"\n[1/6] Fetching voucher '{voucher_number}' from database...")
    voucher = await _db_fetch_voucher(db_url, voucher_number)
    if not voucher:
        raise SystemExit(f"[ERROR] Voucher '{voucher_number}' not found in the database.")

    voucher_id      = str(voucher["id"])
    v_status        = str(voucher.get("status") or "").lower()
    sender_id       = str(voucher.get("sender_id") or "")
    sender_data: dict      = voucher.get("sender_data") or {}
    service_id      = str(voucher.get("service_id") or "")
    service_data: dict     = voucher.get("service_data") or {}
    branch_id       = str(voucher.get("branch_id") or "")
    branch_data: dict      = voucher.get("branch_data") or {}
    svc_arr_id      = str(voucher.get("service_arrangement_id") or "")
    svc_arr_data: dict     = voucher.get("service_arrangement_data") or {}
    total_amount    = _decimal(voucher.get("total_amount"))
    total_duration  = int(voucher.get("total_duration") or 0)
    currency        = str(voucher.get("currency") or "KWD")
    payment_id_gw   = str(voucher.get("payment_id") or "")
    payment_data: dict     = voucher.get("payment_data") or {}
    payment_provider = str(voucher.get("payment_provider") or "MyFatoorah")
    payment_through  = str(voucher.get("payment_through") or "ushspa")
    payment_url      = str(voucher.get("payment_url") or "")
    addons: list    = voucher.get("addons") or []
    extra_time      = voucher.get("extra_time")
    price_for_extra_time = voucher.get("price_for_extra_time")
    recipient_id    = str(voucher.get("recipient_id") or "")
    recipient_phone = str(voucher.get("recipient_phone") or "")
    recipient_data: dict   = voucher.get("recipient_data") or {}
    created_by      = str(voucher.get("created_by") or sender_id or "")
    expire_date     = str(voucher.get("expire_date") or "")

    service_name    = service_data.get("name") or "Gift Voucher"
    sender_name     = sender_data.get("name") or ""
    sender_phone    = sender_data.get("phone_number") or sender_data.get("phone") or ""
    sender_email    = sender_data.get("email") or ""

    print(f"  ✓ Found: {voucher_number} | status={v_status} | amount={total_amount} {currency}")
    print(f"    service={service_name} | provider={payment_provider} | through={payment_through}")

    # ── Step 2: Verify eligibility ───────────────────────────────────────────
    print(f"\n[2/6] Checking eligibility...")
    eligible_statuses = {"active", "scheduled", "delivered", "redeemed", "expired"}
    if v_status not in eligible_statuses:
        raise SystemExit(
            f"[ERROR] Voucher status is '{v_status}'. "
            f"Only {eligible_statuses} are eligible (must have been paid)."
        )
    if not payment_id_gw and not payment_data:
        raise SystemExit(
            "[ERROR] Voucher has no payment_id or payment_data — it may not have been paid."
        )
    print(f"  ✓ Eligible (status={v_status}, payment_id={payment_id_gw or '<in payment_data>'})")

    # ── Step 3: Check for existing invoice (from payment_data JSONB) ─────────
    print(f"\n[3/6] Checking for existing invoice...")

    # The invoice_number and ushanr_invoice_id are stored in gift_voucher.payment_data
    # after a successful ushnotice flow (not as top-level DB columns).
    stored_inv_num: str | None = (
        payment_data.get("invoice_number")
        or payment_data.get("ushanr_invoice_number")
        or None
    )
    stored_inv_id: str | None = (
        payment_data.get("ushanr_invoice_id")
        or None
    )

    existing_invoice_number: str | None = stored_inv_num
    existing_invoice_id: str | None     = stored_inv_id

    if existing_invoice_number:
        print(f"  ✓ Invoice already exists in payment_data: {existing_invoice_number}")
        if existing_invoice_id:
            print(f"    ushanr invoice_id: {existing_invoice_id}")
    else:
        print("  Invoice NOT found in voucher.payment_data — will create.")

    # ── Step 4: Create invoice if missing ────────────────────────────────────
    invoice_result: dict[str, Any] = {}

    if existing_invoice_number:
        print(f"\n[4/6] Invoice already exists ({existing_invoice_number}) — skipping creation")
        invoice_result = {
            "invoice_name": existing_invoice_number,
            "invoice_id": existing_invoice_id or "",
        }
    else:
        print(f"\n[4/6] Creating invoice in ushanr...")

        # 4a. Ensure partner
        print(f"  4a. Ensuring partner (external_id={sender_id or voucher_id})...")
        ensure_partner_payload = {
            "company_id": company_id,
            "external_id": sender_id or voucher_id,
            "name": sender_name,
            "phone": sender_phone,
            "email": sender_email,
            "currency_code": currency,
        }
        if dry_run:
            print(f"  [DRY-RUN] POST {ushanr_url}/api/v1/internal/partners/ensure/")
            print(f"  Payload:\n{json.dumps(ensure_partner_payload, indent=4)}")
            partner_id = "<dry-run-partner-id>"
        else:
            try:
                partner_resp = _post(
                    f"{ushanr_url}/api/v1/internal/partners/ensure/",
                    ensure_partner_payload,
                    headers=ushanr_headers,
                )
                partner_id = str(partner_resp["partner_id"])
                print(f"  ✓ Partner resolved: {partner_id}")
            except Exception as exc:
                raise SystemExit(f"[ERROR] ensure_partner failed: {exc}")

        # 4b. Create invoice
        print(f"  4b. Creating invoice (source_document_id={voucher_id})...")
        invoice_line_name = (
            f"Gift Voucher — {service_name}"
            f"{f' ({voucher_number})' if voucher_number else ''}"
        )
        invoice_payload: dict[str, Any] = {
            "company_id": company_id,
            "partner_id": partner_id,
            "journal_id": journal_id,
            "invoice_date": _today(),
            "source_document_type": "gift_voucher",
            "source_document_id": voucher_id,
            "source_document_ref": voucher_number,
            "currency_code": currency,
            "notes": f"Gift Voucher {voucher_number}",
            "lines": [
                {
                    "account_id": revenue_acct,
                    "name": invoice_line_name,
                    "quantity": "1",
                    "unit_price": total_amount,
                    "discount": "0",
                }
            ],
            "is_paid": True,
            "payment_status": "paid",
            "amount_paid": float(Decimal(total_amount)),
            "payment_id": (
                payment_id_gw
                or str(payment_data.get("payment_id") or "")
            ),
        }
        try:
            invoice_result = _post(
                f"{ushanr_url}/api/v1/internal/invoices/from-source/",
                invoice_payload,
                headers=ushanr_headers,
                dry_run=dry_run,
            )
            if not dry_run:
                inv_name = invoice_result.get("invoice_name") or invoice_result.get("invoice_id")
                print(f"  ✓ Invoice created: {inv_name}")
                print(f"    Response: {json.dumps(invoice_result, default=str)}")
        except Exception as exc:
            raise SystemExit(f"[ERROR] create_invoice failed: {exc}")

    invoice_number: str | None = (
        invoice_result.get("invoice_name")
        or existing_invoice_number
        or None
    )
    invoice_id_ushanr: str | None = (
        invoice_result.get("invoice_id")
        or existing_invoice_id
        or None
    )

    # ── Step 5: Check if payment record exists in DB ─────────────────────────
    print(f"\n[5/6] Checking for existing payment record (voucher_id={voucher_id})...")
    existing_payment = await _db_fetch_payment(db_url, voucher_id)

    if existing_payment:
        pmt_num = existing_payment.get("payment_number")
        pmt_inv = existing_payment.get("invoice_number")
        print(f"  ✓ Payment record already exists: {pmt_num} (invoice_number={pmt_inv})")

        # If payment exists but missing invoice_number — link it
        if invoice_number and not pmt_inv:
            print(f"  ↳ Payment is missing invoice_number — linking {invoice_number}...")
            link_payload = {
                "source_type": "gift_voucher",
                "source_id": voucher_id,
                "invoice_number": invoice_number,
            }
            try:
                link_resp = _post(
                    f"{booknpay_base}/api/v1/payments/link-invoice/",
                    link_payload,
                    headers=booknpay_headers,
                    dry_run=dry_run,
                )
                if not dry_run:
                    print(f"  ✓ Invoice linked: {json.dumps(link_resp, default=str)}")
            except Exception as exc:
                print(f"  [WARN] link-invoice failed (non-fatal): {exc}")
        else:
            print("  Invoice already linked — nothing to do.")
    else:
        print("  Payment record NOT found — will create.")

    # ── Step 6: Create payment record if missing ────────────────────────────
    if existing_payment:
        print(f"\n[6/6] Payment record already exists — skipping creation")
    else:
        print(f"\n[6/6] Creating payment record in ushbooknpay...")

        # 6a. Link invoice first (idempotent, no-op if no payments yet)
        if invoice_number:
            print(f"  6a. Linking invoice {invoice_number} to voucher payments (pre-create)...")
            link_payload = {
                "source_type": "gift_voucher",
                "source_id": voucher_id,
                "invoice_number": invoice_number,
            }
            try:
                link_resp = _post(
                    f"{booknpay_base}/api/v1/payments/link-invoice/",
                    link_payload,
                    headers=booknpay_headers,
                    dry_run=dry_run,
                )
                if not dry_run:
                    print(f"  ✓ Link result: {json.dumps(link_resp, default=str)}")
            except Exception as exc:
                print(f"  [WARN] link-invoice failed (non-fatal): {exc}")

        # 6b. Compute addons price
        addons_price_total: float = 0.0
        for addon in (addons or []):
            try:
                addons_price_total += float((addon or {}).get("price") or 0)
            except (TypeError, ValueError):
                pass
        addons_price_str: str | None = (
            f"{addons_price_total:.3f}" if addons_price_total > 0 else None
        )

        # 6c. Creator snapshot
        created_by_user_data: dict[str, Any] = {
            "id": created_by or sender_id or "",
            "name": sender_name,
            "phone": sender_phone,
            "email": sender_email,
            "user_type": "customer",
            "role": "customer",
        }

        # 6d. Build merged payment_data (preserve all original gateway fields)
        built_payment_data: dict[str, Any] = dict(payment_data) if payment_data else {}
        for k, v in [
            ("payment_id", payment_id_gw),
            ("invoice_number", invoice_number),
            ("ushanr_invoice_id", invoice_id_ushanr),
            ("invoice_value", total_amount),
            ("payment_provider", payment_provider),
            ("payment_through", payment_through),
            ("payment_url", payment_url or None),
        ]:
            if v and k not in built_payment_data:
                built_payment_data[k] = v

        # 6e. Full payment payload — mirrors VoucherActiveHandler._create_payment_record
        payment_payload: dict[str, Any] = {
            # Required
            "customer_id": sender_id or recipient_id or voucher_id,
            "total_amount": total_amount,
            "total_duration": total_duration,
            "currency": currency,
            # Associations
            "voucher_id": voucher_id,
            "voucher_number": voucher_number,
            "booking_id": None,
            # Status & classification
            "status": "success",
            "payment_for": "gift_voucher",
            "payment_provider": payment_provider,
            "payment_through": payment_through,
            "payment_method": str(
                payment_data.get("payment_method")
                or payment_data.get("paymentMethod")
                or "card"
            ),
            # Invoice & transaction identifiers
            "payment_id": payment_id_gw or payment_data.get("payment_id") or "",
            "invoice_number": invoice_number,
            "invoice_value": total_amount,
            "transaction_id": str(
                payment_data.get("transaction_id")
                or payment_data.get("transactionId")
                or payment_id_gw
                or ""
            ) or None,
            "track_id": str(
                payment_data.get("track_id") or payment_data.get("trackId") or ""
            ) or None,
            "invoice_id": invoice_id_ushanr or payment_data.get("invoice_id") or None,
            "reference_id": str(
                payment_data.get("reference_id") or payment_data.get("referenceId") or ""
            ) or None,
            "transaction_date": str(
                payment_data.get("transaction_date")
                or payment_data.get("transactionDate")
                or payment_data.get("paid_at")
                or ""
            ) or None,
            "transaction_status": (
                "success"
                if str(payment_data.get("status", "")).lower() in ("paid", "success")
                else str(payment_data.get("status") or "") or None
            ),
            "payment_url": payment_url or payment_data.get("payment_url") or None,
            # Service & location
            "service_id": service_id or None,
            "service_data": service_data or None,
            "branch_id": branch_id or None,
            "branch_data": branch_data or None,
            "service_arrangement_id": svc_arr_id or None,
            "service_arrangement_data": svc_arr_data or None,
            # Pricing breakdown
            "addons": addons or None,
            "addons_price": addons_price_str,
            "extra_time": extra_time if extra_time else None,
            "price_for_extra_time": (
                str(price_for_extra_time) if price_for_extra_time else None
            ),
            # Recipient
            "recipient_id": recipient_id or None,
            "recipient_phone": recipient_phone or None,
            "recipient_data": recipient_data or None,
            # Sender as customer snapshot
            "sender_id": sender_id or None,
            "sender_data": sender_data or None,
            "customer_data": {
                "name": sender_name,
                "mobile": sender_phone,
                "email": sender_email,
            },
            # Audit / Creator
            "created_by": created_by or sender_id or None,
            "created_by_user": created_by or sender_id or None,
            "created_by_user_data": created_by_user_data,
            # Voucher snapshot
            "voucher_data": {
                "voucher_id": voucher_id,
                "voucher_number": voucher_number,
                "service_id": service_id or "",
                "service_name": service_name,
                "branch_id": branch_id or "",
                "branch_name": branch_data.get("name") or "",
                "expire_date": expire_date,
                "total_amount": total_amount,
                "total_duration": total_duration,
                "recipient_phone": recipient_phone or "",
                "recipient_name": recipient_data.get("name") or "",
                "sender_name": sender_name,
                "sender_phone": sender_phone,
                "addons_price": addons_price_str,
                "price_for_extra_time": (
                    str(price_for_extra_time) if price_for_extra_time else None
                ),
                "invoice_number": invoice_number,
                "invoice_id": invoice_id_ushanr or None,
            },
            # Raw gateway data JSONB
            "payment_data": built_payment_data,
        }

        print(f"  6b. Creating payment record...")
        try:
            pay_result = _post(
                f"{booknpay_base}/api/v1/payments/",
                payment_payload,
                headers=booknpay_headers,
                dry_run=dry_run,
            )
            if not dry_run:
                pay_out = pay_result.get("data") or pay_result
                pmt_num = pay_out.get("payment_number") if isinstance(pay_out, dict) else None
                print(f"  ✓ Payment record created: {pmt_num}")
                print(f"    Response: {json.dumps(pay_result, default=str)[:1000]}")
        except Exception as exc:
            raise SystemExit(f"[ERROR] create_payment failed: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("  REPAIR SUMMARY")
    print(f"  Voucher        : {voucher_number}")
    print(f"  Voucher ID     : {voucher_id}")
    print(f"  Status         : {v_status}")
    print(f"  Invoice        : {invoice_number or '(see above)'}")
    print(f"  Invoice ID     : {invoice_id_ushanr or '(see above)'}")
    print(
        f"  Payment record : "
        + ("ALREADY EXISTS" if existing_payment else "CREATED")
        + (f" ({existing_payment.get('payment_number')})" if existing_payment else "")
    )
    if dry_run:
        print("\n  ⚠ DRY-RUN MODE — No data was written to any service.")
    print("=" * 66)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair missing invoice/payment records for a paid gift voucher.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run (safe, no writes):
  python scripts/repair_voucher_records.py --voucher-number VOU/2026/10/000147 --dry-run

  # Live repair:
  python scripts/repair_voucher_records.py --voucher-number VOU/2026/10/000147

  # Inside Docker container:
  docker exec ushbooknpay python /app/scripts/repair_voucher_records.py \\
      --voucher-number VOU/2026/10/000147 --env /app/.env --dry-run
        """,
    )
    parser.add_argument(
        "--voucher-number", "-v",
        required=True,
        help="Voucher number, e.g. VOU/2026/10/000147",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        default=False,
        help="Show what would be created without making any changes.",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Path to a specific .env file (default: auto-detect).",
    )
    args = parser.parse_args()

    _load_env(args.env)

    try:
        asyncio.run(run(args.voucher_number.strip(), dry_run=args.dry_run))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[FATAL] Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
