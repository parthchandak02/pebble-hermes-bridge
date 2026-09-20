"""Action triggers — deterministic spoken phrases that cause physical actions.

A press whose transcript STARTS with a configured phrase (config.yaml
``actions.triggers``) is a command, not a question. The adapter fires the
action directly — no LLM in the decision path, same reason the sensitive gate
is code and not a prompt: a string-match cannot be talked out of matching.

v1 action transport: POST to an ntfy.sh topic. The user's iPhone subscribes to
the topic in the ntfy app; an iOS 27 Shortcuts automation ("when I receive a
notification from ntfy") runs a Call shortcut in the background. The phone
number lives ONLY inside the Call shortcut on the phone — it never enters the
adapter, the config, or the network payload (red-team: who can name the
destination).

Hardening rules (Sep 19 2026 red-team + reliability review):
- PREFIX match only: the phrase must open the canonicalized transcript.
  Ambient speech mentioning the phrase mid-press never triggers.
- Gate > trigger precedence: the caller (adapter.dispatch) only consults
  actions when the sensitive gate did NOT hit. A "call the doctor" press
  is answered in intake, never auto-dialed.
- Fail-closed: invalid trigger config raises -> adapter fires nothing and
  still forwards the press to Hermes. A broken action config can never
  turn into a wrong action.
- Rate-capped per action (default 3/hour) and every attempt is logged to
  WhatsApp by the caller (visible, auditable).
- Kill switch: ``actions.enabled: false`` in config.yaml + launchd kickstart.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import aiohttp

log = logging.getLogger("index01.phone_action")

DEFAULT_RATE_LIMIT_PER_HOUR = 3
_DEFAULT_TIMEOUT = 10.0

# module-level rate state: {action_name: [epoch timestamps of recent fires]}
_RATE: dict[str, list[float]] = {}


def _canon(text: str) -> str:
    """Lowercase; collapse every non-alphanumeric run to a single space.

    Mirrors pebble_router._hits canonicalization so 'Alpha, Beta!  pineapple'
    and 'alpha beta pineapple' match identically.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def load_triggers(actions_cfg: Any) -> dict[str, dict[str, str]]:
    """Validate the ``actions:`` config block. Raises on invalid (fail-closed).

    Expected shape:
        actions:
          enabled: true
          rate_limit_per_hour: 3
          triggers:
            phone_call:
              phrase: "alpha beta pineapple"
              url: "https://ntfy.sh/<topic>"
              headers: {Title: "...", Tags: "..."}
              body: "static text - transcript is never embedded"
    """
    if actions_cfg is None:
        return {}
    if not isinstance(actions_cfg, dict):
        raise ValueError("actions must be a mapping")
    triggers = actions_cfg.get("triggers") or {}
    if not isinstance(triggers, dict):
        raise ValueError("actions.triggers must be a mapping")
    validated: dict[str, dict[str, str]] = {}
    for name, t in triggers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError(f"actions.triggers: bad action name {name!r}")
        if not isinstance(t, dict):
            raise ValueError(f"actions.triggers.{name} must be a mapping")
        phrase = _canon(str(t.get("phrase", "")))
        if len(phrase) < 3:
            raise ValueError(f"actions.triggers.{name}.phrase must be >= 3 chars")
        url = str(t.get("url", ""))
        if not url.lower().startswith(("https://", "http://")):
            raise ValueError(f"actions.triggers.{name}.url must be http(s)")
        validated[name] = {
            "phrase": phrase,
            "url": url,
            "headers": t.get("headers") or {},
            "body": str(t.get("body", "")),
        }
    return validated


def get_actions() -> tuple[bool, int, dict[str, dict[str, str]]]:
    """Read config.yaml FRESH and return (enabled, rate_limit, validated_triggers).

    Fresh read per press gives the kill switch (``actions.enabled: false``)
    next-press effect, mirroring routing.yaml semantics. Raises on invalid
    config - caller fails closed (no action), the press still forwards.
    """
    import os

    import yaml

    path = os.environ.get("INDEX01_CONFIG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    actions = data.get("actions") or {}
    if not isinstance(actions, dict):
        raise ValueError("actions must be a mapping")
    enabled = bool(actions.get("enabled", False))
    rate = int(actions.get("rate_limit_per_hour", DEFAULT_RATE_LIMIT_PER_HOUR))
    triggers = load_triggers(actions)
    return enabled, rate, triggers


def match_action(transcript: str, triggers: dict[str, dict[str, str]]) -> Optional[str]:
    """Return the action whose phrase is a PREFIX of the canonicalized transcript.

    Longest phrase wins when several match. None when nothing matches.
    """
    if not isinstance(transcript, str):
        return None
    canon = _canon(transcript)
    matches = [
        (len(cfg["phrase"]), name)
        for name, cfg in triggers.items()
        if canon.startswith(cfg["phrase"])
    ]
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _rate_ok(name: str, limit: int, now: Optional[float] = None) -> bool:
    """Sliding-hour cap. True when this fire is within the limit."""
    now = now if now is not None else time.time()
    window = _RATE.setdefault(name, [])
    cutoff = now - 3600.0
    _RATE[name] = [ts for ts in window if ts > cutoff]
    if len(_RATE[name]) >= max(1, limit):
        return False
    _RATE[name].append(now)
    return True


async def fire_action(
    session: Optional[aiohttp.ClientSession],
    trigger: dict[str, str],
    *,
    delivery: str,
    rate_limit_per_hour: int = DEFAULT_RATE_LIMIT_PER_HOUR,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """POST the trigger to its endpoint. Returns (ok, detail).

    The body is STATIC config text - the untrusted transcript is never
    embedded in the push (injection surface: zero). Best-effort like the
    WhatsApp ack: an action failure is logged and reported, never raised
    into the press path.
    """
    if session is None:
        return False, "no http session"
    if not _rate_ok(_rate_key(trigger, delivery), rate_limit_per_hour):
        log.warning("action rate-limited", extra={"json_fields": {"delivery": delivery}})
        return False, "rate-limited"
    headers = {str(k): str(v) for k, v in (trigger.get("headers") or {}).items()}
    body: Any = trigger.get("body") or ""
    try:
        async with session.post(
            trigger["url"],
            data=body.encode("utf-8") if isinstance(body, str) else body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            await resp.read()
            ok = 200 <= resp.status < 300
            return ok, f"HTTP {resp.status}"
    except Exception as exc:
        log.warning(
            "action fire failed",
            extra={"json_fields": {"delivery": delivery, "error": str(exc)[:120]}},
        )
        return False, f"{exc.__class__.__name__}: {str(exc)[:80]}"


def _rate_key(trigger: dict[str, str], delivery: str) -> str:
    """Rate-limit key: the trigger URL (stable per action, no PII)."""
    return trigger.get("url", delivery)
