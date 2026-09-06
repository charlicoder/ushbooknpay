# ushbooknpay — USHSPA Booking & Payment Microservice

Production-ready microservice combining **appointment booking** and **payment processing** for the USHSPA mobile application.

---

## Architecture Overview

```
app/
├── core/              # Cross-cutting concerns (config, DB, security, middleware, logging)
├── common/            # Shared utilities (pagination, Redis client, types, utils)
├── booking/           # Booking domain
│   ├── domain/        # Value objects, state machine, business rules (pure, no I/O)
│   ├── infrastructure/ # SQLAlchemy models, repository
│   ├── application/   # Use-case services
│   └── interfaces/    # Pydantic schemas, API mappers
├── payment/           # Payment domain
│   ├── domain/        # Value objects, state machine, gateway Protocol
│   ├── infrastructure/ # Models, repository, provider implementations
│   ├── application/   # Payment use-case services
│   └── interfaces/    # Pydantic schemas, webhook handlers
├── availability/      # Availability engine (pure computation)
│   └── domain/        # Interval engine (O(n log n) merge/subtract/block generation)
├── events/            # Event system
│   ├── contracts.py   # Domain event dataclasses
│   └── sqs_client.py  # Async SQS publisher (publishes directly to notification queue)
├── integrations/      # External service clients
│   └── ushauth_client.py  # API gateway client for ushauth (branches, services, therapists)
├── api/
│   ├── deps.py        # FastAPI dependency injection
│   └── v1/            # REST API routers
│       ├── availability.py
│       ├── bookings.py
│       ├── payments.py
│       └── router.py
└── main.py            # Application factory + lifespan
```

## Domain Boundaries

| Domain | Responsibility |
|--------|----------------|
| Booking | Booking lifecycle, state machine, double-booking prevention, reschedule requests |
| Payment | Payment sessions, provider integration (MyFatoorah / Tap), refunds |
| Availability | O(n log n) slot computation from schedules, bookings, leaves, and holds |

## Key Design Decisions

### 1. Transactional Outbox
Events (BookingCreated, BookingConfirmed, etc.) are written to the `outbox_events` table **in the same database transaction** as the state change. A background asyncio task polls the table and publishes to SQS. This guarantees at-least-once delivery even if the process crashes.

### 2. O(n log n) Availability Engine
The availability engine is a pure computation module with zero I/O. It:
- Merges working hours + extra hours using sorted sweep
- Subtracts leaves, bookings, and holds using two-pointer subtraction
- Generates 30-minute time blocks with service-duration continuity checks
- Applies home-service travel buffers before/after home bookings

### 3. Double-Booking Prevention
- Row-level `SELECT FOR UPDATE` lock on the booking row during creation
- Overlap query against `bookings` table before inserting
- `TemporaryHold` records block slots during payment (15-minute TTL)
- PostgreSQL EXCLUDE constraint (defined in Alembic migration) as final guard

### 4. Payment Provider Selection
Customers choose between **MyFatoorah** (KNET/Card) and **Tap Payments** at payment time. Both satisfy the `PaymentGateway` Protocol — no inheritance, pure structural subtyping. Adding a third provider requires only a new file implementing the Protocol.

### 5. JWT + Application Token Auth
- `USHSPA-TOKEN` header: service-to-service authentication
- `Authorization: Bearer <JWT>` header: customer identity (RS256, validated locally from ushauth public key)
- Both are required on customer-facing endpoints
- Internal endpoints require only USHSPA-TOKEN

## Quick Start

### 1. Copy environment file
```bash
cp .env.example .env
# Fill in required values: DATABASE_URL, USHSPA_TOKEN, JWT_PUBLIC_KEY, API_GATEWAY_BASE_URL
```

### 2. Start infrastructure
```bash
docker-compose up postgres redis -d
```

### 3. Run migrations
```bash
pip install -e ".[dev]"
alembic upgrade head
```

### 4. Start the service
```bash
uvicorn app.main:app --reload --port 8001
```

### 5. API Documentation
Open `http://localhost:8001/api/docs/`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health/` | Health check |
| GET | `/api/v1/ready/` | Readiness probe |
| GET | `/api/v1/availability/` | Query therapist availability |
| POST | `/api/v1/bookings/` | Create booking |
| GET | `/api/v1/bookings/` | List customer bookings |
| GET | `/api/v1/bookings/{id}/` | Get booking detail |
| POST | `/api/v1/bookings/{id}/cancel/` | Cancel booking |
| POST | `/api/v1/bookings/{id}/reschedule/` | Request reschedule |
| POST | `/api/v1/payments/initiate/` | Initiate payment |
| GET | `/api/v1/payments/{booking_id}/status/` | Get payment status |
| POST | `/api/v1/payments/webhook/myfatoorah/` | MyFatoorah webhook |
| POST | `/api/v1/payments/webhook/tap/` | Tap webhook |

## Running Tests

```bash
# All unit tests (no database required)
pytest tests/unit/ -v

# Service tests (requires test database)
pytest tests/service/ -v
```

## Docker Build

```bash
docker build -t ushbooknpay:latest .
docker-compose up
```

## Domain Events Published to SQS

| Event | Trigger |
|-------|---------|
| `Booking.Created` | New booking created |
| `Booking.Confirmed` | Payment succeeded |
| `Booking.Cancelled` | Booking cancelled |
| `Booking.Completed` | Appointment completed |
| `Booking.NoShow` | Customer no-show |
| `Booking.RescheduleRequested` | Customer requests reschedule |
| `Payment.Initiated` | Payment session started |
| `Payment.Succeeded` | Payment confirmed by provider |
| `Payment.Failed` | Payment failed |
| `Payment.RefundIssued` | Refund processed |

## Environment Variables

See [`.env.example`](.env.example) for the full list with documentation.

Critical required variables:
- `DATABASE_URL` — PostgreSQL async connection string
- `USHSPA_TOKEN` — Application authentication token
- `JWT_PUBLIC_KEY` — RS256 public key from ushauth
- `API_GATEWAY_BASE_URL` — Base URL of the API gateway

## Future Extraction

The `booking/` and `payment/` directories are designed to be extracted into independent microservices with minimal changes:
1. Each has its own domain, application, infrastructure, and interfaces layers
2. They communicate only through the outbox event system
3. The only shared code is `app/core/` and `app/common/` — move to a shared library
