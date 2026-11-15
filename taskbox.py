#!/usr/bin/env python3
"""taskbox: one task inbox on this machine that every agent shares.

The single source of truth for tasks: ring presses, things assigned on WhatsApp,
and work handed to other bots all land in one SQLite file next to this script
(``data/taskbox.db``, git-ignored). Agents reach it through a small MCP server
(Streamable HTTP, JSON responses, tools only) on 127.0.0.1:8646.

* Each agent has its own bearer token (``taskbox.secrets.env``), so "who did
  this" in the history is authenticated, never self-declared.
* Admin agents (Hermes) see and change everything. External agents (Grok Bot)
  only ever see tasks assigned to them, and sensitive tasks can never be
  assigned to an external agent.
* Assigning a task to an agent with a ``webhook_url`` rings its doorbell: one
  POST with the task id, title and body, so the bot starts without polling.

Stdlib + aiohttp + PyYAML only, like the adapter. Never logs tokens or bodies.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "taskbox.db"
DEFAULT_SECRETS = HERE / "taskbox.secrets.env"

STATUSES = ("open", "claimed", "done", "failed", "cancelled")
FINAL = ("done", "failed", "cancelled")
MAX_TITLE = 200
MAX_BODY = 20_000
MAX_RESULT = 50_000
MAX_NOTE = 5_000
MAX_REQUEST = 256 * 1024
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
TASK_RE = re.compile(r"^t_[0-9a-f]{10}$")

log = logging.getLogger("taskbox")


class TaskError(Exception):
    """A tool call the caller got wrong (bad id, not allowed, bad value)."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    source      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    assignee    TEXT,
    claimed_by  TEXT,
    result      TEXT,
    sensitive   INTEGER NOT NULL DEFAULT 0,
    origin_ref  TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS tasks_assignee ON tasks(assignee, status);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_origin ON tasks(source, origin_ref) WHERE origin_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT NOT NULL,
    at       INTEGER NOT NULL,
    actor    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
