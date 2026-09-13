#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/market-intelligence/deploy}"
ENV_FILE="${ENV_FILE:-${DEPLOY_DIR}/oracle.env}"

docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" ps
docker compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}" logs --tail 100 --no-color
