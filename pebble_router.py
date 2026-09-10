"""Topic routing for pebble presses (Option C from the Sep 9 2026 research synthesis).

Design (fail-closed, no model->config write path):
- routing.yaml is read FRESH on every press (user edits take effect next press).
- Keyword gate first: deterministic, runs before any LLM. Sensitive hits force intake.
- Label candidates from keywords only; the LLM (pebble session agent) confirms ONE
  label from this enum. Code maps label->JID. A spoken instruction can never add a
  route, change a JID, or disable the sensitive gate.
- Low confidence / no candidate / any error -> intake. Never forward on doubt.

The router NEVER touches routing.yaml. Config is an input, never an output.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("index01.router")

ROUTING_FILE = Path(__file__).resolve().parent / "routing.yaml"


@dataclass
class RouteDecision:
    label: str                      # "intake" or a route label from routing.yaml
    chat_id: str                    # resolved JID (or intake JID)
    reason: str                     # one sentence, shown in the ack
    candidates: list[str] = field(default_factory=list)
    sensitive: bool = False
    confidence: float = 0.0


def load_routing(path: Path = ROUTING_FILE) -> dict:
    """Read + schema-validate routing.yaml. Raises on invalid config (fail-closed)."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("routing.yaml: top level must be a mapping")
    intake = raw.get("intake_chat_id")
    if not isinstance(intake, str) or not intake.endswith("@g.us"):
        raise ValueError("routing.yaml: intake_chat_id must be a group JID")
    routes = raw.get("routes") or {}
    if not isinstance(routes, dict):
        raise ValueError("routing.yaml: routes must be a mapping")
    for label, r in routes.items():
        if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_]+", label):
            raise ValueError(f"routing.yaml: bad label {label!r} (lowercase word chars only)")
        if not isinstance(r, dict) or not str(r.get("chat_id", "")).endswith("@g.us"):
            raise ValueError(f"routing.yaml: {label}.chat_id must be a group JID")
        kws = r.get("keywords") or []
        if not isinstance(kws, list) or not all(isinstance(k, str) and k for k in kws):
            raise ValueError(f"routing.yaml: {label}.keywords must be a list of strings")
    gate = raw.get("sensitive_gate") or []
    if not isinstance(gate, list) or not all(isinstance(g, str) and g for g in gate):
        raise ValueError("routing.yaml: sensitive_gate must be a list of strings")
    return raw


def _hits(text: str, terms: list[str]) -> list[str]:
    """Keyword hits against WHITESPACE- AND PUNCTUATION-CANONICALIZED text.

    Canonicalization closes the bypass the adversarial reviewer found: multi-word
    gates like 'blood pressure' survived 'blood  pressure' (double space) or
    'blood-pressure'. Subword false positives ('password' in 'pass worded') are
    acceptable - the gate only ever forces INTAKE (over-blocking is safe).
    """
    canon = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    hits = []
    for t in terms:
        t_canon = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
        if " " in t_canon:
            if t_canon in canon:
                hits.append(t)
        else:
            if re.search(rf"\b{re.escape(t_canon)}\b", canon):
                hits.append(t)
    return hits


def sensitive_hit(transcript: str, cfg: dict) -> list[str]:
    """Deterministic sensitive-topic gate. Overrides everything."""
    return _hits(transcript.lower(), cfg.get("sensitive_gate") or [])


def keyword_candidates(transcript: str, cfg: dict) -> list[str]:
    """Route labels whose keywords appear in the transcript, best (most hits) first."""
    scored = []
    for label, r in (cfg.get("routes") or {}).items():
        hits = _hits(transcript.lower(), r.get("keywords") or [])
        if hits:
            scored.append((len(hits), label))
    scored.sort(reverse=True)
    return [label for _, label in scored]


def route_press(transcript: str, *, llm_label: Optional[str] = None,
                llm_confidence: float = 0.0, routing_path: Path = ROUTING_FILE) -> RouteDecision:
    """Full routing decision for one press. NEVER raises; errors resolve to intake.

    Order of authority (per research synthesis):
      1. sensitive gate -> intake (always)
      2. keyword candidates -> if exactly one, route (deterministic)
      3. llm confirmation among candidates -> route if it picks one of them
      4. otherwise -> intake
    """
    intake = RouteDecision(label="intake", chat_id="",
                           reason="default intake", candidates=[], confidence=0.0)
    try:
        cfg = load_routing(routing_path)
        intake.chat_id = cfg["intake_chat_id"]
    except Exception as e:
        log.error("routing.yaml invalid, failing closed to intake: %s", e)
        # intake JID unknown too - caller falls back to adapter default ack
        intake.reason = f"routing.yaml invalid ({e.__class__.__name__}) - intake"
        return intake

    sens = sensitive_hit(transcript, cfg)
    if sens:
        return RouteDecision(
            label="intake", chat_id=cfg["intake_chat_id"],
            reason=f"sensitive content detected ({sens[0]}) - kept in intake",
            sensitive=True,
        )

    cands = keyword_candidates(transcript, cfg)
    if len(cands) == 1:
        label = cands[0]
        return RouteDecision(
            label=label, chat_id=cfg["routes"][label]["chat_id"],
            reason=f"matched {label} keywords", candidates=cands, confidence=1.0,
        )

    # 0 or 2+ keyword candidates: only the LLM can disambiguate, and only to a label
    # that the keyword pass surfaced. Model inventing a label = intake.
    if llm_label and llm_label in cands and llm_confidence >= 0.6:
        return RouteDecision(
            label=llm_label, chat_id=cfg["routes"][llm_label]["chat_id"],
            reason=f"classified as {llm_label} (confidence {llm_confidence:.0%})",
            candidates=cands, confidence=llm_confidence,
        )

    if not cands:
        return RouteDecision(label="intake", chat_id=cfg["intake_chat_id"],
                             reason="no topic match", confidence=0.0)
    return RouteDecision(label="intake", chat_id=cfg["intake_chat_id"],
                         reason=f"ambiguous between {', '.join(cands)} - kept in intake",
                         candidates=cands, confidence=llm_confidence)
