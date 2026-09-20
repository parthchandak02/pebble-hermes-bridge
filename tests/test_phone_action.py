"""Tests for phone_action: prefix matching, fail-closed config, rate limits.

Covers the red-team requirements: prefix-only matching, gate-veto is the
caller's job (tested in test_adapter), static push body (no transcript
embedding), per-hour rate cap, and fail-closed config validation.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch as mock_patch

import aiohttp
import pytest
import yaml

import phone_action
from phone_action import (
    fire_action,
    get_actions,
    load_triggers,
    match_action,
    _canon,
    _rate_ok,
)

VALID_TRIGGER = {
    "phone_call": {
        "phrase": "alpha beta pineapple",
        "url": "https://ntfy.sh/test-topic-abc",
        "headers": {"Title": "t"},
        "body": "static body",
    }
}


# --- canonicalization + matching -------------------------------------------

def test_canon_collapses_punctuation_and_case():
    assert _canon("Alpha, Beta!  PINEAPPLE") == "alpha beta pineapple"


def test_match_exact_prefix():
    assert match_action("Alpha Beta Pineapple", VALID_TRIGGER) == "phone_call"


def test_match_prefix_with_trailing_words():
    assert match_action("Alpha beta pineapple now please", VALID_TRIGGER) == "phone_call"


def test_match_mid_sentence_does_not_trigger():
    assert match_action("hey remember alpha beta pineapple", VALID_TRIGGER) is None


def test_match_partial_phrase_rejected():
    assert match_action("alpha beta", VALID_TRIGGER) is None
    assert match_action("alpha pineapple", VALID_TRIGGER) is None


def test_match_non_string_transcript():
    assert match_action(None, VALID_TRIGGER) is None
    assert match_action(123, VALID_TRIGGER) is None


def test_match_longest_phrase_wins():
    triggers = {
        "short": {"phrase": "alpha beta", "url": "https://x.example", "headers": {}, "body": ""},
        "long": {"phrase": "alpha beta pineapple", "url": "https://y.example", "headers": {}, "body": ""},
    }
    assert match_action("alpha beta pineapple", triggers) == "long"


# --- config validation (fail-closed) ----------------------------------------

def test_load_triggers_none_disables():
    assert load_triggers(None) == {}


def test_load_triggers_rejects_bad_url():
    with pytest.raises(ValueError):
        load_triggers({"triggers": {"a": {"phrase": "call now", "url": "ftp://x"}}})


def test_load_triggers_rejects_short_phrase():
    with pytest.raises(ValueError):
        load_triggers({"triggers": {"a": {"phrase": "ab", "url": "https://x"}}})


def test_load_triggers_rejects_bad_name():
    with pytest.raises(ValueError):
        load_triggers({"triggers": {"Bad Name": {"phrase": "call now", "url": "https://x"}}})


def test_load_triggers_rejects_non_mapping():
    with pytest.raises(ValueError):
        load_triggers(["nope"])


def test_get_actions_reads_real_config():
    enabled, rate, triggers = get_actions()
    assert isinstance(enabled, bool)
    assert rate >= 1
    assert "phone_call" in triggers
    assert triggers["phone_call"]["phrase"] == "alpha beta pineapple"
    assert triggers["phone_call"]["url"].startswith("https://ntfy.sh/")


def test_get_actions_kill_switch(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "actions": {"enabled": False, "triggers": {
            "phone_call": {"phrase": "alpha beta pineapple", "url": "https://ntfy.sh/x"}
        }}
    }))
    monkeypatch.setenv("INDEX01_CONFIG", str(cfg))
    enabled, _, _ = get_actions()
    assert enabled is False


def test_get_actions_raises_on_garbage(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("actions: ['bad']")
    monkeypatch.setenv("INDEX01_CONFIG", str(cfg))
    with pytest.raises(ValueError):
        get_actions()


# --- rate limiting -----------------------------------------------------------

def test_rate_ok_caps_within_hour():
    phone_action._RATE.clear()
    assert _rate_ok("t", 3, now=1000.0)
    assert _rate_ok("t", 3, now=1001.0)
    assert _rate_ok("t", 3, now=1002.0)
    assert not _rate_ok("t", 3, now=1003.0)  # 4th within the hour blocked
    assert _rate_ok("t", 3, now=1000.0 + 3601)  # window slid, allowed again


def test_rate_key_independent_per_trigger():
    phone_action._RATE.clear()
    assert _rate_ok("a", 1, now=1000.0)
    assert _rate_ok("b", 1, now=1000.0)


# --- fire_action --------------------------------------------------------------

class _Resp:
    status = 200
    async def read(self):
        return b""


class _SessionStub:
    def __init__(self, status=200):
        self.status = status
        self.posted = []

    class _CM:
        def __init__(self, outer, url, data, headers):
            self.outer = outer
            self.req = {"url": url, "data": data, "headers": headers}

        async def __aenter__(self):
            self.outer.posted.append(self.req)
            return _Resp()

        async def __aexit__(self, *a):
            return False

    def post(self, url, data=None, headers=None, timeout=None):
        return self._CM(self, url, data, headers)


@pytest.mark.asyncio
async def test_fire_action_posts_static_body_no_transcript():
    session = _SessionStub()
    ok, detail = await fire_action(session, VALID_TRIGGER["phone_call"], delivery="d1")
    assert ok is True
    assert detail == "HTTP 200"
    posted = session.posted[0]
    assert posted["data"] == b"static body"
    # Red-team: transcript must never be in the body.
    assert b"alpha" not in posted["data"].lower() or "static" in str(posted["data"])


@pytest.mark.asyncio
async def test_fire_action_rate_limited():
    phone_action._RATE.clear()
    with mock_patch.object(phone_action, "_rate_ok", return_value=False):
        session = _SessionStub()
        ok, detail = await fire_action(session, VALID_TRIGGER["phone_call"], delivery="d2")
        assert ok is False
        assert detail == "rate-limited"
        assert session.posted == []  # nothing hit the network


@pytest.mark.asyncio
async def test_fire_action_http_500_is_failure():
    class _R:
        status = 500
        async def read(self):
            return b""

    class _S:
        def post(self, *a, **k):
            return self

        async def __aenter__(self):
            return _R()

        async def __aexit__(self, *a):
            return False

    ok, detail = await fire_action(_S(), VALID_TRIGGER["phone_call"], delivery="d3")
    assert ok is False
    assert "500" in detail


@pytest.mark.asyncio
async def test_fire_action_network_error_swallowed():
    class _S:
        def post(self, *a, **k):
            raise aiohttp.ClientError("boom")

    ok, detail = await fire_action(_S(), VALID_TRIGGER["phone_call"], delivery="d4")
    assert ok is False
    assert "ClientError" in detail


@pytest.mark.asyncio
async def test_fire_action_no_session():
    ok, detail = await fire_action(None, VALID_TRIGGER["phone_call"], delivery="d5")
    assert ok is False


# --- end-to-end through adapter.dispatch --------------------------------------

async def test_adapter_dispatch_fires_action_on_phrase(tmp_path, monkeypatch):
    """Full dispatch path: matching press -> action POST + payload marker."""
    from adapter import Config, build_app
    from aiohttp.test_utils import TestClient, TestServer

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "actions": {"enabled": True, "rate_limit_per_hour": 5, "triggers": {
            "phone_call": {
                "phrase": "alpha beta pineapple",
                "url": "https://ntfy.sh/test-e2e",
                "headers": {"Title": "t"},
                "body": "go",
            }
        }}
    }))
    monkeypatch.setenv("INDEX01_CONFIG", str(cfg_file))
    phone_action._RATE.clear()

    fired = {}

    class _FakeResp:
        status = 200

        async def read(self):
            return b""

    class _FakeCM:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def post(self, *a, **k):
            return _FakeCM()

    class _Fwd:
        _session = _FakeSession()

        async def send(self, payload, delivery):
            fired["payload"] = payload
            return 202

    import hashlib, hmac as hmac_mod, json as json_mod, time as time_mod

    secret = "test-secret"
    cfg = Config(signing_secret=secret, route_secret="r", hermes_url="http://x")
    app = build_app(cfg, forwarder=_Fwd())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        boundary = "BOUNDARYX"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="transcription"\r\n\r\n'
            f"Alpha Beta Pineapple\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="recordedAt"\r\n\r\n'
            f"1700000000000\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        ts = str(int(time_mod.time()))
        canonical = f"v1\n{ts}\ndlv-e2e\nsingle-click-hold\n0\n".encode() + body
        sig = hmac_mod.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        resp = await client.post(
            "/webhooks/pebble",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Index-Signature": sig,
                "X-Index-Timestamp": ts,
                "X-Index-Delivery": "dlv-e2e",
                "X-Index-Trigger": "single-click-hold",
            },
        )
        assert resp.status == 202
        assert fired["payload"].get("action_fired") == "phone_call"
    finally:
        await client.close()


async def test_adapter_sensitive_press_never_fires_action(tmp_path, monkeypatch):
    """Gate > trigger: 'Alpha beta pineapple call the doctor about my medication'."""
    from adapter import Config, build_app
    from aiohttp.test_utils import TestClient, TestServer

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "actions": {"enabled": True, "triggers": {
            "phone_call": {"phrase": "alpha beta pineapple", "url": "https://ntfy.sh/t"}
        }}
    }))
    monkeypatch.setenv("INDEX01_CONFIG", str(cfg_file))
    phone_action._RATE.clear()

    fired = {}

    class _Fwd:
        async def send(self, payload, delivery):
            fired["payload"] = payload
            return 202

    import hashlib, hmac as hmac_mod, time as time_mod

    secret = "test-secret"
    cfg = Config(signing_secret=secret, route_secret="r", hermes_url="http://x")
    app = build_app(cfg, forwarder=_Fwd())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        boundary = "BOUNDARYX"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="transcription"\r\n\r\n'
            f"Alpha beta pineapple my medication ran out\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="recordedAt"\r\n\r\n'
            f"1700000000000\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        ts = str(int(time_mod.time()))
        canonical = f"v1\n{ts}\ndlv-sens\nsingle-click-hold\n0\n".encode() + body
        sig = hmac_mod.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        resp = await client.post(
            "/webhooks/pebble",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Index-Signature": sig,
                "X-Index-Timestamp": ts,
                "X-Index-Delivery": "dlv-sens",
                "X-Index-Trigger": "single-click-hold",
            },
        )
        assert resp.status == 202
        assert "action_fired" not in fired["payload"]
        assert fired["payload"]["sensitive"] is True
    finally:
        await client.close()
