"""Lane-aware peer transport.

Why this exists instead of shelling out to ``hermes peer run``: the public CLI
hardcodes the Bot Chat title (``BOT_CHAT_TITLE``), and lanes are the whole point.
This module speaks the same documented HTTP surface the CLI speaks, so nothing new
is exposed on the wire.

Everything here is **client-side**. A peer needs no plugin for any of it, only a
stock Hermes gateway with ``api_server`` enabled: a lane is just a session on that
gateway, ensured by title. That is why the estate nodes need nothing installed
while a federated friend would need Mindscape on both ends (the tier ladder is
receiving-side enforcement, which is a plugin job, not a gateway one).

Credentials never cross origins: requests go through the same
``open_credentialed_url`` helper the CLI uses, which strips the bearer header on a
cross-origin redirect. The key is never logged or returned.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from hermes_cli.urllib_security import open_credentialed_url

DEFAULT_WORK_LANE = "Mindscape"
DEFAULT_CHAT_LANE = "Mindscape Chat"
DEFAULT_SOURCE = "mindscape"
_TIMEOUT_S = 20
_SEND_TIMEOUT_S = 60   # a chat turn can be long; a timeout here is "unanswered", not "failed"
_SESSION_PAGE = 200


class TransportError(Exception):
    """Raised for anything that prevents a well-formed request."""


class RunNotFound(TransportError):
    """The peer has no such run.

    Deliberately distinct from a transport failure. A 404 is a *fact* about the run
    (the peer has forgotten it), not a problem reaching the peer, and conflating the
    two leaves the row stamped as "poll attempted" forever while nothing changes.
    """


class SendTimeout(TransportError):
    """A chat POST did not answer in time.

    NOT the same as a failure. The request reached the peer, the peer accepted the
    message and started working on it, and only the *reply* failed to arrive inside our
    window. Measured: a 3,312-char update reported as a hard failure was present in the
    peer's lane 3 seconds after the request, with the peer's agent already acting on it.
    Reporting this as a failure is what makes a caller resend, and a resend duplicates a
    message the peer already has.
    """


# ---------------------------------------------------------------- resolution

def _settings() -> dict:
    try:
        from hermes_cli.config import load_config

        entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
        settings = (entries.get("mindscape") or {}).get("settings")
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def lane_title(kind: str) -> str:
    """Lane title for 'work' or 'chat'. Operator-overridable, stable by default."""
    lanes = _settings().get("lanes")
    lanes = lanes if isinstance(lanes, dict) else {}
    default = DEFAULT_CHAT_LANE if kind == "chat" else DEFAULT_WORK_LANE
    return str(lanes.get(kind) or default).strip() or default


def resolve_node(node: str) -> tuple[str, str]:
    """(base_url, key) for a registered peer. Raises rather than guessing."""
    name = (node or "").strip().lower()
    if not name:
        raise TransportError("A node is required.")
    try:
        from hermes_cli.config import load_config

        peers = (load_config() or {}).get("bot_peers") or {}
    except Exception as exc:
        raise TransportError(f"Could not read peer config: {type(exc).__name__}") from exc
    entry = peers.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(peers)) or "(none)"
        raise TransportError(f"Unknown node {name!r}. Registered nodes: {known}")

    base = str(entry.get("url") or "").rstrip("/")
    if not base:
        raise TransportError(f"Node {name!r} has no url registered.")

    env_name = f"HERMES_PEER_{name.upper().replace('-', '_')}_KEY"
    key = ""
    try:
        from agent.secret_scope import get_secret

        key = (get_secret(env_name, "") or "").strip()
    except Exception:
        pass
    if not key:
        import os

        key = (os.environ.get(env_name) or "").strip()
    if not key:
        raise TransportError(f"Node {name!r} has no key set ({env_name}).")
    return base, key


# ------------------------------------------------------------------ requests

def _request(base: str, key: str, method: str, path: str, body=None,
             headers: dict | None = None, timeout: float = _TIMEOUT_S):
    """Return (status, parsed_json). Never raises for HTTP/transport failures."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    merged = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        merged.update(headers)
    request = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                     headers=merged)
    try:
        with open_credentialed_url(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return exc.code, {"error": f"HTTP {exc.code}", "detail": detail}
    except urllib.error.URLError as exc:
        # A connect timeout arrives as URLError wrapping a socket timeout, NOT as a bare
        # TimeoutError. Missing this shape meant the most common timeout took the generic
        # branch and got reported as a hard failure.
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
            return 0, {"timeout": True, "error": f"no response within {timeout:g}s"}
        return 0, {"error": f"URLError: {str(exc)[:160]}"}
    except (TimeoutError, socket.timeout):
        # Distinguish "no answer yet" from "could not send". A POST may well have been
        # accepted and answered elsewhere; the caller decides what that means.
        return 0, {"timeout": True, "error": f"no response within {timeout:g}s"}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


# --------------------------------------------------------------------- lanes

def list_sessions(node: str, limit: int = _SESSION_PAGE) -> list:
    base, key = resolve_node(node)
    status, payload = _request(
        base, key, "GET", f"/api/sessions?limit={int(limit)}&include_hidden=1")
    if status != 200:
        raise TransportError(
            f"Listing sessions on {node!r} failed: "
            f"{payload.get('detail') or payload.get('error')}")
    rows = payload.get("data") if isinstance(payload, dict) else payload
    return rows if isinstance(rows, list) else []


def ensure_lane(node: str, title: str) -> dict:
    """Find or create the session titled `title` on `node`.

    Idempotent by title, verified against a live peer: the title filter works and
    the create is kept safe by the peer's UNIQUE(title) guard. Client-side matching
    is done anyway, so a peer that ignores the server-side filter still behaves.
    """
    base, key = resolve_node(node)
    quoted = urllib.parse.urlencode({"limit": _SESSION_PAGE, "title": title,
                                     "include_hidden": 1})
    status, payload = _request(base, key, "GET", f"/api/sessions?{quoted}")
    if status == 200:
        rows = payload.get("data") if isinstance(payload, dict) else payload
        for session in (rows or []):
            if isinstance(session, dict) and (session.get("title") or "").strip() == title:
                sid = session.get("id") or session.get("session_id")
                if sid:
                    return {"session_id": sid, "existed": True, "title": title}

    status, payload = _request(base, key, "POST", "/api/sessions",
                               {"title": title, "source": DEFAULT_SOURCE})
    if status not in (200, 201):
        detail = payload.get("detail") or payload.get("error")
        # A racing create loses to the UNIQUE(title) guard: re-read rather than fail.
        status2, payload2 = _request(base, key, "GET", f"/api/sessions?{quoted}")
        rows = (payload2.get("data") if isinstance(payload2, dict) else None) or []
        for session in rows:
            if isinstance(session, dict) and (session.get("title") or "").strip() == title:
                sid = session.get("id") or session.get("session_id")
                if sid:
                    return {"session_id": sid, "existed": True, "title": title}
        raise TransportError(f"Could not ensure lane {title!r} on {node!r}: {detail}")

    session = (payload or {}).get("session") or payload or {}
    sid = session.get("id") or session.get("session_id")
    if not sid:
        raise TransportError(f"Peer {node!r} created a lane but returned no id.")
    return {"session_id": sid, "existed": False, "title": title}


# ---------------------------------------------------------------------- runs

def start_run(node: str, session_id: str, message: str, idempotency_key: str) -> dict:
    base, key = resolve_node(node)
    status, payload = _request(
        base, key, "POST", "/v1/runs",
        {"input": message, "session_id": session_id},
        headers={"Idempotency-Key": idempotency_key})
    if status not in (200, 201, 202):
        raise TransportError(
            f"Starting a run on {node!r} failed: "
            f"{payload.get('detail') or payload.get('error')}")
    return payload if isinstance(payload, dict) else {}


def get_run(node: str, run_id: str) -> dict:
    base, key = resolve_node(node)
    status, payload = _request(
        base, key, "GET", f"/v1/runs/{urllib.parse.quote(run_id, safe='')}")
    if status == 404:
        raise RunNotFound(f"Run {run_id} is not known to {node!r} (HTTP 404).")
    if status != 200:
        raise TransportError(
            f"Reading run {run_id} on {node!r} failed: "
            f"{payload.get('detail') or payload.get('error')}")
    return payload if isinstance(payload, dict) else {}


def stop_run(node: str, run_id: str) -> dict:
    base, key = resolve_node(node)
    status, payload = _request(
        base, key, "POST", f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop")
    if status not in (200, 202):
        raise TransportError(
            f"Stopping run {run_id} on {node!r} failed: "
            f"{payload.get('detail') or payload.get('error')}")
    return payload if isinstance(payload, dict) else {}


def send_message(node: str, session_id: str, message: str) -> dict:
    """Post a turn into an existing lane, DM-style (answered inside that session).

    A chat turn can take arbitrarily long, so the window here is generous and a timeout
    is reported as `SendTimeout`: accepted-but-unanswered, never "failed".
    """
    base, key = resolve_node(node)
    path = f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat"
    status, payload = _request(base, key, "POST", path,
                               {"input": message, "session_id": session_id},
                               timeout=_SEND_TIMEOUT_S)
    if status == 0 and payload.get("timeout"):
        raise SendTimeout(
            f"Lane {session_id} on {node!r} had not answered after {_SEND_TIMEOUT_S:g}s. "
            f"The message was almost certainly accepted and is being worked on. "
            f"Do NOT resend; read the lane instead.")
    if status not in (200, 201, 202):
        raise TransportError(
            f"Sending into lane {session_id} on {node!r} failed: "
            f"{payload.get('detail') or payload.get('error')}")
    return payload if isinstance(payload, dict) else {}


# ------------------------------------------------------------------ features

def capabilities(node: str) -> dict:
    base, key = resolve_node(node)
    status, payload = _request(base, key, "GET", "/v1/capabilities")
    if status != 200:
        raise TransportError(f"Capability probe of {node!r} failed: "
                             f"{payload.get('error')}")
    return payload if isinstance(payload, dict) else {}
