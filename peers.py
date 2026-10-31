"""Mindscape peers: the buddy list, and the tiers that bound it.

Invite-only by decision (Rell, 2026-10-30). There is no directory and no
discovery. A peer exists here only because a human passed its Mindscape ID and
public key across out of band and called `mindscape_admit`.

Two rules this file exists to enforce:

1. **Tiers are local and non-negotiable.** A peer's tier is set on this side and
   can never be changed by anything the peer sends. Nothing in an inbound request
   influences it.
2. **Nothing here can raise a tier.** `mindscape_admit` grants `chat` and refuses
   anything higher; there is deliberately no tool that grants `artifacts` or
   `work`. Raising a tier is an operator edit to the peers file, so a
   prompt-injected agent cannot promote a peer, and a peer cannot promote itself.

Tier 2 also stays gated at *use* time, not just at admission time. A wrongly
granted tier should still fail closed when it is exercised. Defence in depth,
because the first line is a file an agent can technically write.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .identity import id_is_well_formed, normalize_id

TIERS = ("chat", "artifacts", "work")
TIER_RANK = {name: rank for rank, name in enumerate(TIERS)}
DEFAULT_TIER = "chat"
PEERS_FILENAME = "mindscape-peers.yaml"

# Only this tier may be granted by a tool call. Everything above is an operator edit.
TOOL_GRANTABLE = "chat"


# ------------------------------------------------------------------- storage

def _settings() -> dict:
    try:
        from hermes_cli.config import load_config

        entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
        settings = (entries.get("mindscape") or {}).get("settings")
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _peers_path() -> Path:
    from hermes_constants import get_hermes_home

    configured = str(_settings().get("peers_file") or PEERS_FILENAME)
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = get_hermes_home() / path
    return path


def _load_file() -> dict:
    path = _peers_path()
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:
        try:
            data = json.loads(text)
        except Exception:
            return {}
    peers = data.get("peers") if isinstance(data, dict) else None
    return peers if isinstance(peers, dict) else {}


def _save_file(peers: dict) -> None:
    path = _peers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"peers": peers}
    try:
        import yaml

        text = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
    except Exception:
        text = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ lookups

def load_peers() -> dict:
    """Admitted peers, keyed by normalized Mindscape ID."""
    return {normalize_id(k): v for k, v in _load_file().items()
            if isinstance(v, dict)}


def tier_of(peer_id: str) -> str | None:
    entry = load_peers().get(normalize_id(peer_id))
    return (entry or {}).get("tier")


def access_allows(peer_id: str, tier: str) -> bool:
    """Does this peer's granted tier reach `tier`? Unknown peers get nothing."""
    if tier not in TIER_RANK:
        return False
    granted = tier_of(peer_id)
    if granted not in TIER_RANK:
        return False
    return TIER_RANK[granted] >= TIER_RANK[tier]


# --------------------------------------------------------------------- tools

def mindscape_peers(args, **kwargs) -> str:
    peers = load_peers()
    out = []
    for peer_id, entry in sorted(peers.items()):
        out.append({
            "mindscape_id": peer_id,
            "label": entry.get("label") or "",
            "tier": entry.get("tier") or DEFAULT_TIER,
            "address": entry.get("address") or "",
            "admitted_at": entry.get("admitted_at"),
            "added_by": entry.get("added_by") or "operator",
            "id_well_formed": id_is_well_formed(peer_id),
            "has_public_key": bool(entry.get("public_key")),
        })
    counts = {tier: sum(1 for p in out if p["tier"] == tier) for tier in TIERS}
    return json.dumps({
        "success": True,
        "peers": out,
        "count": len(out),
        "by_tier": counts,
        "peers_file": str(_peers_path()),
        "invite_only": True,
        "note": ("Invite-only: no directory, no discovery. A peer is admitted only by "
                 "an explicit local action. Chat is granted by tool; artifacts and work "
                 "are operator edits to the peers file, deliberately not tool-grantable."),
    }, indent=2)


def mindscape_admit(args, **kwargs) -> str:
    raw_id = str(args.get("mindscape_id") or "").strip()
    if not raw_id:
        return json.dumps({"success": False, "error": "'mindscape_id' is required."}, indent=2)
    # Fail loudly rather than silently ignoring a security-relevant argument. If a
    # caller asks for a tier and gets "success", it will believe the tier was set.
    if args.get("tier") is not None:
        return json.dumps({
            "success": False,
            "error": ("Admission cannot set a tier. Chat is granted by tool; 'artifacts' "
                      "and 'work' are operator edits to the peers file. Silently ignoring "
                      "your tier argument would be worse than refusing it, so this is a "
                      "refusal."),
            "requested_tier": args.get("tier"),
            "grantable_by_tool": TOOL_GRANTABLE,
        }, indent=2)

    peer_id = normalize_id(raw_id)

    if not id_is_well_formed(peer_id):
        return json.dumps({
            "success": False,
            "error": ("That Mindscape ID failed its check characters, so it was almost "
                      "certainly mistyped. Ask the peer to re-send it exactly as shown by "
                      "mindscape_identity."),
            "received": peer_id,
        }, indent=2)

    public_key = str(args.get("public_key") or "").strip()
    address = str(args.get("address") or "").strip()
    label = str(args.get("label") or "").strip()[:120]
    transport = str(args.get("transport") or "").strip()

    peers = load_peers()
    existing = peers.get(peer_id) or {}
    if existing and (existing.get("tier") or DEFAULT_TIER) != DEFAULT_TIER:
        # Never silently downgrade a peer an operator deliberately promoted.
        return json.dumps({
            "success": False,
            "error": (f"Peer already admitted at tier "
                      f"'{existing.get('tier')}'. Re-admitting would downgrade it. "
                      f"Revoke first if that is intended."),
            "mindscape_id": peer_id,
        }, indent=2)

    peers[peer_id] = {
        "label": label or existing.get("label") or "",
        "public_key": public_key or existing.get("public_key") or "",
        "address": address or existing.get("address") or "",
        "tier": existing.get("tier") or TOOL_GRANTABLE,
        "transport": transport or existing.get("transport") or "",
        "admitted_at": existing.get("admitted_at") or time.time(),
        "added_by": "mindscape_admit",
    }
    _save_file(peers)

    return json.dumps({
        "success": True,
        "mindscape_id": peer_id,
        "tier": peers[peer_id]["tier"],
        "label": peers[peer_id]["label"],
        "note": (f"Admitted at '{peers[peer_id]['tier']}' only. Raising to 'artifacts' or "
                 f"'work' is a deliberate operator edit to {_peers_path()}, not a tool call, "
                 f"so neither this agent nor the peer can escalate. Admission is one-sided "
                 f"until the peer admits this node in return."),
    }, indent=2)


def mindscape_revoke(args, **kwargs) -> str:
    raw_id = str(args.get("mindscape_id") or "").strip()
    if not raw_id:
        return json.dumps({"success": False, "error": "'mindscape_id' is required."}, indent=2)
    peer_id = normalize_id(raw_id)
    peers = load_peers()
    if peer_id not in peers:
        return json.dumps({"success": False, "error": "That peer is not admitted.",
                           "mindscape_id": peer_id}, indent=2)
    removed = peers.pop(peer_id)
    _save_file(peers)
    return json.dumps({
        "success": True,
        "revoked": peer_id,
        "previous_tier": removed.get("tier"),
        "note": ("Removed locally. Nothing is announced: the peer simply stops being "
                 "reachable from this side."),
    }, indent=2)
