#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/market-intelligence/deploy}"
ENV_FILE="${ENV_FILE:-${DEPLOY_DIR}/oracle.env}"

for required in "${DEPLOY_DIR}/compose.yaml" "${DEPLOY_DIR}/Caddyfile" "${ENV_FILE}"; do
  if [[ ! -f "${required}" ]]; then
    echo "Missing required deployment file: ${required}" >&2
    exit 1
  fi
done

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

for required in API_HOST BOT_HOST NEON_API_CONTEXT TELEGRAM_BOT_CONTEXT NEON_API_ENV_FILE TELEGRAM_BOT_ENV_FILE; do
  if [[ -z "${!required:-}" ]]; then
    echo "Missing required setting: ${required}" >&2
    exit 1
  fi
done

for secret_file in "${NEON_API_ENV_FILE}" "${TELEGRAM_BOT_ENV_FILE}"; do
  if [[ ! -f "${secret_file}" ]]; then
    echo "Missing secret environment file: ${secret_file}" >&2
    exit 1
  fi
  chmod 600 "${secret_file}"
done

docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" config --quiet
docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" build --pull
docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" up -d --remove-orphans
docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" ps

echo "Waiting for public health endpoints..."
for attempt in {1..24}; do
  api_ok=0
  bot_ok=0
  curl -fsS --max-time 10 "https://${API_HOST}/" >/dev/null && api_ok=1 || true
  curl -fsS --max-time 10 "https://${BOT_HOST}/health" >/dev/null && bot_ok=1 || true
  if [[ ${api_ok} -eq 1 && ${bot_ok} -eq 1 ]]; then
    echo "Deployment healthy: API and Telegram bot are reachable over HTTPS."
    exit 0
  fi
  echo "Health check ${attempt}/24 pending (api=${api_ok}, bot=${bot_ok})"
  sleep 10
done

echo "Deployment started, but public health checks did not both pass." >&2
docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" ps
exit 1
