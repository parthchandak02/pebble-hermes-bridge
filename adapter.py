"""index01-adapter — bridge Pebble Index 01 app webhooks to the Hermes webhook platform.

Receives multipart/form-data POSTs from the Pebble Index 01 Android/iOS app on
127.0.0.1:8645 (any path), verifies the Pebble HMAC signature over the raw request
body, forwards a minimal JSON event to Hermes's webhook endpoint, and relays Hermes's
HTTP status back to Pebble.

Design goals
------------
* No LLM latency in the request path — parse, verify, forward, relay. Nothing async-spawned
  inside the handler that depends on an external model.
* Secrets never hardcoded — loaded from ``config.yaml`` (or env overrides) sitting next to
  this module.
* Raw multipart body bytes are read *once* and reused for (a) HMAC verification and
  (b) multipart parsing, so an attacker cannot race the body between signing and parsing.
* Constant-time signature comparison via :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp
import yaml
from aiohttp import web

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Base for adapter-originated request rejections."""

    def __init__(self, status: int, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.fields = fields


class BadRequest(AdapterError):
    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(400, reason, **fields)


class Forbidden(AdapterError):
    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(403, reason, **fields)


class PayloadTooLarge(AdapterError):
    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(413, reason, **fields)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line for machine-friendly processing."""

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        extra = getattr(record, "json_fields", None)
        if isinstance(extra, dict):
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, separators=(",", ":"))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Runtime configuration. All secrets come from config.yaml or env vars."""

    signing_secret: Optional[str] = None  # Pebble signing secret; None disables verification
    bearer_token: Optional[str] = None    # Pebble Authorization bearer; auths test events
    route_secret: str = ""                # Hermes route secret (outbound signing)
    hermes_url: str = ""                  # Full Hermes webhook endpoint URL
    listen_host: str = "127.0.0.1"
    listen_port: int = 8645
    max_body_size: int = 5 * 1024 * 1024     # inbound multipart cap (5 MB)
    max_timestamp_age: int = 300             # seconds an X-Index-Timestamp may be old
    log_level: str = "INFO"
    request_timeout: float = 20.0            # outbound Hermes request timeout

    @property
    def signing_enabled(self) -> bool:
        return self.signing_secret is not None and self.signing_secret != ""


def _yaml_loader() -> tuple[Any, Optional[str]]:
    path = os.environ.get("INDEX01_CONFIG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    if not os.path.exists(path):
        return {}, path
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data, path


def load_config() -> Config:
    """Load config from config.yaml, then layer env-var overrides on top.

    Env vars (all optional, highest precedence):
        INDEX01_CONFIG            path to config file (default: ./config.yaml)
        INDEX01_SIGNING_SECRET    Pebble signing secret
        INDEX01_ROUTE_SECRET      Hermes route secret
        INDEX01_HERMES_URL        Hermes webhook endpoint
        INDEX01_LISTEN_HOST       bind host
        INDEX01_LISTEN_PORT       bind port
        INDEX01_MAX_BODY          max inbound multipart body bytes
        INDEX01_LOG_LEVEL         INFO / DEBUG / WARNING / ERROR
    """
    data, path = _yaml_loader()

    def _get(*keys: str, default: Any = None) -> Any:
        node = data
        for key in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
            if node is None:
                return default
        return node

    env = os.environ
    signing = env.get("INDEX01_SIGNING_SECRET", _get("pebble", "signing_secret"))
    bearer = env.get("INDEX01_BEARER_TOKEN", _get("pebble", "bearer_token"))
    route = env.get("INDEX01_ROUTE_SECRET", _get("hermes", "route_secret"))
    hermes_url = env.get("INDEX01_HERMES_URL", _get("hermes", "url"))
    host = env.get("INDEX01_LISTEN_HOST", _get("listen", "host", default="127.0.0.1"))
    port = int(env.get("INDEX01_LISTEN_PORT", _get("listen", "port", default=8645)))
    max_body = int(
        env.get("INDEX01_MAX_BODY", _get("pebble", "max_body_size", default=5 * 1024 * 1024))
    )
    age = int(_get("pebble", "max_timestamp_age", default=300))
    log_level = env.get("INDEX01_LOG_LEVEL", _get("log_level", default="INFO"))

    missing = [k for k, v in (("route_secret", route), ("hermes_url", hermes_url)) if not v]
    if missing:
        raise ValueError(
            "Missing required config: %s (set in %s or via INDEX01_%s)"
            % (", ".join(missing), path, " / INDEX01_".join(x.upper() for x in missing))
        )

    if not signing or signing == "":
        if env.get("INDEX01_ALLOW_UNSIGNED") != "1":
            raise ValueError(
                "signing_secret is required (set pebble.signing_secret in %s or "
                "INDEX01_SIGNING_SECRET; set INDEX01_ALLOW_UNSIGNED=1 to run without verification)"
                % path
            )

    return Config(
        signing_secret=signing,
        bearer_token=bearer,
        route_secret=route,
        hermes_url=hermes_url,
        listen_host=str(host),
        listen_port=port,
        max_body_size=max_body,
        max_timestamp_age=age,
        log_level=log_level,
    )


def extract_boundary(content_type: str) -> Optional[str]:
    """Return the multipart boundary token from a Content-Type header, or None.

    Handles both ``boundary=XYZ`` and quoted ``boundary="XYZ"`` forms.
    """
    if not content_type.lower().startswith("multipart/form-data"):
        return None
    m = re.search(r"boundary\s*=\s*(?:\"([^\"]+)\"|([^;]+))", content_type, re.IGNORECASE)
    if not m:
        return None
    token = (m.group(1) or m.group(2) or "").strip()
    return token or None


# ---------------------------------------------------------------------------
# Multipart parsing (operates on the raw bytes already read for HMAC)
# ---------------------------------------------------------------------------


class MultipartError(ValueError):
    """Raised when a multipart body is malformed."""


def _parse_content_disposition(header_block: str) -> tuple[Optional[str], Optional[str]]:
    """Extract ``name`` and ``filename`` fields from a part's header block.

    Returns (name, filename); filename is present only for file/attached parts
    (e.g. the ``audio`` m4a part).
    """
    name: Optional[str] = None
    filename: Optional[str] = None
    for line in header_block.split("\r\n"):
        if not line.lower().lstrip().startswith("content-disposition:"):
            continue
        for segment in line.split(";")[1:]:
            segment = segment.strip()
            m = re.match(r"name=\"([^\"]*)\"", segment)
            if m:
                name = m.group(1)
                continue
            m = re.match(r"filename=\"([^\"]*)\"", segment)
            if m:
                filename = m.group(1)
    return name, filename


def parse_multipart(body: bytes, boundary: str) -> dict[str, str]:
    """Parse ``multipart/form-data`` payload into a {field_name: text value} map.

    Non-file text parts are decoded UTF-8. File parts (those carrying a
    ``filename``, e.g. the ``audio`` m4a) are skipped entirely — we discard audio.
    Parsing is deliberately independent of aiohttp's internal stream machinery so
    it can run directly over the raw body bytes used for HMAC verification.

    :raises MultipartError: for malformed framing (missing boundary, no header
        terminator, unterminated part).
    """
    delimiter = b"--" + boundary.encode("utf-8")
    pos = body.find(delimiter)
    if pos == -1:
        raise MultipartError("boundary delimiter not found in body")
    pos += len(delimiter)

    fields: dict[str, str] = {}
    part_count = 0
    while True:
        if body[pos : pos + 2] == b"--":  # closing delimiter (--boundary--)
            break
        if body[pos : pos + 2] == b"\r\n":
            pos += 2
        else:
            raise MultipartError("expected CRLF after boundary")

        part_count += 1
        if part_count > 32:
            raise MultipartError("too many multipart parts")

        header_end = body.find(b"\r\n\r\n", pos)
        if header_end == -1:
            raise MultipartError("part header terminator not found")
        if header_end - pos > 8192:
            raise MultipartError("part header block too large")
        header_block = body[pos:header_end].decode("latin-1", errors="replace")
        pos = header_end + 4

        data_end = body.find(delimiter, pos)
        if data_end == -1:
            raise MultipartError("part data unterminated (no following boundary)")
        chunk = body[pos:data_end]
        # The CRLF immediately preceding the boundary is the boundary terminator,
        # not part content (RFC 2046). Strip a single trailing CRLF.
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        pos = data_end + len(delimiter)

        name, filename = _parse_content_disposition(header_block)
        if name is not None and not filename:
            fields[name] = chunk.decode("utf-8", errors="replace")

    return fields


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def build_pebble_canonical_bytes(
    *,
    timestamp: str,
    delivery_id: str,
    trigger_value: str,
    is_test: bool,
    raw_body: bytes,
) -> bytes:
    """Assemble the exact Pebble HMAC canonical string and return its UTF-8 bytes.

    Canonical form (verbatim, from IndexWebhookApi.kt):
        "v1\\n<timestamp>\\n<deliveryId>\\n<triggerValue>\\n<0|1>\\n" + RAW_MULTIPART_BODY_BYTES

    ``is_test`` encodes to ``"1"`` for test events, ``"0"`` otherwise.
    """
    is_test_flag = "1" if is_test else "0"
    prefix = f"v1\n{timestamp}\n{delivery_id}\n{trigger_value}\n{is_test_flag}\n".encode("utf-8")
    return prefix + raw_body


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    """Return lowercase hex HMAC-SHA256 of ``message`` keyed with ``key``."""
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_pebble_signature(
    signing_secret: str,
    *,
    timestamp: str,
    delivery_id: str,
    trigger_value: str,
    is_test: bool,
    raw_body: bytes,
    provided: str,
) -> bool:
    """Constant-time check of the Pebble signature over the canonical string.

    Uses :func:`hmac.compare_digest`, which resists timing attacks against the
    digest comparison. Returns True only if the recomputed signature matches the
    header value byte-for-byte.
    """
    canonical = build_pebble_canonical_bytes(
        timestamp=timestamp,
        delivery_id=delivery_id,
        trigger_value=trigger_value,
        is_test=is_test,
        raw_body=raw_body,
    )
    expected = hmac_sha256_hex(signing_secret.encode("utf-8"), canonical)
    return hmac.compare_digest(expected, provided.lower())


def build_hermes_message(timestamp: str, body_bytes: bytes) -> bytes:
    """Assemble the Hermes outbound HMAC message: ``f"{timestamp}.{body}"``."""
    return timestamp.encode("utf-8") + b"." + body_bytes


def sign_hermes_request(route_secret: str, timestamp: str, body_bytes: bytes) -> str:
    """Compute the X-Webhook-Signature-V2 value for a Hermes forward."""
    message = build_hermes_message(timestamp, body_bytes)
    return hmac_sha256_hex(route_secret.encode("utf-8"), message)


# ---------------------------------------------------------------------------
# Outbound Hermes client
# ---------------------------------------------------------------------------


class HermesForwarder:
    """Sends verified events to the Hermes webhook platform and returns its status."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        route_secret: str,
        url: str,
        timeout: float = 20.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._route_secret = route_secret
        self._url = url
        self._timeout = timeout
        self._clock = clock

    async def send(self, payload: dict[str, Any], delivery: str) -> int:
        """Serialize ``payload`` exactly, sign, and POST; return the HTTP status.

        The body bytes are serialized once and the signature is computed over that
        exact serialization, so request and signature can never diverge.
        """
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(self._clock()))
        signature = sign_hermes_request(self._route_secret, timestamp, body_bytes)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": signature,
            "X-Request-ID": delivery,
        }
        try:
            async with self._session.post(
                self._url, data=body_bytes, headers=headers, timeout=self._timeout
            ) as resp:
                status = resp.status
                await resp.read()  # drain; Hermes may include a short body
                return status
        except aiohttp.ClientError as exc:
            raise AdapterError(502, f"forward to Hermes failed: {exc}", state="hermes_unreachable")
        except TimeoutError as exc:
            raise AdapterError(504, f"forward to Hermes timed out: {exc}", state="hermes_timeout")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Ctx:
    """Bundles injected dependencies so tests can swap forwarder/clock freely."""

    def __init__(
        self, cfg: Config, forwarder: HermesForwarder, clock: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self.forwarder = forwarder
        self.clock = clock


CTX_KEY: web.AppKey = web.AppKey("ctx", _Ctx)
SESSION_KEY: web.AppKey = web.AppKey("session", aiohttp.ClientSession)


async def dispatch(request: web.Request, ctx: _Ctx) -> web.Response:
    """Process one Pebble webhook and return the response to relay to Pebble."""
    cfg = ctx.cfg
    log = get_logger("index01.dispatch")
    delivery: Optional[str] = None
    trigger: str = ""
    try:
        # --- content type -------------------------------------------------
        content_type = request.headers.get("Content-Type", "")
        boundary = extract_boundary(content_type)
        if boundary is None:
            raise BadRequest("not multipart/form-data", content_type=content_type)

        # --- size guard (before reading the stream into memory) ----------
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length.strip()) > cfg.max_body_size:
                    raise PayloadTooLarge("body exceeds max_body_size", header_size=content_length)
            except ValueError:
                pass

        raw_body = await request.read()  # aiohttp enforces client_max_size as well
        if len(raw_body) > cfg.max_body_size:
            raise PayloadTooLarge("body exceeds max_body_size", actual_size=len(raw_body))

        # --- signature headers -------------------------------------------
        signature = request.headers.get("X-Index-Signature", "")
        ts_header = request.headers.get("X-Index-Timestamp", "")
        delivery = request.headers.get("X-Index-Delivery", "")
        trigger = request.headers.get("X-Index-Trigger", "")
        test_header = request.headers.get("X-Index-Test", "").strip().lower() == "true"

        # Parse the multipart now so the test flag from the body can feed the HMAC.
        fields = parse_multipart(raw_body, boundary)
        is_test = test_header or fields.get("test", "").strip().lower() == "true"

        def _bearer_ok() -> bool:
            expected = f"Bearer {cfg.bearer_token}" if cfg.bearer_token else None
            provided = request.headers.get("Authorization", "").strip()
            return bool(expected) and hmac.compare_digest(expected, provided)

        if signature or ts_header:
            # Signed mode: the HMAC binds the RAW header values, so verify before
            # any synthesis (an omitted header is signed as the empty string).
            if cfg.signing_enabled:
                if not ts_header or not re.fullmatch(r"[0-9]{1,13}", ts_header):
                    raise Forbidden("missing or malformed X-Index-Timestamp")
                if not signature:
                    raise Forbidden("missing X-Index-Signature")
                ts_seconds = int(ts_header)
                now = int(ctx.clock())
                if ts_seconds > now:
                    raise Forbidden(
                        "X-Index-Timestamp is in the future",
                        skew=ts_seconds - now,
                    )
                if now - ts_seconds > cfg.max_timestamp_age:
                    raise Forbidden(
                        "X-Index-Timestamp is stale / replay",
                        age=now - ts_seconds,
                        max_age=cfg.max_timestamp_age,
                    )
                if not verify_pebble_signature(
                    cfg.signing_secret,
                    timestamp=ts_header,
                    delivery_id=delivery,
                    trigger_value=trigger,
                    is_test=is_test,
                    raw_body=raw_body,
                    provided=signature,
                ):
                    raise Forbidden("signature verification failed", delivery=delivery)
        elif _bearer_ok():
            # Unsigned mode (app "Sign requests" off): upstream IndexWebhookApi.kt
            # only sends X-Index-Delivery/X-Index-Trigger alongside a signature, so
            # Bearer-only apps omit them. Authenticated via the verbatim Bearer
            # header; synthesize the idempotency/gesture keys.
            if not delivery:
                delivery = f"unsigned-{uuid.uuid4().hex[:12]}"
            if not trigger:
                trigger = "test-event" if is_test else "single-click-hold"
        elif is_test:
            # App "Send test event" carries no signature headers by design; the
            # user-set Authorization bearer (sent verbatim per official docs)
            # is the auth.
            raise Forbidden("test event without signature requires matching Authorization header")
        else:
            raise Forbidden("unsigned request requires a matching Authorization header")

        # --- transcript guard -----------------------------------------------
        transcript = fields.get("transcription", "")
        if len(transcript) < 3:
            log.info(
                "transcript too short; acknowledged without forwarding (garbled-guard)",
                extra={
                    "json_fields": {
                        "delivery": delivery,
                        "trigger": trigger,
                        "len": len(transcript),
                    }
                },
            )
            return web.Response(status=200, text="ok")

        recorded_at_field = fields.get("recordedAt", "")
        try:
            recorded_at = int(recorded_at_field)
        except (TypeError, ValueError) as exc:
            raise BadRequest("recordedAt is missing or not an epoch-ms integer") from exc

        payload = {
            "transcript": transcript,
            "recordedAt": recorded_at,
            "trigger": trigger,
            "delivery": delivery,
            "isTest": is_test,
        }

        status = await ctx.forwarder.send(payload, delivery)
        log.info(
            "forwarded to Hermes",
            extra={
                "json_fields": {
                    "delivery": delivery,
                    "trigger": trigger,
                    "hermes_status": status,
                }
            },
        )
        return web.Response(status=status, text="")
    except AdapterError as exc:
        log.warning(
            "request rejected",
            extra={
                "json_fields": {
                    "status": exc.status,
                    "reason": exc.reason,
                    "delivery": delivery,
                    **exc.fields,
                }
            },
        )
        return web.Response(status=exc.status, text=exc.reason)
    except MultipartError as exc:
        log.warning(
            "request rejected",
            extra={"json_fields": {"delivery": delivery, "reason": "multipart_parse"}},
        )
        return web.Response(status=400, text=f"malformed multipart body: {exc}")
    except Exception as exc:
        log.error(
            "unexpected error in dispatch",
            extra={"json_fields": {"delivery": delivery, "trigger": trigger}},
            exc_info=True,
        )
        return web.Response(status=500, text="internal error")


@web.middleware
async def error_middleware(request: web.Request, handler: Callable[[web.Request], Any]) -> web.Response:
    """Catch framework-generated HTTP errors (e.g. 413 body-too-large) and any
    unexpected application exception, log them in a structured way, and return a
    clean response — never leak an exception trace toward Pebble."""
    log = get_logger("index01.http")
    try:
        return await handler(request)
    except web.HTTPException as exc:
        log.info(
            "framework http response",
            extra={"json_fields": {"status": exc.status, "path": request.path}},
        )
        return web.json_response(
            {"error": exc.reason or "error"},
            status=exc.status,
        )
    except AdapterError as exc:
        # Already relayed by dispatch; defensive in case one escapes.
        log.warning(
            "adapter error at middleware",
            extra={"json_fields": {"status": exc.status, "reason": exc.reason}},
        )
        return web.Response(status=exc.status, text=exc.reason)
    except aiohttp.ClientError as exc:
        log.error(
            "outbound client error",
            extra={"json_fields": {"path": request.path, "error": str(exc)}},
            exc_info=True,
        )
        return web.Response(status=502, text="gateway error")
    except OSError as exc:
        log.error(
            "os error",
            extra={"json_fields": {"path": request.path, "error": str(exc)}},
            exc_info=True,
        )
        return web.Response(status=500, text="internal error")


def build_app(
    cfg: Config,
    forwarder: Optional[HermesForwarder] = None,
    clock: Callable[[], float] = time.time,
) -> web.Application:
    """Assemble the aiohttp app. A forwarder is created on startup if not supplied.

    The outbound ClientSession is created inside on_startup so a running event
    loop exists (launchd cold-start has none; creating a session at build time
    raises 'no running event loop' on Python 3.12+).
    """
    app = web.Application(client_max_size=cfg.max_body_size, middlewares=[error_middleware])
    ctx = _Ctx(cfg=cfg, forwarder=forwarder, clock=clock)

    async def _on_startup(app: web.Application) -> None:
        if ctx.forwarder is None:
            session = aiohttp.ClientSession()
            ctx.forwarder = HermesForwarder(
                session, cfg.route_secret, cfg.hermes_url, timeout=cfg.request_timeout
            )
            app[SESSION_KEY] = session

    async def _on_cleanup(app: web.Application) -> None:
        session: Optional[aiohttp.ClientSession] = app.get(SESSION_KEY)
        if session is not None:
            await session.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    async def _any_path(request: web.Request) -> web.Response:
        return await dispatch(request, ctx)

    async def _healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_route("GET", "/healthz", _healthz)
    app.router.add_route("POST", "/{tail:.*}", _any_path)
    # Convenience catch for bare "/" too (aiohttp matches root to the wildcard above,
    # but be explicit and safe regardless of version behavior).
    app.router.add_route("POST", "/", _any_path)
    app[CTX_KEY] = ctx
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="index01-adapter: Pebble -> Hermes bridge")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: next to module)")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    parser.add_argument("--log-level", default=None, help="override log level")
    args = parser.parse_args()

    if args.config:
        os.environ["INDEX01_CONFIG"] = args.config
    cfg = load_config()
    if args.port is not None:
        cfg = Config(**{**cfg.__dict__, "listen_port": args.port})  # type: ignore[arg-type]
    log_level = args.log_level or cfg.log_level
    setup_logging(log_level)
    log = get_logger("index01")
    if not cfg.signing_enabled:
        log.critical(
            "signing_secret is not configured; inbound requests will not be authenticated",
            extra={"json_fields": {"allow_unsigned": True}},
        )
    log.info(
        "starting index01-adapter",
        extra={
            "json_fields": {
                "listen_host": cfg.listen_host,
                "listen_port": cfg.listen_port,
                "hermes_url": cfg.hermes_url,
                "signing_enabled": cfg.signing_enabled,
                "max_body_size": cfg.max_body_size,
            }
        },
    )
    app = build_app(cfg)
    web.run_app(app, host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()