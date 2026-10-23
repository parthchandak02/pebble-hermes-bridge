#!/usr/bin/env python3
"""Launch the adapter from this checkout (used by launchd).

Secrets come ONLY from the git-ignored secrets.env (mode 600) and are passed to
adapter.py as INDEX01_* environment variables, so config.yaml carries no secret
values. Never prints secret values.

Python rather than a shell script on purpose: on macOS, a launchd-started
/bin/bash is denied access to external volumes, while the Python interpreter
can be granted it.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRETS = HERE / "secrets.env"
ENV_NAMES = {
    "PEBBLE_SIGNING_SECRET": "INDEX01_SIGNING_SECRET",
    "PEBBLE_BEARER_TOKEN": "INDEX01_BEARER_TOKEN",
    "HERMES_ROUTE_SECRET": "INDEX01_ROUTE_SECRET",
}


def main() -> None:
    if not SECRETS.exists():
        sys.exit(f"secrets file missing: {SECRETS}")
    values = {}
    for line in SECRETS.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    for src, dst in ENV_NAMES.items():
        if values.get(src):
            os.environ[dst] = values[src]
    os.environ.setdefault("INDEX01_CONFIG", str(HERE / "config.yaml"))
    os.chdir(HERE)
    os.execv(sys.executable, [sys.executable, str(HERE / "adapter.py")])


if __name__ == "__main__":
    main()
