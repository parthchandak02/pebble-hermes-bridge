#!/usr/bin/env bash
# Installs the index01-adapter launchd agent (macOS). Run from the repo checkout.
# Assumes .venv already exists (python3 -m venv .venv && .venv/bin/pip install -e .).
set -euo pipefail
cd "$(dirname "$0")"
INSTALL_DIR="$(pwd)"
HOME_DIR="$(cd ~ && pwd)"

./make-config.sh
mkdir -p ~/Library/Logs/index01-adapter

# Template the plist with this checkout's paths.
sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" -e "s|__HOME__|${HOME_DIR}|g" \
    com.user.index01-adapter.plist > ~/Library/LaunchAgents/com.user.index01-adapter.plist

launchctl unload ~/Library/LaunchAgents/com.user.index01-adapter.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.user.index01-adapter.plist
sleep 2
if lsof -nP -iTCP:8645 -sTCP:LISTEN | grep -q LISTEN; then
  echo "adapter listening on 8645"
else
  echo "WARNING: adapter not listening yet - check ~/Library/Logs/index01-adapter/err.log" >&2
fi
