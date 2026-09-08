"""Tests for index01-adapter.

Run with ``python -m pytest`` from the project root.
No external network calls — in-process aiohttp TestClient servers plus a local
fake Hermes target for the outbound round-trip. Hermes platform at 8644 is never
contacted.
"""

import hashlib
import hmac
import inspect
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from adapter import (
    Config,
    HermesForwarder,
    MultipartError,
    build_app,
    build_hermes_message,
    build_pebble_canonical_bytes,
    hmac_sha256_hex,
    load_config,
    parse_multipart,
    sign_hermes_request,
    verify_pebble_signature,
)

SIGNING_SECRET = "test_pebble_signing_secret"
ROUTE_SECRET = "test_hermes_route_secret"
BEARER_TOKEN = "test_bearer_token"
BOUNDARY = "test_boundary_abc"

# ---------------------------------------------------------------------------
# Known-answer vector (computed at build time with the stdlib hmac module)
# ---------------------------------------------------------------------------
# Canonical string:
#     "v1\n1700000000000\ndelivery-abc123\nsingle-click-hold\n0\nhello multipart body"
# i.e. the fixed prefix "v1\n<ts>\n<delivery>\n<trigger>\n<isTest>\n" concatenated
# with the RAW multipart request body bytes ("hello multipart body").
# key = b"test_payload_secret"
# HMAC-SHA256(key, canonical).hexdigest()
#   = 0d10ac4623ecc5fe9747a0c4fd4622087171f4d3d2f1d8eee25d4604bf6f05c1
KAT_CANONICAL = "v1\n1700000000000\ndelivery-abc123\nsingle-click-hold\n0\nhello multipart body"
KAT_KEY = "test_payload_secret"
KAT_SIG = "0d10ac4623ecc5fe9747a0c4fd4622087171f4d3d2f1d8eee25d4604bf6f05c1"


def _sig(secret: str, message: bytes) -> str:
    return hmac_sha256_hex(secret.encode("utf-8"), message)


def build_multipart_body(
    fields: dict[str, str], boundary: str = BOUNDARY, audio: bytes | None = None
) -> bytes:
    """Assemble a multipart/form-data body shaped like the Pebble app's."""
    out = b""
    for key, value in fields.items():
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
        ).encode("utf-8")
        out += value.encode("utf-8") + b"\r\n"
    if audio is not None:
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="audio"; filename="rec.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode("utf-8")
        out += audio + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return out


def sign_pebble(
    secret: str,
    *,
    timestamp: str,
    delivery: str,
    trigger: str,
    is_test: bool,
    raw_body: bytes,
) -> str:
    canonical = build_pebble_canonical_bytes(
        timestamp=timestamp,
        delivery_id=delivery,
        trigger_value=trigger,
        is_test=is_test,
        raw_body=raw_body,
    )
    return _sig(secret, canonical)


def valid_body(
    *,
    trigger: str = "single-click-hold",
    ts: str = "1700000000",
    delivery: str = "fileId-42",
    is_test: bool = False,
    secret: str = SIGNING_SECRET,
    fields: dict[str, str] | None = None,
    transcript: str = "remind me to water plants",
) -> tuple[bytes, dict[str, str]]:
    """Build a well-formed Pebble multipart body + signed headers. A fixed clock
    (unused by this builder; kept so callers pin timestamp/delivery as needed)."""
    flds = fields or {
        "transcription": transcript,
        "recordedAt": "1700000000500",
        "client": "ring",
    }
    body = build_multipart_body(flds)
    sig = sign_pebble(
        secret, timestamp=ts, delivery=delivery, trigger=trigger, is_test=is_test, raw_body=body
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Webhook-Version": "1",
        "X-Index-Trigger": trigger,
        "X-Index-Timestamp": ts,
        "X-Index-Delivery": delivery,
        "X-Index-Signature": sig,
    }
    return body, headers


