"""Loyalty system overhaul — switch to points-based model.

Changes to promotions_loyalty_tracker:
  - Drop columns: service_id, service_arrangement_id, service_name,
                  booking_count, bookings_required, total_bookings,
                  last_recorded_booking_id
  - Rename: total_rewards_earned → total_reward_points
  - Add: points_expire_at (DateTime, nullable)
  - Drop old unique constraint (customer_id, service_id, service_arrangement_id)
  - Add unique constraint on customer_id alone
  - Drop old indexes on service_id, lookup

Changes to promotions_loyalty_reward:
  - Drop columns: service_id, service_arrangement_id, service_name,
                  tracker_id (FK), redeemed_in_booking_id (FK),
                  redeemed_at, expires_at
  - Add: reward_points (Integer, NOT NULL default 0)
  - Add: created_by (String(100), nullable)
  - Migrate status: 'available' → 'added', 'expired' → 'canceled'
  - Drop old indexes on service_id/status

Revision ID: 0036_loyalty_system_overhaul
Revises: 0035_add_gift_from
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0036_loyalty_system_overhaul"
down_revision: Union[str, None] = "0035_add_gift_from"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ─────────────────────────────────────────────────────────────────────────
    # 1. promotions_loyalty_tracker
    # ─────────────────────────────────────────────────────────────────────────
    if "promotions_loyalty_tracker" in tables:
        tracker_cols = {c["name"] for c in inspector.get_columns("promotions_loyalty_tracker")}
        tracker_indexes = {i["name"] for i in inspector.get_indexes("promotions_loyalty_tracker")}
        tracker_uqs = {
            uc["name"]
            for uc in inspector.get_unique_constraints("promotions_loyalty_tracker")
        }

        # Drop old unique constraint (customer_id, service_id, service_arrangement_id)
        if "uq_promotions_tracker_customer_service_arrangement" in tracker_uqs:
            op.drop_constraint(
                "uq_promotions_tracker_customer_service_arrangement",
                "promotions_loyalty_tracker",
                type_="unique",
            )

        # Drop old indexes
        for idx in (
            "ix_promotions_tracker_service_id",
            "ix_promotions_tracker_lookup",
        ):
            if idx in tracker_indexes:
                op.drop_index(idx, table_name="promotions_loyalty_tracker")

        # Drop removed columns
        for col in (
            "service_id",
            "service_arrangement_id",
            "service_name",
            "booking_count",
            "bookings_required",
            "total_bookings",
            "last_recorded_booking_id",
        ):
            if col in tracker_cols:
                op.drop_column("promotions_loyalty_tracker", col)

        # Rename total_rewards_earned → total_reward_points
        if "total_rewards_earned" in tracker_cols and "total_reward_points" not in tracker_cols:
            op.alter_column(
                "promotions_loyalty_tracker",
                "total_rewards_earned",
                new_column_name="total_reward_points",
            )
        elif "total_reward_points" not in tracker_cols:
            op.add_column(
                "promotions_loyalty_tracker",
                sa.Column("total_reward_points", sa.Integer(), nullable=False, server_default="0"),
            )

        # Add points_expire_at
        if "points_expire_at" not in tracker_cols:
            op.add_column(
                "promotions_loyalty_tracker",
                sa.Column("points_expire_at", sa.DateTime(timezone=True), nullable=True),
            )

        # Add unique constraint on customer_id alone
        existing_uqs = {
            uc["name"]
            for uc in inspector.get_unique_constraints("promotions_loyalty_tracker")
        }
        if "uq_promotions_tracker_customer_id" not in existing_uqs:
            op.create_unique_constraint(
                "uq_promotions_tracker_customer_id",
                "promotions_loyalty_tracker",
                ["customer_id"],
            )

        # Add new index on points_expire_at
        existing_idx = {i["name"] for i in inspector.get_indexes("promotions_loyalty_tracker")}
        if "ix_promotions_tracker_points_expire_at" not in existing_idx:
            op.create_index(
                "ix_promotions_tracker_points_expire_at",
                "promotions_loyalty_tracker",
                ["points_expire_at"],
            )

    # ─────────────────────────────────────────────────────────────────────────
    # 2. promotions_loyalty_reward
    # ─────────────────────────────────────────────────────────────────────────
    if "promotions_loyalty_reward" in tables:
        reward_cols = {c["name"] for c in inspector.get_columns("promotions_loyalty_reward")}
        reward_indexes = {i["name"] for i in inspector.get_indexes("promotions_loyalty_reward")}
        reward_fks = {fk["name"] for fk in inspector.get_foreign_keys("promotions_loyalty_reward")}

        # Drop old FK constraints before dropping columns
        # tracker_id FK
        for fk in inspector.get_foreign_keys("promotions_loyalty_reward"):
            if fk.get("constrained_columns") == ["tracker_id"]:
                if fk.get("name"):
                    op.drop_constraint(fk["name"], "promotions_loyalty_reward", type_="foreignkey")

        # redeemed_in_booking_id FK
        for fk in inspector.get_foreign_keys("promotions_loyalty_reward"):
            if fk.get("constrained_columns") == ["redeemed_in_booking_id"]:
                if fk.get("name"):
                    op.drop_constraint(fk["name"], "promotions_loyalty_reward", type_="foreignkey")

        # Drop old indexes
        for idx in (
            "ix_promotions_reward_service_status",
            "ix_promotions_reward_expires_at",
        ):
            if idx in reward_indexes:
                op.drop_index(idx, table_name="promotions_loyalty_reward")

        # Drop removed columns
        for col in (
            "service_id",
            "service_arrangement_id",
            "service_name",
            "tracker_id",
            "redeemed_in_booking_id",
            "redeemed_at",
            "expires_at",
        ):
            if col in reward_cols:
                op.drop_column("promotions_loyalty_reward", col)

        # Add reward_points
        if "reward_points" not in reward_cols:
            op.add_column(
                "promotions_loyalty_reward",
                sa.Column("reward_points", sa.Integer(), nullable=False, server_default="0"),
            )

        # Add created_by
        if "created_by" not in reward_cols:
            op.add_column(
                "promotions_loyalty_reward",
                sa.Column("created_by", sa.String(100), nullable=True),
            )

        # Add new indexes if missing
        existing_idx = {i["name"] for i in inspector.get_indexes("promotions_loyalty_reward")}
        if "ix_promotions_reward_customer_created" not in existing_idx:
            op.create_index(
                "ix_promotions_reward_customer_created",
                "promotions_loyalty_reward",
                ["customer_id", "created_at"],
            )
        if "ix_promotions_reward_booking" not in existing_idx:
            op.create_index(
                "ix_promotions_reward_booking",
                "promotions_loyalty_reward",
                ["earned_from_booking_id"],
            )

        # Migrate status values: 'available' → 'added', 'expired' → 'canceled'
        op.execute(
            "UPDATE promotions_loyalty_reward SET status = 'added' WHERE status = 'available'"
        )
        op.execute(
            "UPDATE promotions_loyalty_reward SET status = 'canceled' WHERE status = 'expired'"
        )


def downgrade() -> None:
    # Downgrade is intentionally minimal — restoring dropped columns from a
    # data-destructive migration is not safe.  Re-run the previous migration
    # if a full rollback is needed.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "promotions_loyalty_reward" in tables:
        reward_cols = {c["name"] for c in inspector.get_columns("promotions_loyalty_reward")}
        if "reward_points" in reward_cols:
            op.drop_column("promotions_loyalty_reward", "reward_points")
        if "created_by" in reward_cols:
            op.drop_column("promotions_loyalty_reward", "created_by")

    if "promotions_loyalty_tracker" in tables:
        tracker_cols = {c["name"] for c in inspector.get_columns("promotions_loyalty_tracker")}
        tracker_uqs = {
            uc["name"]
            for uc in inspector.get_unique_constraints("promotions_loyalty_tracker")
        }
        if "uq_promotions_tracker_customer_id" in tracker_uqs:
            op.drop_constraint(
                "uq_promotions_tracker_customer_id",
                "promotions_loyalty_tracker",
                type_="unique",
            )
        if "points_expire_at" in tracker_cols:
            op.drop_column("promotions_loyalty_tracker", "points_expire_at")
