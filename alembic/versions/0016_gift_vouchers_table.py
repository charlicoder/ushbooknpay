"""Create gift_vouchers table.

Revision ID: 0016_gift_vouchers_table
Revises: 0015_payment_voucher_fields
Create Date: 2026-09-04 00:00:00.000000

"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016_gift_vouchers_table"
down_revision: Union[str, None] = "0015_payment_voucher_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" in tables:
        # Already applied — idempotent guard
        return

    op.create_table(
        "gift_vouchers",

        # ── Identity ─────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),

        # ── Service & branch snapshot (external refs — no FK) ─────────────────
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("branch_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("service_arrangement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "service_arrangement_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),

        # ── Add-ons & timing ──────────────────────────────────────────────────
        sa.Column("addons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extra_time", sa.Integer(), nullable=False, server_default="0"),

        # ── Lifecycle ─────────────────────────────────────────────────────────
        sa.Column(
            "expire_date",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="created",
        ),

        # ── Sender (buyer) ────────────────────────────────────────────────────
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),

        # ── Recipient ─────────────────────────────────────────────────────────
        sa.Column("recipient_phone", sa.String(50), nullable=True),
        sa.Column(
            "recipient_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),

        # ── Authoring ─────────────────────────────────────────────────────────
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),

        # ── Financial ─────────────────────────────────────────────────────────
        sa.Column("total_duration", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KWD"),

        # ── Personalisation ───────────────────────────────────────────────────
        sa.Column("gift_message", sa.Text(), nullable=True),
        sa.Column("gift_template", sa.String(100), nullable=True),

        # ── Security tokens ───────────────────────────────────────────────────
        sa.Column("secret_code", sa.String(6), nullable=False),
        sa.Column("public_token", sa.String(64), nullable=False),

        # ── Redemption ────────────────────────────────────────────────────────
        sa.Column("redeemed_booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),

        # ── Cross-references ──────────────────────────────────────────────────
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),

        # ── Audit timestamps ──────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),

        # ── Constraints ───────────────────────────────────────────────────────
        sa.UniqueConstraint("public_token", name="uq_gift_vouchers_public_token"),

        # ── FK to bookings (SET NULL on delete) ───────────────────────────────
        sa.ForeignKeyConstraint(
            ["redeemed_booking_id"],
            ["bookings.id"],
            name="fk_gift_vouchers_redeemed_booking_id_bookings",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name="fk_gift_vouchers_booking_id_bookings",
            ondelete="SET NULL",
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index("ix_gift_vouchers_status", "gift_vouchers", ["status"])
    op.create_index("ix_gift_vouchers_sender_id", "gift_vouchers", ["sender_id"])
    op.create_index("ix_gift_vouchers_recipient_phone", "gift_vouchers", ["recipient_phone"])
    op.create_index("ix_gift_vouchers_expire_date", "gift_vouchers", ["expire_date"])
    op.create_index("ix_gift_vouchers_payment_id", "gift_vouchers", ["payment_id"])
    op.create_index("ix_gift_vouchers_service_id", "gift_vouchers", ["service_id"])
    op.create_index("ix_gift_vouchers_created_at", "gift_vouchers", ["created_at"])
    op.create_index(
        "ix_gift_vouchers_sender_status",
        "gift_vouchers",
        ["sender_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "gift_vouchers" not in tables:
        return

    # Drop indexes first
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("gift_vouchers")}
    for idx_name in [
        "ix_gift_vouchers_sender_status",
        "ix_gift_vouchers_created_at",
        "ix_gift_vouchers_service_id",
        "ix_gift_vouchers_payment_id",
        "ix_gift_vouchers_expire_date",
        "ix_gift_vouchers_recipient_phone",
        "ix_gift_vouchers_sender_id",
        "ix_gift_vouchers_status",
    ]:
        if idx_name in existing_indexes:
            op.drop_index(idx_name, table_name="gift_vouchers")

    op.drop_table("gift_vouchers")
