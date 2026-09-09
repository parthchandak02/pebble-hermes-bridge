#!/usr/bin/env bash
# Generates config.yaml (+ secrets.env on first run) for the index01-adapter.
# Never prints secret values. Run from the repo checkout.
set -euo pipefail
cd "$(dirname "$0")"

# First run: generate fresh random secrets.
if [ ! -f secrets.env ]; then
  {
    echo "PEBBLE_SIGNING_SECRET=$(openssl rand -hex 32)"
    echo "HERMES_ROUTE_SECRET=$(openssl rand -hex 32)"
    echo "PEBBLE_BEARER_TOKEN=$(openssl rand -hex 24)"
  } > secrets.env
  chmod 600 secrets.env
  echo "secrets.env generated (600 perms). Keep it private; it is gitignored."
fi

for var in PEBBLE_SIGNING_SECRET HERMES_ROUTE_SECRET PEBBLE_BEARER_TOKEN; do
  if ! grep -q "^${var}=" secrets.env; then
    echo "missing ${var} in secrets.env" >&2; exit 1
  fi
done
PEB="$(grep '^PEBBLE_SIGNING_SECRET=' secrets.env | cut -d= -f2)"
HER="$(grep '^HERMES_ROUTE_SECRET=' secrets.env | cut -d= -f2)"
BEAR="$(grep '^PEBBLE_BEARER_TOKEN=' secrets.env | cut -d= -f2)"
cat > config.yaml << EOF
pebble:
  signing_secret: "${PEB}"
  bearer_token: "${BEAR}"
  max_body_size: 5242880
  max_timestamp_age: 300
hermes:
  route_secret: "${HER}"
  url: "http://127.0.0.1:8644/webhooks/pebble"
listen:
  host: "127.0.0.1"
  port: 8645
# Instant "🪨 Heard. Working on it…" ack in a WhatsApp group when a press arrives.
# Uses the local Baileys bridge. Leave ack_chat_id empty to disable.
whatsapp:
  ack_chat_id: "${ACK_CHAT_ID:-}"
  ack_bridge_port: 3000
  ack_emoji: "🪨"
log_level: "INFO"
EOF
chmod 600 config.yaml
echo "config.yaml written (600 perms)"
echo "Route secret for the Hermes config.yaml route block: use the same value as hermes.route_secret (see apply-hermes-config.py)."
