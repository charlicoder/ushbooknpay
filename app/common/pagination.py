"""
app/common/pagination.py
────────────────────────
Standard pagination response envelope matching the ushauth API convention:

{
    "success": true,
    "data": [...],
    "meta": {
        "pagination": {
            "count": 100,
            "total_pages": 10,
            "current_page": 1,
            "page_size": 10,
            "next": "...",
            "previous": null
        }
    }
}
"""

from __future__ import annotations

import math
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginationMeta(BaseModel):
    count: int
    total_pages: int
    current_page: int
    page_size: int
    next: str | None = None
    previous: str | None = None


class MetaWrapper(BaseModel):
    pagination: PaginationMeta


class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    data: list[T]
    meta: MetaWrapper


def make_paginated_response(
    items: list[T],
    *,
    count: int,
    page: int,
    page_size: int,
    base_url: str = "",
) -> PaginatedResponse[T]:
    """
    Construct a paginated response envelope.

    Args:
        items: The current page of items.
        count: Total number of items across all pages.
        page: The current 1-indexed page number.
        page_size: Items per page.
        base_url: Base URL for generating next/previous links.
    """
    total_pages = max(1, math.ceil(count / page_size)) if page_size > 0 else 1

    def _page_url(p: int) -> str | None:
        if not base_url:
            return None
        return f"{base_url}?page={p}&page_size={page_size}"

    return PaginatedResponse(
        success=True,
        data=items,
        meta=MetaWrapper(
            pagination=PaginationMeta(
                count=count,
                total_pages=total_pages,
                current_page=page,
                page_size=page_size,
                next=_page_url(page + 1) if page < total_pages else None,
                previous=_page_url(page - 1) if page > 1 else None,
            )
        ),
    )
