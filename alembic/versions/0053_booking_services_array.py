"""Multi-service bookings: add bookings.services JSONB array.

Each booking can now contain several services (same branch + therapist).
The legacy single-service columns are kept and mirror ``services[0]``.

Revision ID: 0053_booking_services_array
Revises: 0052_refund_lifecycle_columns
Create Date: 2026-10-24
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053_booking_services_array"
down_revision: Union[str, None] = "0052_refund_lifecycle_columns"
branch_labels = None
depends_on = None

_INDEX = "ix_bookings_services_gin"
_CHECK = "ck_bookings_services_array"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bookings" not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns("bookings")}

    # 1. Nullable-with-default column first (fast, metadata-only on PG >= 11).
    if "services" not in cols:
        op.add_column(
            "bookings",
            sa.Column(
                "services",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    # 2. Set-based backfill from the legacy single-service columns.
    op.execute(
        sa.text(
            """
            UPDATE bookings
            SET services = jsonb_build_array(
                jsonb_build_object(
                    'service_id', service_id::text,
                    'service_data', COALESCE(service_data, '{}'::jsonb),
                    'service_arrangement_id', service_arrangement_id::text,
                    'service_arrangement_data', COALESCE(service_arrangement_data, '{}'::jsonb),
                    'base_price', to_char(COALESCE(base_price, 0), 'FM999999990.000'),
                    'duration_minutes', COALESCE(duration_minutes, 0),
                    'position', 0
                )
            )
            WHERE services = '[]'::jsonb
            """
        )
    )

    # 3. Constraint added NOT VALID then validated (short lock).
    checks = {c["name"] for c in inspector.get_check_constraints("bookings")}
    if _CHECK not in checks:
        op.execute(
            sa.text(
                f"ALTER TABLE bookings ADD CONSTRAINT {_CHECK} CHECK "
                "(jsonb_typeof(services) = 'array' "
                "AND jsonb_array_length(services) BETWEEN 1 AND 10) NOT VALID"
            )
        )
        op.execute(sa.text(f"ALTER TABLE bookings VALIDATE CONSTRAINT {_CHECK}"))

    # 4. GIN index for containment lookups.
    op.execute(
        sa.text(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            "ON bookings USING gin (services jsonb_path_ops)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "bookings" not in inspector.get_table_names():
        return

    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
    op.execute(sa.text(f"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS {_CHECK}"))

    cols = {c["name"] for c in inspector.get_columns("bookings")}
    if "services" in cols:
        op.drop_column("bookings", "services")
