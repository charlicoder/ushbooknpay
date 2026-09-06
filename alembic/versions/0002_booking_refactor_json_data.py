"""
0002_booking_refactor_json_data
────────────────────────────────
Refactor booking model:
- Store entity snapshots in JSONB fields (customer_data, branch_data, service_data, service_arrangement_data, therapist_data).
- Add extra_minutes and price_for_extra_minutes.
- Make therapist_id non-nullable (mandatory).
- Exclude service base_price (overridden by arrangement price).
- Remove redundant snapshot columns (customer_name, customer_phone, etc.).
- Remove service_type and home_* address fields.

Revision ID: 0002_booking_refactor_json_data
Revises: 0001_initial_schema
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0002_booking_refactor_json_data"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        booking_cols = {c["name"] for c in inspector.get_columns("bookings")}

        # ── 1. Add JSON data snapshot columns ─────────────────────────────────
        json_cols = [
            "customer_data",
            "branch_data",
            "service_data",
            "service_arrangement_data",
            "therapist_data",
        ]
        for col_name in json_cols:
            if col_name not in booking_cols:
                op.add_column(
                    "bookings",
                    sa.Column(
                        col_name,
                        postgresql.JSONB(astext_type=sa.Text()),
                        nullable=True,
                        server_default=sa.text("'{}'::jsonb"),
                    ),
                )

        # ── 2. Add extra minutes and price columns ────────────────────────────
        if "extra_minutes" not in booking_cols:
            op.add_column(
                "bookings",
                sa.Column(
                    "extra_minutes",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        if "price_for_extra_minutes" not in booking_cols:
            op.add_column(
                "bookings",
                sa.Column(
                    "price_for_extra_minutes",
                    sa.Numeric(precision=10, scale=3),
                    nullable=False,
                    server_default="0.000",
                ),
            )

        # ── 3. Make therapist_id mandatory ────────────────────────────────────
        for c in inspector.get_columns("bookings"):
            if c["name"] == "therapist_id" and c.get("nullable", True):
                op.alter_column(
                    "bookings",
                    "therapist_id",
                    existing_type=postgresql.UUID(as_uuid=True),
                    nullable=False,
                )

        # ── 4. Drop legacy name, home, and base_price columns ─────────────────
        legacy_cols = [
            "base_price",
            "customer_name",
            "customer_phone",
            "customer_email",
            "branch_name",
            "service_name",
            "arrangement_name",
            "therapist_name",
            "service_type",
            "home_address_line1",
            "home_address_line2",
            "home_city",
            "home_country",
            "home_latitude",
            "home_longitude",
        ]
        for lcol in legacy_cols:
            if lcol in booking_cols:
                op.drop_column("bookings", lcol)

    if "temporary_holds" in tables:
        for c in inspector.get_columns("temporary_holds"):
            if c["name"] == "therapist_id" and c.get("nullable", True):
                op.alter_column(
                    "temporary_holds",
                    "therapist_id",
                    existing_type=postgresql.UUID(as_uuid=True),
                    nullable=False,
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "bookings" in tables:
        booking_cols = {c["name"] for c in inspector.get_columns("bookings")}

        # ── Re-add legacy columns ─────────────────────────────────────────────
        if "base_price" not in booking_cols:
            op.add_column("bookings", sa.Column("base_price", sa.Numeric(precision=10, scale=3), nullable=False, server_default="0.000"))
        if "customer_name" not in booking_cols:
            op.add_column("bookings", sa.Column("customer_name", sa.String(200), nullable=True))
        if "customer_phone" not in booking_cols:
            op.add_column("bookings", sa.Column("customer_phone", sa.String(30), nullable=True))
        if "customer_email" not in booking_cols:
            op.add_column("bookings", sa.Column("customer_email", sa.String(254), nullable=True))
        if "branch_name" not in booking_cols:
            op.add_column("bookings", sa.Column("branch_name", sa.String(200), nullable=True))
        if "service_name" not in booking_cols:
            op.add_column("bookings", sa.Column("service_name", sa.String(200), nullable=True))
        if "arrangement_name" not in booking_cols:
            op.add_column("bookings", sa.Column("arrangement_name", sa.String(200), nullable=True))
        if "therapist_name" not in booking_cols:
            op.add_column("bookings", sa.Column("therapist_name", sa.String(200), nullable=True))
        if "service_type" not in booking_cols:
            op.add_column("bookings", sa.Column("service_type", sa.String(20), nullable=False, server_default="branch"))
        if "home_address_line1" not in booking_cols:
            op.add_column("bookings", sa.Column("home_address_line1", sa.String(255), nullable=True))
        if "home_address_line2" not in booking_cols:
            op.add_column("bookings", sa.Column("home_address_line2", sa.String(255), nullable=True))
        if "home_city" not in booking_cols:
            op.add_column("bookings", sa.Column("home_city", sa.String(100), nullable=True))
        if "home_country" not in booking_cols:
            op.add_column("bookings", sa.Column("home_country", sa.String(2), nullable=True))
        if "home_latitude" not in booking_cols:
            op.add_column("bookings", sa.Column("home_latitude", sa.Numeric(precision=10, scale=7), nullable=True))
        if "home_longitude" not in booking_cols:
            op.add_column("bookings", sa.Column("home_longitude", sa.Numeric(precision=11, scale=7), nullable=True))

        for c in inspector.get_columns("bookings"):
            if c["name"] == "therapist_id" and not c.get("nullable", True):
                op.alter_column(
                    "bookings",
                    "therapist_id",
                    existing_type=postgresql.UUID(as_uuid=True),
                    nullable=True,
                )

        # ── Drop new columns ──────────────────────────────────────────────────
        if "price_for_extra_minutes" in booking_cols:
            op.drop_column("bookings", "price_for_extra_minutes")
        if "extra_minutes" in booking_cols:
            op.drop_column("bookings", "extra_minutes")
        for col_name in ["therapist_data", "service_arrangement_data", "service_data", "branch_data", "customer_data"]:
            if col_name in booking_cols:
                op.drop_column("bookings", col_name)

    if "temporary_holds" in tables:
        for c in inspector.get_columns("temporary_holds"):
            if c["name"] == "therapist_id" and not c.get("nullable", True):
                op.alter_column(
                    "temporary_holds",
                    "therapist_id",
                    existing_type=postgresql.UUID(as_uuid=True),
                    nullable=True,
                )
