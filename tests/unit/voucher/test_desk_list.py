"""Desk voucher list: filter building / SQL compilation (no database needed)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.voucher.api.router import _desk_filters
from app.voucher.application import desk_list as d


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}))


def test_six_or_more_statuses_means_no_status_filter():
    all_seven = "scheduled,pending,redeemed,partially_redeemed,active,expired,cancelled"
    assert _desk_filters(all_seven, None, None, None, None, None, None, None).statuses == []
    assert _desk_filters("active,expired", None, None, None, None, None, None, None).statuses == ["active", "expired"]
    assert _desk_filters(None, None, None, None, None, None, None, None).statuses == []


def test_no_filters_builds_no_conditions():
    assert d.build_conditions(d.DeskFilters()) == []


def test_all_filters_compile_for_list_and_report_views():
    f = d.DeskFilters(
        statuses=["active", "redeemed", "pending", "partially_redeemed"],
        channel="ushweb",
        created_by="__online__",
        q="Sara 965-123",
        created_from="2026-11-01",
        created_to="2026-11-09",
        redeemed_from="2026-11-01",
        redeemed_to="2026-11-09",
    )
    conds = d.build_conditions(f)
    assert len(conds) == 7
    for view in ("list", "report"):
        sql = _sql(select(*d._slim_columns(view)).where(*conds))
        assert "gift_vouchers" in sql


def test_list_view_selects_slim_columns_only():
    sql = _sql(select(*d._slim_columns("list")))
    for heavy in ("payment_data", "update_history", "booking_data", "delivery_address", "ordered_items", "redemptions"):
        assert heavy not in sql.split("FROM")[0], heavy


def test_invalid_created_by_matches_nothing():
    conds = d.build_conditions(d.DeskFilters(created_by="not-a-uuid"))
    assert "false" in _sql(select(d.G.id).where(*conds)).lower()


def test_search_digits_filter_only_for_three_or_more_digits():
    short = _sql(select(d.G.id).where(d._search_clause("ab 12")))
    long = _sql(select(d.G.id).where(d._search_clause("965123")))
    assert "regexp_replace" not in short
    assert "regexp_replace" in long


def test_period_window_is_created_or_redeemed():
    conds = d.build_conditions(d.DeskFilters(period_from="2026-11-01", period_to="2026-11-09"))
    assert len(conds) == 1
    sql = _sql(select(d.G.id).where(*conds))
    assert " OR " in sql and "created_at" in sql
