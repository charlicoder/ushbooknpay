# ─────────────────────────────────────────────────────────────────────────
# ushbooknpay — Production Dockerfile
# Multi-stage build: deps → runtime
# ─────────────────────────────────────────────────────────────────────────

# ── Stage 1: Dependency resolver ─────────────────────────────────────────
FROM python:3.14-slim AS deps

WORKDIR /app

# Install build tools needed for native extensions (e.g. asyncpg, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY dependency manifests to leverage Docker layer caching
COPY requirements.txt pyproject.toml ./

# Install production dependencies into a prefix dir for copying
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime image ───────────────────────────────────────────────
FROM python:3.14-slim AS runtime

# Security: run as non-root
RUN groupadd -r ushapp && useradd -r -g ushapp ushapp

WORKDIR /app

# Copy only pre-built dependencies from deps stage (cached unless requirements change)
COPY --from=deps /install /usr/local

# Copy application source (changing these files will NOT re-run pip install)
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
COPY pyproject.toml ./
COPY requirements.txt ./

# Set ownership
RUN chown -R ushapp:ushapp /app

USER ushapp

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8003/api/v1/health/')"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8003

# Default startup command
CMD ["uvicorn", "app.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8003", \
    "--workers", "2", \
    "--loop", "uvloop", \
    "--http", "httptools", \
    "--proxy-headers", \
    "--forwarded-allow-ips", "*"]
