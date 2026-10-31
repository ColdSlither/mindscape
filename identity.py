"""Mindscape identity: a self-certifying node key.

Syncthing's model, copied deliberately because it is the right shape for a
network with no administrator:

* a keypair is generated on first use, locally, by the node itself;
* the **Mindscape ID** is derived from the public key (SHA-256, base32, grouped),
  so it certifies itself and needs no CA, account or vendor;
* admission is by explicit exchange of IDs, out of band, by both sides;
* requests are **signed**, so a peer verifies a key rather than trusting a shared
  bearer secret. A bearer key is a symmetric shared secret: no forward secrecy,
  no per-direction rotation, and a leak is total.

Threat note, stated plainly: the private key sits in the plugin data directory
with mode 0600. Anyone who can read it can impersonate this node. It belongs on
an encrypted disk, same as Syncthing's own keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

PLUGIN = "mindscape"
KEY_FILENAME = "identity.json"
SCHEME = "ed25519"

# Identity format, deliberately matching Syncthing's Device ID shape: an ID that
# humans copy out of band needs typo detection, so Luhn check characters are
# appended. SHA-256 gives 52 base32 chars; four check chars make 56, rendered as
# eight groups of seven.
B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
ID_BODY_LEN = 52
ID_CHECK_CHARS = 4
ID_GROUPS = 8
ID_GROUP_LEN = 7
ID_TOTAL_LEN = ID_GROUPS * ID_GROUP_LEN
assert ID_BODY_LEN + ID_CHECK_CHARS == ID_TOTAL_LEN


def _luhn_mod_n(payload: str, alphabet_len: int = 32) -> str:
    """Luhn check character over the base32 alphabet (the Syncthing construction)."""
    factor, total = 1, 0
    for char in payload:
        codepoint = B32_ALPHABET.index(char)
        addend = factor * codepoint
        factor = 1 if factor == 2 else 2
        addend = (addend // alphabet_len) + (addend % alphabet_len)
        total += addend
    return B32_ALPHABET[(alphabet_len - (total % alphabet_len)) % alphabet_len]


def _with_check_chars(body: str) -> str:
    for _ in range(ID_CHECK_CHARS):
        body += _luhn_mod_n(body)
    return body


# ------------------------------------------------------------------ storage

def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    path = Path(plugin_data_dir(PLUGIN))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key_path() -> Path:
    return _data_dir() / KEY_FILENAME


# ---------------------------------------------------------------- derivation

def derive_id(public_key_raw: bytes) -> str:
    """Mindscape ID: SHA-256 of the raw public key, base32, with Luhn check chars."""
    digest = hashlib.sha256(public_key_raw).digest()
    body = base64.b32encode(digest).decode().rstrip("=")[:ID_BODY_LEN]
    full = _with_check_chars(body)
    return "-".join(full[i:i + ID_GROUP_LEN]
                    for i in range(0, ID_TOTAL_LEN, ID_GROUP_LEN))


def id_is_well_formed(value: str) -> bool:
    """True when an ID's check characters are consistent, i.e. probably not a typo.

    The point of check characters: an ID is exchanged between people out of band,
    by copy-paste or by reading it aloud. This catches the transcription error
    before anyone tries to connect.
    """
    cleaned = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    if len(cleaned) != ID_TOTAL_LEN:
        return False
    if any(ch not in B32_ALPHABET for ch in cleaned):
        return False
    return _with_check_chars(cleaned[:ID_BODY_LEN]) == cleaned


def normalize_id(value: str) -> str:
    """Accept an ID with or without grouping, upper or lower case."""
    cleaned = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    return "-".join(cleaned[i:i + ID_GROUP_LEN]
                    for i in range(0, len(cleaned), ID_GROUP_LEN))


# ----------------------------------------------------------------- identity

def load_or_create() -> dict:
    """Return this node's identity, generating a keypair on first use."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = _key_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("id") and data.get("public_key"):
                return data
        except Exception:
            # Corrupt key file: do not silently overwrite an identity.
            raise RuntimeError(
                f"Mindscape identity file is unreadable: {path}. "
                f"Refusing to generate a new key and orphan existing peers. "
                f"Move it aside to re-key deliberately."
            )

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    public_raw = private.public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)

    data = {
        "scheme": SCHEME,
        "id": derive_id(public_raw),
        "public_key": base64.b64encode(public_raw).decode(),
        "private_key": base64.b64encode(private_raw).decode(),
        "created_at": time.time(),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return data


# ------------------------------------------------------------------ signing

def sign(message: bytes, identity: dict | None = None) -> str:
    """Sign bytes with this node's key. Returns base64 signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    identity = identity or load_or_create()
    private = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(identity["private_key"]))
    return base64.b64encode(private.sign(message)).decode()


def verify(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """Verify a signature against a peer's advertised public key. Never raises."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public.verify(base64.b64decode(signature_b64), message)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def id_for_public_key(public_key_b64: str) -> str:
    return derive_id(base64.b64decode(public_key_b64))


# -------------------------------------------------------------------- tool

def mindscape_identity(args, **kwargs) -> str:
    """Report this node's Mindscape identity. Never exposes the private key."""
    try:
        identity = load_or_create()
    except RuntimeError as exc:
        return json.dumps({"success": False, "error": str(exc)}, indent=2)

    path = _key_path()
    mode = oct(path.stat().st_mode & 0o777) if path.is_file() else "?"
    return json.dumps({
        "success": True,
        "mindscape_id": identity["id"],
        "scheme": identity.get("scheme", SCHEME),
        "public_key": identity["public_key"],
        "created_at": identity.get("created_at"),
        "key_file": str(path),
        "key_file_mode": mode,
        "note": ("Share the Mindscape ID freely; it is a public fingerprint, like a "
                 "Syncthing Device ID. Admission is mutual and explicit: a peer must "
                 "add this ID before anything is accepted. The private key stays on "
                 "this machine and is never sent. Anyone who can read the key file "
                 "can impersonate this node, so it belongs on an encrypted disk."),
    }, indent=2)