def make_config(**overrides) -> Config:
    base = dict(
        signing_secret=SIGNING_SECRET,
        route_secret=ROUTE_SECRET,
        hermes_url="http://127.0.0.1:8644/webhooks/pebble",
        listen_host="127.0.0.1",
        listen_port=8645,
        max_body_size=5 * 1024 * 1024,
        max_timestamp_age=300,
        log_level="INFO",
        request_timeout=5.0,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


async def make_client(cfg: Config, forwarder, clock) -> TestClient:
    app = build_app(cfg, forwarder=forwarder, clock=clock)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class FakeForwarder:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[tuple[dict, str]] = []

    async def send(self, payload: dict, delivery: str) -> int:
        self.calls.append((json.loads(json.dumps(payload)), delivery))
        return self.status


# ---------------------------------------------------------------------------
# Pebble signing / canonical string
# ---------------------------------------------------------------------------


def test_known_answer_vector_pebble_hmac() -> None:
    """Guard the canonical-string builder against regressions (KAT above)."""
    raw = b"hello multipart body"
    canonical = build_pebble_canonical_bytes(
        timestamp="1700000000000",
        delivery_id="delivery-abc123",
        trigger_value="single-click-hold",
        is_test=False,
        raw_body=raw,
    )
    assert canonical == KAT_CANONICAL.encode("utf-8")
    assert _sig(KAT_KEY, canonical) == KAT_SIG
    assert verify_pebble_signature(
        KAT_KEY,
        timestamp="1700000000000",
        delivery_id="delivery-abc123",
        trigger_value="single-click-hold",
        is_test=False,
        raw_body=raw,
        provided=KAT_SIG,
    )


def test_canonical_covers_exact_raw_body_bytes() -> None:
    """Re-encoding the same fields differently (different raw bytes) must break
    the signature — the HMAC covers the exact received octet stream."""
    fields = {"transcription": "hello", "recordedAt": "1700000000000"}
    raw_a = build_multipart_body(fields, boundary="b1")
    raw_b = build_multipart_body(dict(reversed(list(fields.items()))), boundary="b1")
    sig_a = _sig("k", build_pebble_canonical_bytes(timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw_a))
    sig_b = _sig("k", build_pebble_canonical_bytes(timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw_b))
    assert sig_a != sig_b


def test_is_test_flag_encodes_0_and_1() -> None:
    raw = b"x"
    c0 = build_pebble_canonical_bytes(timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw)
    c1 = build_pebble_canonical_bytes(timestamp="1", delivery_id="d", trigger_value="t", is_test=True, raw_body=raw)
    assert c0 == b"v1\n1\nd\nt\n0\nx"
    assert c1 == b"v1\n1\nd\nt\n1\nx"
    assert c0 != c1


def test_verify_rejects_bad_signatures_constant_time() -> None:
    raw = build_multipart_body({"transcription": "hello"})
    canonical = build_pebble_canonical_bytes(timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw)
    good = _sig("k", canonical)
    tampered = ("0" + good[1:]) if good[0] != "0" else ("1" + good[1:])
    assert verify_pebble_signature(
        "k", timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw, provided=good
    )
    assert not verify_pebble_signature(
        "k", timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw, provided=tampered
    )
    assert not verify_pebble_signature(
        "wrong-key", timestamp="1", delivery_id="d", trigger_value="t", is_test=False, raw_body=raw, provided=good
    )
    # Must actually use constant-time comparison, not plain ==.
    assert "hmac.compare_digest" in inspect.getsource(verify_pebble_signature)


# ---------------------------------------------------------------------------
# Hermes outbound signing
# ---------------------------------------------------------------------------


def test_hermes_request_signature_round_trip() -> None:
    body = b'{"a":1,"delivery":"rt-1"}'
    ts = "1700000000001"
    sig = sign_hermes_request(ROUTE_SECRET, ts, body)
    assert sig == _sig(ROUTE_SECRET, build_hermes_message(ts, body))
    assert sig.islower() and len(sig) == 64
    # Signature must change when body bytes change (canonical over exact body).
    assert sign_hermes_request(ROUTE_SECRET, ts, body + b" ") != sig
    assert sign_hermes_request(ROUTE_SECRET, "1700000000002", body) != sig


# ---------------------------------------------------------------------------
# Multipart parsing (pure, no HTTP)
# ---------------------------------------------------------------------------


def test_parse_multipart_audio_discarded() -> None:
    body = build_multipart_body(
        {"transcription": "hello world", "recordedAt": "1700000000000", "client": "ring"},
        audio=b"\x00\x01\x02fake-m4a",
    )
    parsed = parse_multipart(body, BOUNDARY)
    assert parsed == {"transcription": "hello world", "recordedAt": "1700000000000", "client": "ring"}
    assert "audio" not in parsed  # file parts are never surface to handlers


def test_parse_multipart_utf8_multibyte() -> None:
    transcript = "caf\u00e9 \u4f60\u597d \U0001f44d"
    body = build_multipart_body({"transcription": transcript})
    assert parse_multipart(body, BOUNDARY)["transcription"] == transcript


def test_parse_multipart_missing_boundary_raises() -> None:
    with pytest.raises(MultipartError):
        parse_multipart(b"no boundary present anywhere", "nonexistent")


def test_parse_multipart_garbage_raises() -> None:
    with pytest.raises(MultipartError):
        parse_multipart(b"--only\r\nno-header-terminator-here\r\n", "only")


def _build_many_parts_body(n: int, boundary: str = BOUNDARY) -> bytes:
    out = b""
    for i in range(n):
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="f{i}"\r\n\r\n'
            "v\r\n"
        ).encode("utf-8")
    out += f"--{boundary}--\r\n".encode("utf-8")
    return out


