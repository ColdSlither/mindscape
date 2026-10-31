"""Mindscape backend API — mounted at /api/plugins/mindscape/.

Serves the desktop half: node roster, health, activity ledger, chat, and work dispatch.

Every handler is a thin wrapper around the plugin's own tool handlers in
``mindscape.tools`` / ``mindscape.chat`` — the same code paths the agent calls, so the
desktop surface and the agent surface cannot drift. Nothing is reimplemented here.

Two deliberate design choices:

* **The roster is cheap by default.** A live probe dials every node, and a dead node
  costs a full connect timeout (oya alone measured 20 s). A UI polling that is unusable,
  so ``GET /roster`` serves the cached last-known health and reports its age; refreshing
  is an explicit ``POST /probe``. Expensive work happens when a human asks for it, never
  on a timer.
* **Blocking probes run off the event loop.** ``_probe`` and the message send are
  synchronous urllib calls, so they are wrapped in ``asyncio.to_thread``: a timing-out
  node must never wedge the gateway's loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

# The plugin is a package under the plugins root; make that root importable so this
# module reaches its own siblings whatever the gateway's sys.path happens to be.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PLUGINS_DIR = _PLUGIN_ROOT.parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

try:
    from mindscape import tools as _tools_mod
    from mindscape import chat as _chat_mod
except Exception:  # pragma: no cover - reported, not raised, so the app still mounts
    logger.exception("Mindscape: backend could not import its own plugin modules")
    _tools_mod = None
    _chat_mod = None

router = APIRouter()

# node -> last known probe result. Process-local and deliberately not persisted: health
# is a fact about *now*, and a row in a database would outlive its own truth.
_HEALTH: dict[str, dict[str, Any]] = {}


def _ready() -> tuple[Any, Any]:
    """Return (tools, chat), or refuse.

    Every handler takes its modules from here, which makes the import guard explicit to
    both the runtime and a type checker: neither should have to reason about a module
    that is None.
    """
    if _tools_mod is None or _chat_mod is None:
        raise HTTPException(status_code=503, detail="Mindscape plugin modules unavailable")
    return _tools_mod, _chat_mod


def _parsed(raw: str) -> dict:
    """Tool handlers return JSON strings; unwrap one for an HTTP caller."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail=f"Unparsable result: {str(raw)[:200]}")
    if isinstance(value, dict) and value.get("success") is False:
        raise HTTPException(status_code=400, detail=value.get("error") or "Request refused")
    return value if isinstance(value, dict) else {"data": value}


def _broadcast(event: str, payload: dict) -> None:
    """Fire-and-forget push to every connected desktop window. Never fatal.

    A plugin_api router runs inside the gateway, so this reaches Desktop windows
    attached to that gateway. With no window attached it is a logged no-op, which is
    the documented and correct outcome rather than a failure.
    """
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event("mindscape", event, payload)
    except Exception:
        logger.debug("Mindscape: broadcast %s skipped", event, exc_info=True)


def _count_active_runs(con, node: str) -> int:
    """Runs still in flight, with the vocabulary derived from the one _TERMINAL list.

    Never a second hardcoded copy: the copy that used to live in the roster omitted
    "completed", so every finished run read as active and the count lied.
    """
    tools, _ = _ready()
    terminal = tools._TERMINAL
    placeholders = ",".join("?" * len(terminal))
    row = con.execute(
        f"SELECT COUNT(*) FROM runs WHERE node=?"
        f" AND (status IS NULL OR lower(trim(status)) NOT IN ({placeholders}))",
        (node, *terminal),
    ).fetchone()
    return int(row[0]) if row else 0


def _build_roster() -> dict:
    """Assemble the roster from local reads only. Never dials."""
    tools, _ = _ready()
    peers = tools._peers()
    con = tools._db()
    nodes: list[dict] = []
    for name in sorted(peers):
        entry = peers[name] if isinstance(peers[name], dict) else {}
        view = tools._node_view(name, entry)
        cached = _HEALTH.get(name)
        view["health"] = cached or {"reachable": None, "probed": False}
        if cached:
            view["health_age_s"] = round(time.time() - float(cached.get("at") or 0), 1)
        view["active_runs"] = _count_active_runs(con, name)
        nodes.append(view)

    reachable = sum(1 for n in nodes if (n.get("health") or {}).get("reachable"))
    ever_probed = any((n.get("health") or {}).get("probed") for n in nodes)
    return {
        "success": True,
        "nodes": nodes,
        "reachable": reachable,
        "total": len(nodes),
        "probed": ever_probed,
        "stale": bool(nodes) and not ever_probed,
    }


