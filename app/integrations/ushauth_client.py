"""
app/integrations/ushauth_client.py
────────────────────────────────────
HTTP client for the ushauth microservice.

All catalog data (branches, services, therapists, schedules) is fetched
through the API Gateway using the USHSPA_TOKEN for service-to-service auth.

Caching strategy:
- Branch, Service, Therapist catalog data: cached in Redis for CATALOG_CACHE_TTL seconds.
- Working hour schedules: per-therapist, shorter TTL.
- Cache-aside pattern: check cache → fetch from API on miss → populate cache.

Performance:
- Pagination is followed automatically.
- Concurrent page fetching is supported for large catalogs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AuthorizationError,
    GatewayError,
    GatewayTimeoutError,
    NotFoundError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class USHAuthClient:
    """
    Async HTTP client for ushauth API.

    Designed for dependency injection — instantiate once per request scope
    or as a singleton. Uses a shared httpx.AsyncClient for connection pooling.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        redis_client: Any,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._redis = redis_client
        self._settings = settings or get_settings()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "X-USHSPA-TOKEN": self._settings.USHSPA_TOKEN,
            "USHSPA-TOKEN": self._settings.USHSPA_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """Execute a GET request to ushauth and return parsed JSON."""
        url = f"{self._settings.ushauth_base_url.rstrip('/')}{path}"
        try:
            response = await self._http.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self._settings.GATEWAY_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            logger.error("ushauth_request_timeout", url=url, error=str(exc))
            raise GatewayTimeoutError(f"ushauth request timed out: {path}")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ushauth_http_error",
                url=url,
                status=exc.response.status_code,
                detail=exc.response.text[:200],
            )
            if exc.response.status_code == 404:
                raise NotFoundError(
                    f"Resource not found in ushauth: {path}",
                    detail=exc.response.text[:500],
                )
            if exc.response.status_code in (401, 403):
                raise AuthorizationError(
                    f"ushauth authentication/authorization failed for {path}",
                    detail=exc.response.text[:500],
                )
            raise GatewayError(
                f"ushauth returned {exc.response.status_code} for {path}",
                detail=exc.response.text[:500],
            )
    async def _post(self, path: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a POST request to ushauth and return parsed JSON."""
        url = f"{self._settings.ushauth_base_url.rstrip('/')}{path}"
        try:
            response = await self._http.post(
                url,
                headers=self._headers(),
                json=json_data,
                timeout=self._settings.GATEWAY_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            logger.error("ushauth_request_timeout", url=url, error=str(exc))
            raise GatewayTimeoutError(f"ushauth request timed out: {path}")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ushauth_http_error",
                url=url,
                status=exc.response.status_code,
                detail=exc.response.text[:200],
            )
            if exc.response.status_code == 404:
                raise NotFoundError(
                    f"Resource not found in ushauth: {path}",
                    detail=exc.response.text[:500],
                )
            if exc.response.status_code in (401, 403):
                raise AuthorizationError(
                    f"ushauth authentication/authorization failed for {path}",
                    detail=exc.response.text[:500],
                )
            raise GatewayError(
                f"ushauth returned {exc.response.status_code} for {path}",
                detail=exc.response.text[:500],
            )

    async def _get_cached(
        self, cache_key: str, path: str, params: dict | None = None
    ) -> dict[str, Any]:
        """Cache-aside GET: check Redis first, then fetch and store."""
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as exc:
            logger.warning("cache_miss_redis_error", key=cache_key, error=str(exc))

        data = await self._get(path, params)

        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(data),
            )
        except Exception as exc:
            logger.warning("cache_set_redis_error", key=cache_key, error=str(exc))

        return data

    async def _get_all_pages(self, path: str, params: dict | None = None) -> list[Any]:
        """Fetch all pages from a paginated ushauth endpoint."""
        params = dict(params or {})
        params.setdefault("page_size", 100)

        first_page = await self._get(path, params)
        items: list[Any] = list(first_page.get("data", []))

        meta = first_page.get("meta", {})
        pagination = meta.get("pagination", {})
        total_pages: int = pagination.get("total_pages", 1)

        if total_pages > 1:
            # Fetch remaining pages concurrently
            tasks = [
                self._get(path, {**params, "page": page})
                for page in range(2, total_pages + 1)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error("ushauth_page_fetch_error", error=str(result))
                    continue
                items.extend(result.get("data", []))  # type: ignore[union-attr]

        return items

    # ── Branch API ────────────────────────────────────────────────────────

    async def get_branch(self, branch_id: str) -> dict[str, Any]:
        """Fetch a single branch by ID (cached)."""
        cache_key = f"branch:{branch_id}"
        return await self._get_cached(cache_key, f"/api/v1/branches/{branch_id}/")

    async def list_branches(self) -> list[dict[str, Any]]:
        """Fetch all active branches (cached full list)."""
        cache_key = "branches:all"
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        items = await self._get_all_pages("/api/v1/branches/")

        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(items),
            )
        except Exception:
            pass

        return items

    # ── Service API ───────────────────────────────────────────────────────

    async def get_service(self, service_id: str) -> dict[str, Any]:
        """Fetch a single service by ID (cached)."""
        cache_key = f"service:{service_id}"
        return await self._get_cached(cache_key, f"/api/v1/services/{service_id}/")

    async def list_services(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch all services, optionally filtered by branch."""
        params = {}
        if branch_id:
            params["branch"] = branch_id
        cache_key = f"services:branch:{branch_id or 'all'}"

        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        items = await self._get_all_pages("/api/v1/services/", params)

        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(items),
            )
        except Exception:
            pass

        return items

    async def list_service_arrangements(self, service_id: str) -> list[dict[str, Any]]:
        """Fetch arrangements for a service (room types + pricing)."""
        cache_key = f"service:{service_id}:arrangements"
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        items = await self._get_all_pages(
            f"/api/v1/services/{service_id}/arrangements/"
        )
        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(items),
            )
        except Exception:
            pass

        return items

    async def get_service_arrangement(self, arrangement_id: str) -> dict[str, Any]:
        """Fetch a single service arrangement by ID (cached)."""
        cache_key = f"arrangement:{arrangement_id}"
        return await self._get_cached(
            cache_key, f"/api/v1/service-arrangements/{arrangement_id}/"
        )

    # ── Therapist API ─────────────────────────────────────────────────────

    async def get_therapist(self, therapist_id: str) -> dict[str, Any]:
        """Fetch therapist profile with schedule (cached)."""
        cache_key = f"therapist:{therapist_id}"
        return await self._get_cached(
            cache_key, f"/api/v1/therapists/{therapist_id}/"
        )

    async def list_therapists_for_service(
        self, service_id: str
    ) -> list[dict[str, Any]]:
        """List therapists qualified for a given service (cached)."""
        cache_key = f"service:{service_id}:therapists"
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        items = await self._get_all_pages(
            f"/api/v1/services/{service_id}/therapists/"
        )
        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(items),
            )
        except Exception:
            pass

        return items

    async def list_therapists(
        self,
        branch_id: str | None = None,
        service_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List therapists filtered by branch and/or service (cached)."""
        params: dict[str, str] = {}
        if branch_id:
            params["branch"] = str(branch_id)
        if service_id:
            params["service"] = str(service_id)

        cache_key = f"therapists:branch:{branch_id or 'all'}:service:{service_id or 'all'}"
        try:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        items = await self._get_all_pages("/api/v1/therapists/", params)
        try:
            await self._redis.setex(
                cache_key,
                self._settings.CATALOG_CACHE_TTL_SECONDS,
                json.dumps(items),
            )
        except Exception:
            pass

        return items

    # ── Customer API ──────────────────────────────────────────────────────

    async def get_customer_profile(self, customer_id: str) -> dict[str, Any]:
        """Fetch customer profile (no cache — always fresh)."""
        return await self._get(f"/api/v1/customers/{customer_id}/")

    async def validate_token_and_get_user(self, token: str) -> dict[str, Any]:
        """
        Validate token and retrieve customer profile via GET /api/v1/customers/me/.
        """
        url = f"{self._settings.ushauth_base_url.rstrip('/')}/api/v1/customers/me/"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-USHSPA-TOKEN": self._settings.USHSPA_TOKEN,
            "USHSPA-TOKEN": self._settings.USHSPA_TOKEN,
            "Accept": "application/json",
        }
        try:
            response = await self._http.get(
                url,
                headers=headers,
                timeout=self._settings.GATEWAY_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            logger.error("ushauth_auth_timeout", url=url, error=str(exc))
            raise GatewayTimeoutError("ushauth auth validation timed out.")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ushauth_auth_http_error",
                url=url,
                status=exc.response.status_code,
                detail=exc.response.text[:200],
            )
            raise GatewayError(
                f"ushauth returned {exc.response.status_code} for customer profile validation",
                detail=exc.response.text[:500],
            )

    # ── Availability API ──────────────────────────────────────────────────

    async def check_appointment_availability(
        self,
        service_arrangement_id: str,
        appointment_date: str,
        appointment_time: str,
        duration: int,
        therapist_id: str | None = None,
        therapist_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Check appointment availability in ushauth microservice.
        POST /api/v1/check-appointment-availability/

        appointment_date must be YYYY-MM-DD.
        appointment_time must be HH:MM or HH:MM:SS.
        """
        import re as _re

        # ── Final safety-net: sanitize date (strip any T... time portion)
        clean_date = str(appointment_date).split("T")[0].strip()

        # ── Final safety-net: sanitize time (strip millis + tz suffix)
        clean_time = str(appointment_time).strip()
        if clean_time.endswith("Z"):
            clean_time = clean_time[:-1]
        clean_time = _re.sub(r"[+-]\d{2}:\d{2}$", "", clean_time)
        clean_time = clean_time.split(".")[0]
        if len(clean_time.split(":")) < 3:
            clean_time = f"{clean_time}:00"

        payload: dict[str, Any] = {
            "service_arrangement_id": str(service_arrangement_id),
            "appointment_date": clean_date,
            "appointment_time": clean_time,
            "duration": int(duration),
        }
        if therapist_id:
            payload["therapist_id"] = str(therapist_id)
        if therapist_ids:
            payload["therapist_ids"] = [str(tid) for tid in therapist_ids]

        return await self._post("/api/v1/check-appointment-availability/", json_data=payload)

    async def check_booking_therapist_availability(
        self,
        therapist_id: str,
        appointment_date: str,
        appointment_time: str,
        duration: int,
    ) -> dict[str, Any]:
        """
        Check therapist availability for home-service bookings.
        POST /api/v1/check-booking-therapists-availability/

        Queries TherapistBookingCache for conflicts. Does NOT check service arrangement.
        Returns {"available": "yes"|"no", "therapist_id": "...", ...}

        appointment_date must be YYYY-MM-DD.
        appointment_time must be HH:MM or HH:MM:SS.
        """
        import re as _re

        # Sanitize date
        clean_date = str(appointment_date).split("T")[0].strip()

        # Sanitize time (strip milliseconds + timezone suffix)
        clean_time = str(appointment_time).strip()
        if clean_time.endswith("Z"):
            clean_time = clean_time[:-1]
        clean_time = _re.sub(r"[+-]\d{2}:\d{2}$", "", clean_time)
        clean_time = clean_time.split(".")[0]
        if len(clean_time.split(":")) < 3:
            clean_time = f"{clean_time}:00"

        payload: dict[str, Any] = {
            "therapist_id": str(therapist_id),
            "appointment_date": clean_date,
            "appointment_time": clean_time,
            "duration": int(duration),
        }

        return await self._post("/api/v1/check-booking-therapists-availability/", json_data=payload)


    async def create_appointment_cache(
        self,
        service_arrangement_id: str,
        therapist_id: str,
        booking_id: str,
        appointment_date: str,
        appointment_time: str,
        booking_type: str = "branch",
        duration: int = 60,
        status: str = "confirmed",
    ) -> dict[str, Any]:
        """
        Create appointment cache records in ushauth (ush_spa_appointment_cache and ush_therapist_booking_cache).
        POST /api/v1/create-appointment-cache/
        """
        payload = {
            "service_arrangement_id": str(service_arrangement_id),
            "therapist_id": str(therapist_id),
            "booking_id": str(booking_id),
            "appointment_date": str(appointment_date),
            "appointment_time": str(appointment_time),
            "booking_type": str(booking_type),
            "duration": int(duration),
            "status": str(status),
        }
        return await self._post("/api/v1/create-appointment-cache/", json_data=payload)