"""


def _clip(value: Any, limit: int, name: str) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        raise TaskError(f"{name} is longer than {limit} characters")
    return text


class Store:
    """SQLite task store. Safe to open from the adapter and the server at once (WAL)."""

    def __init__(self, path: Path | str = DEFAULT_DB, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- helpers ------------------------------------------------------------
    def _now(self) -> int:
        return int(self.clock())

    def _event(self, task_id: str, actor: str, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(task_id, at, actor, kind, detail) VALUES (?,?,?,?,?)",
            (task_id, self._now(), actor, kind, detail[:MAX_NOTE]),
        )

    def _row(self, task_id: str) -> sqlite3.Row:
        if not isinstance(task_id, str) or not TASK_RE.match(task_id):
            raise TaskError("task_id must look like t_0123456789")
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskError(f"no task {task_id}")
        return row

    @staticmethod
    def as_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["sensitive"] = bool(d["sensitive"])
        return d

    # -- operations ---------------------------------------------------------
    def create(
        self,
        *,
        actor: str,
        title: str,
        body: str = "",
        source: Optional[str] = None,
        assignee: Optional[str] = None,
        sensitive: bool = False,
        origin_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        title = _clip(title, MAX_TITLE, "title").strip()
        if not title:
            raise TaskError("title is required")
        body = _clip(body, MAX_BODY, "body")
        source = (source or actor).strip().lower()
        if not AGENT_RE.match(source):
            raise TaskError("source must be a short lowercase name")
        now = self._now()
        if origin_ref is not None:
            dup = self.db.execute(
                "SELECT * FROM tasks WHERE source=? AND origin_ref=?", (source, origin_ref)
            ).fetchone()
            if dup is not None:  # a retried ring press: same delivery id, same task
                return self.as_dict(dup)
        task_id = "t_" + secrets.token_hex(5)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO tasks(id, created_at, updated_at, source, created_by, title, body,"
                " status, assignee, sensitive, origin_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, now, now, source, actor, title, body, "open", assignee,
                 1 if sensitive else 0, origin_ref),
            )
            self._event(task_id, actor, "created", f"source={source}" + (f" assignee={assignee}" if assignee else ""))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        return self.as_dict(self._row(task_id))

    def history(self, task_id: str) -> list[dict[str, Any]]:
        self._row(task_id)
        rows = self.db.execute(
            "SELECT at, actor, kind, detail FROM events WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list(
        self,
        *,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        include_sensitive: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if status == "active":
            clauses.append("status IN ('open','claimed')")
        elif status:
            if status not in STATUSES:
                raise TaskError(f"status must be one of {', '.join(STATUSES)} or active")
            clauses.append("status=?")
            args.append(status)
        if assignee:
            clauses.append("assignee=?")
            args.append(assignee)
        if not include_sensitive:
            clauses.append("sensitive=0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit or 20), 100))
        rows = self.db.execute(
            f"SELECT * FROM tasks {where} ORDER BY updated_at DESC, id LIMIT ?", (*args, limit)
        ).fetchall()
        return [self.as_dict(r) for r in rows]

    def assign(self, task_id: str, assignee: Optional[str], *, actor: str) -> dict[str, Any]:
        row = self._row(task_id)
        if row["status"] in FINAL:
            raise TaskError(f"task is {row['status']}; reopen it before assigning")
        self.db.execute(
            "UPDATE tasks SET assignee=?, status='open', claimed_by=NULL, updated_at=? WHERE id=?",
            (assignee, self._now(), task_id),
        )
        self._event(task_id, actor, "assigned", assignee or "(nobody)")
        return self.get(task_id)

    def claim(self, task_id: str, *, actor: str) -> dict[str, Any]:
        row = self._row(task_id)
        if row["assignee"] not in (None, actor):
            raise TaskError(f"task is assigned to {row['assignee']}")
        cur = self.db.execute(
            "UPDATE tasks SET status='claimed', claimed_by=?, assignee=COALESCE(assignee, ?),"
            " updated_at=? WHERE id=? AND status='open'",
            (actor, actor, self._now(), task_id),
        )
        if cur.rowcount != 1:
            raise TaskError(f"task is {row['status']}, not open (claimed by {row['claimed_by']})")
        self._event(task_id, actor, "claimed")
        return self.get(task_id)

    def update(
        self,
        task_id: str,
        *,
        actor: str,
        is_admin: bool,
        status: Optional[str] = None,
        result: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        row = self._row(task_id)
        if not is_admin and actor not in (row["assignee"], row["claimed_by"]):
            raise TaskError("only the assigned agent can update this task")
        if status is not None and status not in STATUSES:
            raise TaskError(f"status must be one of {', '.join(STATUSES)}")
        if status == "open" and not is_admin:
            raise TaskError("only an admin can reopen a task")
        result = None if result is None else _clip(result, MAX_RESULT, "result")
        note = None if note is None else _clip(note, MAX_NOTE, "note")
        if status is None and result is None and not note:
            raise TaskError("nothing to update: give a status, result or note")
        sets, args = ["updated_at=?"], [self._now()]
        if status is not None:
            sets.append("status=?")
            args.append(status)
            if status == "open":
                sets.append("claimed_by=NULL")
        if result is not None:
            sets.append("result=?")
            args.append(result)
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, task_id))
        if note:
            self._event(task_id, actor, "note", note)
        if status is not None and status != row["status"]:
            self._event(task_id, actor, status)
        if result is not None:
            self._event(task_id, actor, "result", result[:500])
        return self.get(task_id)


# ---------------------------------------------------------------------------
# Agents, config and secrets
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    name: str
    token: str
    admin: bool = False
    external: bool = False
    webhook_url: str = ""
    webhook_key: str = ""


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8646
    db_path: Path = DEFAULT_DB
    agents: dict[str, Agent] = field(default_factory=dict)
    allowed_origins: tuple[str, ...] = ()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def load_server_config(config_path: Optional[Path] = None, secrets_path: Optional[Path] = None) -> ServerConfig:
    import yaml

    config_path = Path(config_path or os.environ.get("INDEX01_CONFIG") or HERE / "config.yaml")
    secrets_path = Path(secrets_path or os.environ.get("TASKBOX_SECRETS") or DEFAULT_SECRETS)
    data = {}
    if config_path.exists():
        data = (yaml.safe_load(config_path.read_text()) or {}).get("taskbox") or {}
    sec = read_env_file(secrets_path)
    agents: dict[str, Agent] = {}
    for name, spec in (data.get("agents") or {}).items():
        spec = spec or {}
        if not AGENT_RE.match(str(name)):
            raise ValueError(f"bad agent name {name!r}")
        token = sec.get(f"TASKBOX_TOKEN_{name.upper().replace('-', '_')}", "")
        if len(token) < 32:
            log.warning("agent has no token (>=32 chars) in secrets; it cannot connect",
                        extra={"json_fields": {"agent": name}})
            continue
        agents[name] = Agent(
            name=name,
            token=token,
            admin=bool(spec.get("admin", False)),
            external=bool(spec.get("external", True)) and not bool(spec.get("admin", False)),
            webhook_url=str(spec.get("webhook_url") or ""),
            webhook_key=sec.get(f"TASKBOX_WEBHOOK_KEY_{name.upper().replace('-', '_')}", ""),
        )
    db_path = Path(data.get("db_path") or DEFAULT_DB)
    if not db_path.is_absolute():
        db_path = HERE / db_path
    return ServerConfig(
        host=str(data.get("host", "127.0.0.1")),
        port=int(data.get("port", 8646)),
        db_path=db_path,
        agents=agents,
        allowed_origins=tuple(data.get("allowed_origins") or ()),
    )


# ---------------------------------------------------------------------------
# Doorbell
# ---------------------------------------------------------------------------

async def ring_doorbell(session, agent: Agent, task: dict[str, Any], timeout: float = 15.0) -> str:
    """POST the task to the agent's webhook. Returns a short outcome string; never raises."""
    if not agent.webhook_url or session is None:
        return "no-webhook"
    import aiohttp

    payload = {
        "event": "task_assigned",
        "task_id": task["id"],
        "title": task["title"],
        "body": task["body"],
        "instructions": (
            "A task in Parth's taskbox is assigned to you. Call claim_task with this task_id, "
            "do the work, then call update_task with status done (or failed) and the result."
        ),
    }
    headers = {"Content-Type": "application/json"}
    if agent.webhook_key:
        headers["Authorization"] = f"Bearer {agent.webhook_key}"
    try:
        async with session.post(
            agent.webhook_url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return f"http-{resp.status}"
    except Exception as exc:  # best-effort: the task is still in the inbox
        return f"error-{type(exc).__name__}"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

_STATUS_ENUM = list(STATUSES)
TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_task",
        "description": (
            "Add a task to Parth's shared inbox. Use for anything Parth asks to get done later or by "
            "another agent. Returns the task with its id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title, a few words"},
                "body": {"type": "string", "description": "Details, links, what done looks like"},
                "assignee": {"type": "string", "description": "Agent to hand it to, e.g. grokbot. Omit to leave it unassigned."},
                "sensitive": {"type": "boolean", "description": "True for private matters; these never go to external agents"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks, newest activity first. External agents only see tasks assigned to them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": [*_STATUS_ENUM, "active"], "description": "active = open or claimed"},
                "assignee": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "get_task",
        "description": "One task with its full history of claims, notes and results.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "claim_task",
        "description": "Start working on an open task assigned to you (or unassigned). Only one agent can hold it.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "update_task",
        "description": (
            "Record progress: add a note, set the result, and/or change status "
            "(done, failed, cancelled; admins can also reopen with open)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": _STATUS_ENUM},
                "result": {"type": "string", "description": "The answer or outcome, in full"},
                "note": {"type": "string", "description": "A progress note for the history"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "assign_task",
        "description": (
            "Hand a task to an agent (admins only). If that agent has a webhook, it is woken up right away. "
            "Sensitive tasks cannot go to external agents."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "assignee": {"type": "string", "description": "Agent name, or empty to unassign"},
            },
            "required": ["task_id", "assignee"],
        },
    },
    {
        "name": "list_agents",
        "description": "Agents that can take tasks, and whether each is woken by webhook.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

INSTRUCTIONS = (
    "Parth's shared task inbox. Tasks come from his ring, WhatsApp and other agents. "
    "Claim before working, record the result with update_task, and never put passwords, "
    "card numbers or similar secrets in a task."
)


class Tools:
    def __init__(self, store: Store, cfg: ServerConfig, session_getter=lambda: None) -> None:
        self.store = store
        self.cfg = cfg
        self.session_getter = session_getter

    def _visible(self, agent: Agent, task: dict[str, Any]) -> bool:
        if agent.admin:
            return True
        return task["assignee"] == agent.name and not task["sensitive"]

    def _check_assignee(self, task: dict[str, Any] | None, assignee: str, sensitive: bool) -> None:
        target = self.cfg.agents.get(assignee)
        if target is None:
            raise TaskError(f"unknown agent {assignee!r}; see list_agents")
        if target.external and sensitive:
            raise TaskError("sensitive tasks cannot be assigned to an external agent")

    async def _maybe_ring(self, actor: str, task: dict[str, Any]) -> Optional[str]:
        target = self.cfg.agents.get(task.get("assignee") or "")
        if target is None or not target.webhook_url:
            return None
        outcome = await ring_doorbell(self.session_getter(), target, task)
        self.store._event(task["id"], actor, "doorbell", f"{target.name}: {outcome}")
        log.info("doorbell", extra={"json_fields": {"task": task["id"], "agent": target.name, "outcome": outcome}})
        return outcome

    async def call(self, agent: Agent, name: str, args: dict[str, Any]) -> Any:
        if not isinstance(args, dict):
            raise TaskError("arguments must be an object")
        if name == "create_task":
            assignee = (args.get("assignee") or "").strip() or None
            sensitive = bool(args.get("sensitive", False))
            if not agent.admin:
                assignee = agent.name  # external agents can only leave work for themselves
                sensitive = False
            elif assignee:
                self._check_assignee(None, assignee, sensitive)
            task = self.store.create(
                actor=agent.name, title=args.get("title", ""), body=args.get("body", "") or "",
                assignee=assignee, sensitive=sensitive,
            )
            if assignee and assignee != agent.name:
                task["doorbell"] = await self._maybe_ring(agent.name, task)
            return task
        if name == "list_tasks":
            assignee = args.get("assignee")
            if not agent.admin:
                assignee = agent.name
            return {"tasks": self.store.list(
                status=args.get("status"), assignee=assignee,
                include_sensitive=agent.admin, limit=args.get("limit", 20),
            )}
        if name == "get_task":
            task = self.store.get(args.get("task_id", ""))
            if not self._visible(agent, task):
                raise TaskError(f"no task {args.get('task_id')}")
            task["history"] = self.store.history(task["id"])
            return task
        if name == "claim_task":
            task = self.store.get(args.get("task_id", ""))
            if not agent.admin and (task["sensitive"] or task["assignee"] not in (None, agent.name)):
                raise TaskError(f"no task {task['id']}")
            if not agent.admin and task["assignee"] is None:
                raise TaskError("external agents can only claim tasks assigned to them")
            return self.store.claim(task["id"], actor=agent.name)
        if name == "update_task":
            task = self.store.get(args.get("task_id", ""))
            if not self._visible(agent, task):
                raise TaskError(f"no task {args.get('task_id')}")
            return self.store.update(
                task["id"], actor=agent.name, is_admin=agent.admin,
                status=args.get("status"), result=args.get("result"), note=args.get("note"),
            )
        if name == "assign_task":
            if not agent.admin:
                raise TaskError("only an admin agent can assign tasks")
            task = self.store.get(args.get("task_id", ""))
            assignee = (args.get("assignee") or "").strip() or None
            if assignee:
                self._check_assignee(task, assignee, task["sensitive"])
            task = self.store.assign(task["id"], assignee, actor=agent.name)
            if assignee and assignee != agent.name:
                task["doorbell"] = await self._maybe_ring(agent.name, task)
            return task
        if name == "list_agents":
            return {"agents": [
                {"name": a.name, "admin": a.admin, "external": a.external, "woken_by_webhook": bool(a.webhook_url)}
                for a in self.cfg.agents.values()
            ]}
        raise TaskError(f"unknown tool {name}")


# ---------------------------------------------------------------------------
# MCP over HTTP (JSON-RPC 2.0, JSON responses, stateless)
# ---------------------------------------------------------------------------

DEFAULT_PROTOCOL = "2025-06-18"
_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def handle_rpc(tools: Tools, agent: Agent, msg: Any) -> Optional[dict[str, Any]]:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, req_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:  # notification (initialized, cancelled, ...)
        return None
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if isinstance(asked, str) and _VERSION_RE.match(asked) else DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "taskbox", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _rpc_error(req_id, -32602, "tools/call needs a name")
        name = params["name"]
        if name not in {t["name"] for t in TOOLS}:
            return _rpc_error(req_id, -32602, f"unknown tool {name}")
        try:
            out = await tools.call(agent, name, params.get("arguments") or {})
            text, is_error = json.dumps(out, ensure_ascii=False, indent=1), False
        except TaskError as exc:
            text, is_error = str(exc), True
        except Exception:
            log.exception("tool failed", extra={"json_fields": {"tool": name, "agent": agent.name}})
            text, is_error = "internal error", True
        log.info("tool call", extra={"json_fields": {"tool": name, "agent": agent.name, "error": is_error}})
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error,
        }}
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def authenticate(cfg: ServerConfig, header: str) -> Optional[Agent]:
    if not header.startswith("Bearer "):
        return None
    given = header[len("Bearer "):].strip()
    found = None
    for agent in cfg.agents.values():  # compare against every token: constant work per request
        if hmac.compare_digest(agent.token, given):
            found = agent
    return found


