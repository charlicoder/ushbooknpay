"""
app/loyalty
────────────
Points-based loyalty programme.

Organised in clean layers (SOLID / DDD):
  domain/         — pure business logic, no framework dependencies
  infrastructure/ — SQLAlchemy ORM models and async repository
  application/    — use-case orchestration (LoyaltyService)
  interfaces/     — Pydantic v2 request / response schemas
  api/            — FastAPI router and dependency helpers
"""
