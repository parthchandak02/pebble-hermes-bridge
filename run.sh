#!/usr/bin/env bash
# Launches the adapter from this checkout. Secrets come ONLY from the git-ignored
# secrets.env (mode 600) and are passed as INDEX01_* env vars, so config.yaml
# carries no secret values. Never prints secret values.
set -euo pipefail
cd "$(dirname "$0")"
[ -f secrets.env ] || { echo "secrets.env missing in $(pwd)" >&2; exit 1; }
_get() { grep "^$1=" secrets.env | head -1 | cut -d= -f2-; }
export INDEX01_SIGNING_SECRET="$(_get PEBBLE_SIGNING_SECRET)"
export INDEX01_BEARER_TOKEN="$(_get PEBBLE_BEARER_TOKEN)"
export INDEX01_ROUTE_SECRET="$(_get HERMES_ROUTE_SECRET)"
export INDEX01_CONFIG="${INDEX01_CONFIG:-$(pwd)/config.yaml}"
exec .venv/bin/python adapter.py