def test_too_many_parts_rejected() -> None:
    with pytest.raises(MultipartError, match="too many multipart parts"):
        parse_multipart(_build_many_parts_body(33), BOUNDARY)


# ---------------------------------------------------------------------------
# HTTP behavior (integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_request_forwards_expected_payload() -> None:
    fwd = FakeForwarder(status=202)
    body, headers = valid_body()
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/anything", data=body, headers=headers)
        assert resp.status == 202
        assert len(fwd.calls) == 1
        payload, delivery = fwd.calls[0]
        assert delivery == "fileId-42"
        assert payload == {
            "transcript": "remind me to water plants",
            "recordedAt": 1700000000500,
            "trigger": "single-click-hold",
            "delivery": "fileId-42",
            "isTest": False,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_test_event_flag_true() -> None:
    fwd = FakeForwarder(status=202)
    body = build_multipart_body(
        {"transcription": "this is a test", "recordedAt": "1700000000500", "client": "ring", "test": "true"}
    )
    sig = sign_pebble(
        SIGNING_SECRET, timestamp="1700000000", delivery="d-1", trigger="test-event", is_test=True, raw_body=body
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Trigger": "test-event",
        "X-Index-Timestamp": "1700000000",
        "X-Index-Delivery": "d-1",
        "X-Index-Signature": sig,
        "X-Index-Test": "true",
    }
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 202
        payload, _ = fwd.calls[0]
        assert payload["isTest"] is True
        assert payload["trigger"] == "test-event"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_signature_403() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body()
    headers = {k: v for k, v in headers.items() if k != "X-Index-Signature"}
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_signature_403() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body()
    headers["X-Index-Signature"] = "0" * 64
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stale_timestamp_403() -> None:
    fwd = FakeForwarder()
    # valid_body defaults ts=1700000000; now is 1700000300 (300s). Make it older.
    body, headers = valid_body(ts="1699999700")  # 400s old
    client = await make_client(make_config(max_timestamp_age=300), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fresh_timestamp_accepted() -> None:
    fwd = FakeForwarder(status=202)
    body, headers = valid_body(ts="1700000299")  # 1s old
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 202
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_future_timestamp_rejected() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body(ts="1700001000")  # 700s in the future
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_numeric_timestamp_rejected() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body(ts="1700000000.5")
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_delivery_id_403() -> None:
    """Deleting a header from a signed request breaks the HMAC -> 403 (tamper),
    not 400; unsigned Bearer-only apps get delivery synthesized instead."""
    fwd = FakeForwarder()
    body, headers = valid_body()
    del headers["X-Index-Delivery"]
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_trigger_403() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body()
    del headers["X-Index-Trigger"]
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unsigned_bearer_only_synthesizes_headers() -> None:
    """Real-world app mode: 'Sign requests' OFF. Upstream IndexWebhookApi.kt omits
    ALL X-Index-* signature headers then; only Authorization + multipart arrive."""
    fwd = FakeForwarder(status=202)
    body = build_multipart_body({"transcription": "bearer only press", "recordedAt": "1700000000500"})
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "Authorization": f"Bearer {BEARER_TOKEN}",
    }
    client = await make_client(make_config(bearer_token=BEARER_TOKEN), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 202
        assert len(fwd.calls) == 1
        payload, delivery = fwd.calls[0]
        assert payload["transcript"] == "bearer only press"
        assert delivery.startswith("unsigned-")
        assert payload["trigger"] == "single-click-hold"
        assert payload["isTest"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unsigned_without_bearer_403() -> None:
    fwd = FakeForwarder()
    body = build_multipart_body({"transcription": "no auth at all", "recordedAt": "1700000000500"})
    headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 403
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_short_transcript_200_without_forward() -> None:
    fwd = FakeForwarder()
    body = build_multipart_body({"transcription": "hi", "recordedAt": "1700000000500"})
    sig = sign_pebble(SIGNING_SECRET, timestamp="1700000000", delivery="f9", trigger="single-click-hold", is_test=False, raw_body=body)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Trigger": "single-click-hold",
        "X-Index-Timestamp": "1700000000",
        "X-Index-Delivery": "f9",
        "X-Index-Signature": sig,
    }
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 200
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_transcription_treated_as_garbled_200() -> None:
    """No transcription part => treated as a garbled/empty recording: acked 200,
    nothing forwarded so Pebble won't surface a failure in Recent runs."""
    fwd = FakeForwarder()
    body = build_multipart_body({"recordedAt": "1700000000500"})
    sig = sign_pebble(SIGNING_SECRET, timestamp="1700000000", delivery="f10", trigger="single-click-hold", is_test=False, raw_body=body)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Trigger": "single-click-hold",
        "X-Index-Timestamp": "1700000000",
        "X-Index-Delivery": "f10",
        "X-Index-Signature": sig,
    }
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 200
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrong_content_type_400() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body()
    headers["Content-Type"] = "application/json"
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_boundary_400() -> None:
    fwd = FakeForwarder()
    body, headers = valid_body()
    headers["Content-Type"] = "multipart/form-data"
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unhandled_exception_returns_500() -> None:
    class BoomForwarder:
        async def send(self, payload: dict, delivery: str) -> int:
            raise RuntimeError("unexpected boom")

    fwd = BoomForwarder()
    body, headers = valid_body()
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 500
        assert await resp.text() == "internal error"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_huge_body_413() -> None:
    fwd = FakeForwarder()
    body = build_multipart_body({"transcription": "x" * 900, "recordedAt": "1700000000500"})
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Trigger": "single-click-hold",
        "X-Index-Timestamp": "1700000000",
        "X-Index-Delivery": "big-1",
        "X-Index-Signature": "0" * 64,
    }
    client = await make_client(make_config(max_body_size=512), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 413
        assert fwd.calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_hermes_status_relayed_to_pebble() -> None:
    for hermes_status in (200, 400, 403, 429, 413):
        fwd = FakeForwarder(status=hermes_status)
        body, headers = valid_body(delivery=f"dedupe-{hermes_status}")
        client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
        try:
            resp = await client.post("/", data=body, headers=headers)
            assert resp.status == hermes_status, hermes_status
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_any_path_accepted() -> None:
    fwd = FakeForwarder(status=200)
    body, headers = valid_body()
    client = await make_client(make_config(), fwd, clock=lambda: 1700000300.0)
    try:
        for path in ("/", "/hook", "/v1/pebble", "/a/b/c"):
            resp = await client.post(path, data=body, headers=headers)
            assert resp.status == 200, path
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_signing_disabled_compat() -> None:
    """With signing_secret unset, skip HMAC but still guard delivery and parse."""
    fwd = FakeForwarder(status=202)
    body = build_multipart_body({"transcription": "demo entry", "recordedAt": "1700000000500"})
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "X-Index-Trigger": "single-click-hold",
        "X-Index-Delivery": "nosign-1",
        "X-Index-Signature": "garbage-ignored-when-disabled",
    }
    client = await make_client(make_config(signing_secret=None), fwd, clock=lambda: 1700000300.0)
    try:
        resp = await client.post("/", data=body, headers=headers)
        assert resp.status == 202
        assert fwd.calls[0][0]["transcript"] == "demo entry"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Outbound Hermes signing round-trip against a fake Hermes server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_forwarder_signature_verified_by_fake_hermes() -> None:
    received: list[dict] = []

    async def hermes_handler(request: web.Request) -> web.Response:
        raw = await request.read()
        ts = request.headers.get("X-Webhook-Timestamp", "")
        sig = request.headers.get("X-Webhook-Signature-V2", "")
        rid = request.headers.get("X-Request-ID", "")
        ct = request.headers.get("Content-Type", "")
        expected = _sig(ROUTE_SECRET, ts.encode("utf-8") + b"." + raw)
        received.append(
            {"ok": sig == expected, "rid": rid, "ct": ct, "body": json.loads(raw)}
        )
        return web.json_response({"status": "queued"}, status=202)

    app = web.Application()
    app.router.add_post("/webhooks/pebble", hermes_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/webhooks/pebble"
        async with aiohttp.ClientSession() as session:
            fwd = HermesForwarder(
                session, ROUTE_SECRET, url, timeout=5.0, clock=lambda: 1700000300.0
            )
            payload = {
                "transcript": "hello",
                "recordedAt": 1700000000500,
                "trigger": "single-click-hold",
                "delivery": "rt-1",
                "isTest": False,
            }
            status = await fwd.send(payload, "rt-1")
        assert status == 202
        assert len(received) == 1
        assert received[0]["ok"] is True        # signature matched fresh recompute
        assert received[0]["rid"] == "rt-1"
        assert received[0]["ct"] == "application/json"
        assert received[0]["body"] == payload
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_config_missing_file_uses_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INDEX01_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    monkeypatch.setenv("INDEX01_ROUTE_SECRET", "env_route")
    monkeypatch.setenv("INDEX01_HERMES_URL", "http://env:1/hook")
    monkeypatch.setenv("INDEX01_ALLOW_UNSIGNED", "1")
    cfg = load_config()
    assert cfg.route_secret == "env_route"
    assert cfg.hermes_url == "http://env:1/hook"
    assert cfg.signing_enabled is False


def test_load_config_missing_signing_secret_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INDEX01_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    monkeypatch.setenv("INDEX01_ROUTE_SECRET", "env_route")
    monkeypatch.setenv("INDEX01_HERMES_URL", "http://env:1/hook")
    monkeypatch.delenv("INDEX01_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("INDEX01_SIGNING_SECRET", raising=False)
    with pytest.raises(ValueError, match="signing_secret is required"):
        load_config()


def test_load_config_missing_required_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INDEX01_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    monkeypatch.delenv("INDEX01_ROUTE_SECRET", raising=False)
    monkeypatch.delenv("INDEX01_HERMES_URL", raising=False)
    with pytest.raises(ValueError):
        load_config()