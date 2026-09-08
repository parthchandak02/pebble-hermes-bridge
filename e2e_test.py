#!/usr/bin/env python3
"""Simulates a Pebble Index 01 webhook POST through the full chain.

Builds a real multipart body the way IndexWebhookApi.kt does, signs it with the
Pebble scheme, POSTs to the adapter, and reports each hop's status.
Usage: python3 e2e_test.py [--expect-hermes]
"""
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).parent
secrets = dict(
    line.split("=", 1) for line in (HERE / "secrets.env").read_text().splitlines() if "=" in line
)
SIGNING_SECRET = secrets["PEBBLE_SIGNING_SECRET"].encode()


def build_multipart(transcript: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex.upper()
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="transcription"\r\n\r\n{transcript}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="recordedAt"\r\n\r\n{int(time.time() * 1000)}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="client"\r\n\r\nring\r\n',
        f"--{boundary}--\r\n",
    ]
    body = "".join(parts).encode()
    return body, boundary


def sign(ts: str, delivery: str, trigger: str, is_test: int, body: bytes) -> str:
    canonical = f"v1\n{ts}\n{delivery}\n{trigger}\n{is_test}\n".encode() + body
    return hmac.new(SIGNING_SECRET, canonical, hashlib.sha256).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="double-click-hold")
    ap.add_argument("--text", default="e2e test: remind me this chain works end to end")
    args = ap.parse_args()

    body, boundary = build_multipart(args.text)
    ts = str(int(time.time()))
    delivery = f"e2e-{uuid.uuid4().hex[:8]}"
    sig = sign(ts, delivery, args.trigger, 0, body)

    req = urllib.request.Request(
        "http://127.0.0.1:8645/webhooks/pebble",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Index-Webhook-Version": "1",
            "X-Index-Trigger": args.trigger,
            "X-Index-Signature": sig,
            "X-Index-Timestamp": ts,
            "X-Index-Delivery": delivery,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = resp.status
            resp_body = resp.read().decode()[:120]
    except urllib.error.HTTPError as e:
        status = e.code
        resp_body = e.read().decode()[:120]

    print(f"Pebble-signed POST  -> adapter :8645 : HTTP {status} {resp_body}")
    if status in (200, 202):
        print("PASS: signature verified + multipart parsed + forwarded")
        print("(Hermes agent status surfaces in the Home WhatsApp group / sessions)")
    elif status in (502, 504):
        print("HALF-PASS: adapter verified + parsed fine; Hermes not listening yet")
        print("(expected until `hermes gateway restart` runs outside this session)")
        sys.exit(2)
    else:
        print("FAIL: see adapter logs ~/Library/Logs/index01-adapter/")
        sys.exit(1)


if __name__ == "__main__":
    main()
