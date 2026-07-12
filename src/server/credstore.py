"""Server-side credential vault. Security invariants (non-negotiable, see UI brief):

- Keys live ONLY here, encrypted at rest (Fernet). Never in localStorage, run records,
  checkpoints, events, logs, or API responses.
- API responses carry only a fingerprint: `…` + last 4 chars.
- The encryption secret comes from the CRED_SECRET env var; without one, a key file is
  generated at data/.cred_key (gitignored, chmod-equivalent private). On serverless hosts
  set CRED_SECRET explicitly — the filesystem is ephemeral.

Stores two kinds of secrets: LLM provider connections and research-source API keys.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from src.tools.models import REPO_ROOT

DATA_DIR = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
_STORE = DATA_DIR / "credentials.json"
_KEYFILE = DATA_DIR / ".cred_key"
_lock = threading.Lock()

KEY_PATTERN = re.compile(r"^(sk-|sk_|key-|api[-_]?key|xoxb-|ghp_|AKIA)", re.IGNORECASE)


def looks_like_api_key(value: str) -> bool:
    """Heuristic used to reject keys pasted into non-credential fields (e.g. model id)."""
    v = (value or "").strip()
    return bool(KEY_PATTERN.match(v)) or (len(v) > 60 and " " not in v)


def _fernet() -> Fernet:
    secret = os.environ.get("CRED_SECRET")
    if secret:
        digest = hashlib.sha256(secret.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _KEYFILE.exists():
        _KEYFILE.write_bytes(Fernet.generate_key())
    return Fernet(_KEYFILE.read_bytes())


def _load() -> Dict:
    if not _STORE.exists():
        # serverless boot: /tmp starts empty — rehydrate the vault from the O2S_VAULT
        # env var (the whole encrypted vault file, pushed there by sync_vault_durable).
        # Contents stay Fernet-encrypted; CRED_SECRET makes them decryptable anywhere.
        blob = os.environ.get("O2S_VAULT", "")
        if blob:
            try:
                data = json.loads(blob)
                _save(data)
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"providers": {}, "source_secrets": {}}
    return json.loads(_STORE.read_text(encoding="utf-8"))


def sync_vault_durable() -> Dict:
    """Push the encrypted vault file into the Vercel project env (O2S_VAULT) so
    UI-saved keys survive instance recycling and redeploys. Requires VERCEL_TOKEN
    (create at vercel.com/account/tokens) and VERCEL_PROJECT_ID in the env.
    Running instances keep their copy; every future boot rehydrates from the env."""
    token = os.environ.get("VERCEL_TOKEN", "")
    project = os.environ.get("VERCEL_PROJECT_ID", "")
    if not _STORE.exists():
        return {"durable": False, "reason": "vault is empty"}
    if not os.environ.get("VERCEL"):
        # always-on host (Railway volume, local disk): the vault file itself persists
        return {"durable": True,
                "reason": "stored on the server's persistent disk (survives restarts and redeploys)"}
    if not token or not project:
        missing = [n for n, v in (("VERCEL_TOKEN", token), ("VERCEL_PROJECT_ID", project)) if not v]
        return {"durable": False,
                "reason": f"set {' + '.join(missing)} in the Vercel project env to persist "
                          "UI-saved keys across redeploys (token: vercel.com/account/tokens); "
                          "until then this key lives only on the current instance"}
    import requests
    blob = _STORE.read_text(encoding="utf-8")
    try:
        resp = requests.post(
            f"https://api.vercel.com/v10/projects/{project}/env?upsert=true",
            headers={"Authorization": f"Bearer {token}"},
            json={"key": "O2S_VAULT", "value": blob, "type": "encrypted",
                  "target": ["production", "preview"]},
            timeout=20)
        if resp.status_code < 300:
            os.environ["O2S_VAULT"] = blob  # current process sees the latest too
            return {"durable": True, "reason": "vault synced to Vercel env (O2S_VAULT); "
                                               "future boots rehydrate automatically"}
        return {"durable": False, "reason": f"Vercel API HTTP {resp.status_code}: {resp.text[:150]}"}
    except Exception as e:  # noqa: BLE001
        return {"durable": False, "reason": f"Vercel API unreachable: {e}"}


def _save(data: Dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fingerprint(key: str) -> str:
    return f"…{key[-4:]}" if key and len(key) >= 4 else "(set)"


# ── LLM provider connections ─────────────────────────────────────────────────

def save_provider(name: str, provider_type: str, base_url: str, api_key: Optional[str]) -> Dict:
    """Create/update. api_key=None on update keeps the existing key."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError("provider name must be 1-40 chars of letters/digits/_/-")
    with _lock:
        data = _load()
        entry = data["providers"].get(name, {})
        if api_key:
            entry["key_enc"] = _fernet().encrypt(api_key.encode()).decode()
            entry["fp"] = fingerprint(api_key)
        elif "key_enc" not in entry:
            raise ValueError("an API key is required for a new provider")
        entry["type"] = provider_type
        entry["base_url"] = base_url.rstrip("/")
        data["providers"][name] = entry
        _save(data)
        return public_provider(name, entry)