def _probe_all() -> dict:
    """Dial every node once and refresh the cache. Blocking; call through to_thread."""
    tools, _ = _ready()
    peers = tools._peers()
    for name in sorted(peers):
        entry = peers[name] if isinstance(peers[name], dict) else {}
        try:
            result = dict(tools._probe(name, entry.get("url", "") or "") or {})
        except Exception as exc:  # a dead node is data, never an exception
            result = {"reachable": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        result["probed"] = True
        result["at"] = time.time()
        _HEALTH[name] = result
    return _HEALTH


@router.get("/identity")
async def identity() -> dict:
    """This node's own Mindscape identity. The public half only; the key never leaves."""
    from mindscape import identity as _identity

    ident = _identity.load_or_create()
    return {
        "id": ident.get("id"),
        "scheme": ident.get("scheme"),
        "public_key": ident.get("public_key"),
        "created_at": ident.get("created_at"),
    }


@router.get("/roster")
async def roster(probe: bool = Query(False, description="Dial every node for live health")) -> dict:
    """Nodes, role, cached health, and how much work each still has in flight.

    ``probe=true`` performs the live dial, which a dead node makes slow. Without it
    this is a local read and returns at once.
    """
    if probe:
        await asyncio.to_thread(_probe_all)
    return _build_roster()


@router.post("/probe")
async def probe(payload: dict | None = None) -> dict:
    """Refresh health now. Optional body {"node": "eshu"} dials just that one."""
    tools, _ = _ready()
    node = str((payload or {}).get("node") or "").strip()
    if node:
        peers = tools._peers()
        if node not in peers:
            raise HTTPException(status_code=404, detail=f"Unknown node {node!r}")
        entry = peers[node] if isinstance(peers[node], dict) else {}
        result = dict(await asyncio.to_thread(tools._probe, node, entry.get("url", "") or "") or {})
        result["probed"] = True
        result["at"] = time.time()
        _HEALTH[node] = result
    else:
        await asyncio.to_thread(_probe_all)
    _broadcast("health.updated", {"nodes": sorted(_HEALTH)})
    return _build_roster()


@router.get("/activity")
async def activity(limit: int = Query(40, ge=1, le=500)) -> dict:
    """Recent chat and dispatched runs, newest first, merged into a single feed."""
    tools, _ = _ready()
    con = tools._db()
    messages: list[dict] = []
    runs: list[dict] = []

    try:
        for row in con.execute(
            "SELECT direction, peer, kind, transport, body, created_at"
            " FROM messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ):
            messages.append({
                "type": "message",
                "direction": row[0],
                "node": row[1],
                "kind": row[2],
                "transport": row[3],
                "body": (row[4] or "")[:400],
                "at": row[5],
            })
    except Exception:
        logger.debug("Mindscape: messages read failed", exc_info=True)

    try:
        for row in con.execute(
            "SELECT run_id, node, agent, goal, status, created_at, updated_at"
            " FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ):
            runs.append({
                "type": "run",
                "run_id": row[0],
                "node": row[1],
                "agent": row[2],
                "goal": (row[3] or "")[:400],
                "status": row[4],
                "at": row[5],
                "updated_at": row[6],
            })
    except Exception:
        logger.debug("Mindscape: runs read failed", exc_info=True)

    feed = sorted(messages + runs, key=lambda x: float(x.get("at") or 0), reverse=True)
    return {
        "success": True,
        "items": feed[:limit],
        "messages": len(messages),
        "runs": len(runs),
    }


@router.post("/say")
async def say(payload: dict) -> dict:
    """Send a chat message into a peer's Mindscape Chat lane.

    Relays the tool handler's own verdict, including its "accepted but unconfirmed"
    answer: a timeout on a chat POST means the peer already has the message, so
    reporting that as failure is what makes a caller resend and duplicate it.
    """
    _, chat = _ready()
    target = str((payload or {}).get("target") or "").strip()
    message = str((payload or {}).get("message") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    result = _parsed(await asyncio.to_thread(chat.mindscape_say, {"target": target, "message": message}))
    _broadcast("message.sent", {
        "to": target, "chars": len(message), "delivered": result.get("delivered"),
    })
    return result


@router.post("/dispatch")
async def dispatch(payload: dict) -> dict:
    """Dispatch a goal into a peer's work lane. Returns the durable run id."""
    tools, _ = _ready()
    target = str((payload or {}).get("target") or "").strip()
    goal = str((payload or {}).get("goal") or (payload or {}).get("message") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")

    result = _parsed(await asyncio.to_thread(tools.mindscape_assign, {"target": target, "goal": goal}))
    _broadcast("run.dispatched", {"to": target, "run_id": result.get("run_id")})
    return result
