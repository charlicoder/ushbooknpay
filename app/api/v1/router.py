"""
app/api/v1/router.py
─────────────────────
Main v1 API router — assembles all sub-routers.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import availability, bookings, payments
from app.promotions.api.router import router as promotions_router
from app.voucher.api.router import (
    list_my_received_vouchers,
    list_my_sent_vouchers,
    router as vouchers_router,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(availability.router)
api_router.include_router(bookings.router)
api_router.include_router(payments.router)
api_router.include_router(promotions_router)
api_router.include_router(vouchers_router)

api_router.add_api_route(
    "/my-bookings/",
    bookings.list_my_bookings,
    methods=["GET"],
    tags=["Bookings"],
    summary="List my bookings (private)",
    description="Private endpoint to list bookings for the authenticated user only. Requires user token.",
)
api_router.add_api_route(
    "/my-bookings",
    bookings.list_my_bookings,
    methods=["GET"],
    tags=["Bookings"],
    include_in_schema=False,
)

api_router.add_api_route(
    "/my-vouchers/",
    list_my_received_vouchers,
    methods=["GET"],
    tags=["Gift Vouchers"],
    summary="List my received gift vouchers (private)",
    description="Private endpoint to list gift vouchers where recipient phone matches requester phone. Requires user token.",
)
api_router.add_api_route(
    "/my-vouchers",
    list_my_received_vouchers,
    methods=["GET"],
    tags=["Gift Vouchers"],
    include_in_schema=False,
)

api_router.add_api_route(
    "/my-sent-vouchers/",
    list_my_sent_vouchers,
    methods=["GET"],
    tags=["Gift Vouchers"],
    summary="List my sent gift vouchers (private)",
    description="Private endpoint to list gift vouchers where requester is the sender. Requires user token.",
)
api_router.add_api_route(
    "/my-sent-vouchers",
    list_my_sent_vouchers,
    methods=["GET"],
    tags=["Gift Vouchers"],
    include_in_schema=False,
)


@api_router.get("/health/", tags=["Operations"], summary="Health check")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ushbooknpay"}


@api_router.get("/ready/", tags=["Operations"], summary="Readiness probe")
async def ready() -> dict[str, str]:
    """Check that database and Redis are reachable."""
    return {"status": "ready"}
