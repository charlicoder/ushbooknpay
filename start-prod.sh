#!/usr/bin/env bash
# =============================================================================
# start-prod.sh — Start ushbooknpay in PRODUCTION mode
#
# - Uses Dockerfile (multi-stage production build)
# - Uses docker-compose.prod.yml (no volume mount, no --reload)
# - Requires .env to have: APP_ENV=production, WORKERS>=2
#
# Usage:
#   ./start-prod.sh          → build and start in background
#   ./start-prod.sh --build  → force full rebuild
# =============================================================================

set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
SERVICE_NAME="ushbooknpay"
PORT="8003"

# Check .env exists
if [ ! -f ".env" ]; then
  echo "❌  .env file not found."
  echo "   Copy and configure: cp .env.example .env"
  exit 1
fi

# Enforce APP_ENV=production
APP_ENV_VAL=$(grep -E "^APP_ENV=" .env | cut -d '=' -f2 | tr -d '[:space:]' || true)
if [ "$APP_ENV_VAL" != "production" ]; then
  echo "❌  ERROR: .env must have APP_ENV=production for production deployment."
  echo "   Current value: '${APP_ENV_VAL:-<not set>}'"
  echo "   Update .env and retry."
  exit 1
fi

echo "🚀  Starting ${SERVICE_NAME} in PRODUCTION mode..."
echo "    Compose file : $COMPOSE_FILE"
echo "    Port         : ${PORT}"
echo ""

docker compose -f "$COMPOSE_FILE" up --build -d "$@"

echo ""
echo "✅  ${SERVICE_NAME} is running in the background on port ${PORT}."
echo "    View logs:  docker compose -f $COMPOSE_FILE logs -f"
echo "    Stop:       docker compose -f $COMPOSE_FILE down"
