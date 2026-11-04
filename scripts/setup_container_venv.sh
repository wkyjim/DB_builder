#!/usr/bin/env bash
# Idempotent Python environment bootstrap for the Hermes Docker container.
#
# The container ships a bare Python; the project's dependencies (yfinance,
# pandas, psycopg2, ...) live in the Windows conda env, not here. This script
# builds an isolated venv so tests and scripts can run inside the container
# without touching Hermes' own interpreter.
#
# Usage:
#   bash scripts/setup_container_venv.sh            # create or verify
#   bash scripts/setup_container_venv.sh --force    # rebuild from scratch
#
# Env:
#   DB_BUILDER_VENV   override venv location (default /opt/db_builder-venv)
#
# Exit codes: 0 = ready, 1 = bootstrap failed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${DB_BUILDER_VENV:-/opt/db_builder-venv}"
REQ="$ROOT/requirements-dev.txt"
STAMP="$VENV_DIR/.requirements.sha256"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ ! -f "$REQ" ]]; then
  echo "ERROR: missing $REQ" >&2
  exit 1
fi

HASH="$(sha256sum "$REQ" | awk '{print $1}')"
IMPORTS='import pandas, numpy, yfinance, sqlalchemy, psycopg2, dotenv, pandas_market_calendars, exchange_calendars, feedparser, bs4, pytest'

# Fast path: venv present, requirements unchanged, imports healthy.
if [[ $FORCE -eq 0 && -x "$VENV_DIR/bin/python" && -f "$STAMP" ]]; then
  if [[ "$(cat "$STAMP")" == "$HASH" ]] && "$VENV_DIR/bin/python" -c "$IMPORTS" >/dev/null 2>&1; then
    echo "OK: $VENV_DIR already provisioned (requirements $HASH)"
    echo "python: $VENV_DIR/bin/python"
    exit 0
  fi
  echo "WARN: venv present but stale or broken; repairing" >&2
fi

if [[ $FORCE -eq 1 && -d "$VENV_DIR" ]]; then
  echo "Removing $VENV_DIR (--force)"
  rm -rf "$VENV_DIR"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "Installing $REQ"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REQ"

"$VENV_DIR/bin/python" - <<'PY'
import bs4, dotenv, exchange_calendars, feedparser, numpy, pandas
import pandas_market_calendars, psycopg2, pytest, sqlalchemy, yfinance
print("pandas", pandas.__version__)
print("numpy", numpy.__version__)
print("yfinance", yfinance.__version__)
print("sqlalchemy", sqlalchemy.__version__)
print("pytest", pytest.__version__)
PY

echo "$HASH" > "$STAMP"
echo "OK: provisioned $VENV_DIR"
echo "Use: $VENV_DIR/bin/python -m pytest tests/..."