def build_app(cfg: ServerConfig, store: Optional[Store] = None):
    import aiohttp
    from aiohttp import web

    store = store or Store(cfg.db_path)
    state: dict[str, Any] = {}
    tools = Tools(store, cfg, session_getter=lambda: state.get("session"))
    app = web.Application(client_max_size=MAX_REQUEST)

    async def _startup(_app):
        state["session"] = aiohttp.ClientSession()

    async def _cleanup(_app):
        if state.get("session"):
            await state["session"].close()
        store.close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    async def mcp_post(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin and origin not in cfg.allowed_origins:
            return web.Response(status=403, text="origin not allowed")
        agent = authenticate(cfg, request.headers.get("Authorization", ""))
        if agent is None:
            log.warning("unauthorized", extra={"json_fields": {"remote": request.remote}})
            return web.Response(status=401, text="unauthorized", headers={"WWW-Authenticate": 'Bearer realm="taskbox"'})
        try:
            msg = json.loads(await request.read())
        except (ValueError, UnicodeDecodeError):
            return web.json_response(_rpc_error(None, -32700, "parse error"), status=400)
        if isinstance(msg, list):
            replies = [r for r in [await handle_rpc(tools, agent, m) for m in msg] if r is not None]
            return web.json_response(replies) if replies else web.Response(status=202)
        reply = await handle_rpc(tools, agent, msg)
        if reply is None:
            return web.Response(status=202)
        return web.json_response(reply)

    async def mcp_other(request: web.Request) -> web.Response:
        # Stateless JSON-only server: no SSE stream to open, no session to delete.
        # Auth first, so an anonymous probe learns nothing beyond "needs a token".
        if authenticate(cfg, request.headers.get("Authorization", "")) is None:
            return web.Response(status=401, text="unauthorized", headers={"WWW-Authenticate": 'Bearer realm="taskbox"'})
        return web.Response(status=405, headers={"Allow": "POST"})

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "agents": sorted(cfg.agents)})

    app.router.add_post("/mcp", mcp_post)
    app.router.add_route("GET", "/mcp", mcp_other)
    app.router.add_route("DELETE", "/mcp", mcp_other)
    app.router.add_get("/healthz", healthz)
    return app


def main() -> None:
    from adapter import setup_logging  # same JSON log format as the adapter
    from aiohttp import web

    parser = argparse.ArgumentParser(description="taskbox: shared task inbox (MCP)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    setup_logging(args.log_level)
    cfg = load_server_config()
    if not cfg.agents:
        raise SystemExit("taskbox: no agents with tokens configured (taskbox.secrets.env)")
    log.info("starting taskbox", extra={"json_fields": {
        "host": cfg.host, "port": cfg.port, "db": str(cfg.db_path), "agents": sorted(cfg.agents),
    }})
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port, access_log=None)


if __name__ == "__main__":
    main()
