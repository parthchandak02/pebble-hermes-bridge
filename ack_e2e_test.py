"""End-to-end test of the index01-adapter WhatsApp ack (🪨) path.

Sends a synthetic non-test press to the adapter webhook, then greps the
whatsapp bridge log for send/ack/error evidence within the last 90 seconds.
WARNING: this is a real press - it posts a real ack to the configured group.
"""
import json
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_LOG = Path.home() / ".hermes" / "whatsapp" / "bridge.log"

secrets = dict(
    line.split("=", 1) for line in (HERE / "secrets.env").read_text().splitlines() if "=" in line
)
bearer = secrets["PEBBLE_BEARER_TOKEN"].strip()
boundary = uuid.uuid4().hex.upper()
body = (
    f'--{boundary}\r\nContent-Disposition: form-data; name="transcription"\r\n\r\n'
    f"live test of the rock emoji ack, end to end\r\n"
    f'--{boundary}\r\nContent-Disposition: form-data; name="recordedAt"\r\n\r\n'
    f"{int(time.time() * 1000)}\r\n"
    f"--{boundary}--\r\n"
).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8645/webhooks/pebble",
    data=body,
    method="POST",
    headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {bearer}",
    },
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print("press:", resp.status)

time.sleep(3)

print("=== bridge log: send/ack/error lines in last 90s ===")
cutoff = (time.time() - 90) * 1000
hits = 0
for line in open(BRIDGE_LOG):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("time", 0) < cutoff:
        continue
    m = str(d.get("msg", ""))
    if any(k in m.lower() for k in ("send", "ack", "error", "fail", "message")):
        keep = {k: d.get(k) for k in ("time", "level", "msg", "msgId", "chatJid", "to") if d.get(k) is not None}
        print(keep)
        hits += 1
print("matching lines:", hits)