def delete_provider(name: str) -> bool:
    with _lock:
        data = _load()
        existed = data["providers"].pop(name, None) is not None
        _save(data)
        return existed


def list_providers() -> List[Dict]:
    return [public_provider(n, e) for n, e in sorted(_load()["providers"].items())]


def public_provider(name: str, entry: Dict) -> Dict:
    """The ONLY provider shape that ever leaves the server — no key material."""
    return {"name": name, "type": entry.get("type", "openai-compatible"),
            "base_url": entry.get("base_url", ""), "key_fingerprint": entry.get("fp", "")}


def get_provider_secret(name: str) -> Optional[Dict]:
    """Server-internal only: full config incl. decrypted key. NEVER serialize this."""
    entry = _load()["providers"].get(name)
    if not entry:
        return None
    try:
        key = _fernet().decrypt(entry["key_enc"].encode()).decode()
    except (InvalidToken, KeyError):
        raise RuntimeError(f"cannot decrypt key for provider {name!r} — "
                           "has CRED_SECRET changed since it was saved?")
    return {"type": entry.get("type", "openai-compatible"),
            "base_url": entry.get("base_url", ""), "api_key": key}


# ── research-source secrets (same vault, separate namespace) ────────────────

def save_source_secret(source_id: str, api_key: str) -> str:
    with _lock:
        data = _load()
        data["source_secrets"][source_id] = {
            "key_enc": _fernet().encrypt(api_key.encode()).decode(),
            "fp": fingerprint(api_key),
        }
        _save(data)
        return data["source_secrets"][source_id]["fp"]


def get_source_secret(source_id: str) -> Optional[str]:
    entry = _load()["source_secrets"].get(source_id)
    if not entry:
        return None
    return _fernet().decrypt(entry["key_enc"].encode()).decode()


def source_secret_fingerprint(source_id: str) -> Optional[str]:
    entry = _load()["source_secrets"].get(source_id)
    return entry["fp"] if entry else None


def delete_source_secret(source_id: str) -> None:
    with _lock:
        data = _load()
        data["source_secrets"].pop(source_id, None)
        _save(data)


def redact(text: str) -> str:
    """Defense-in-depth scrub applied to anything written to events/logs: strips
    key-shaped tokens and any currently-stored secret values."""
    if not text:
        return text
    out = re.sub(r"\b(sk-[A-Za-z0-9_\-]{6,}|ghp_[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9\-]{10,})",
                 "[REDACTED-KEY]", str(text))
    try:
        data = _load()
        f = _fernet()
        for entry in list(data["providers"].values()) + list(data["source_secrets"].values()):
            try:
                secret = f.decrypt(entry["key_enc"].encode()).decode()
                if secret and secret in out:
                    out = out.replace(secret, "[REDACTED-KEY]")
            except Exception:
                continue
    except Exception:
        pass
    return out
