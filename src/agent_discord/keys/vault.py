"""Workspace key vault: HMAC-SHA256 keystream XOR, ciphertext only.

Stores ``{keys_dir}/master.key`` (32 random bytes, mode 0600) and
``{keys_dir}/vault.json`` (nonce + ciphertext + public metadata).
Never writes plaintext secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


def fingerprint_secret(secret: str) -> str:
    """Public fingerprint: last 4 characters of the secret."""

    text = (secret or "").strip()
    if not text:
        return ""
    return text[-4:]


def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream))


def _keystream(master: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            master,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


class KeyVault:
    """Stdlib vault keyed by provider name. Ciphertext only on disk."""

    def __init__(self, keys_dir: str | Path) -> None:
        self.keys_dir = Path(keys_dir)
        self.master_path = self.keys_dir / "master.key"
        self.vault_path = self.keys_dir / "vault.json"

    def put(self, provider: str, secret: str, source: str) -> dict[str, str]:
        name = (provider or "").strip().lower()
        text = (secret or "").strip()
        if not name:
            raise ValueError("provider is required")
        if not text:
            raise ValueError("secret is required")
        master = self._load_or_create_master()
        nonce = os.urandom(16)
        raw = text.encode("utf-8")
        ciphertext = _xor(raw, _keystream(master, nonce, len(raw)))
        created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        fp = fingerprint_secret(text)
        payload = self._read_payload()
        entries = payload.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            payload["entries"] = entries
        entries[name] = {
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "source": source,
            "fingerprint": fp,
            "created_at": created,
        }
        self._write_payload(payload)
        return {
            "provider": name,
            "source": source,
            "fingerprint": fp,
            "created_at": created,
        }

    def get(self, provider: str) -> Optional[str]:
        name = (provider or "").strip().lower()
        entry = self._entry(name)
        if entry is None:
            return None
        nonce_hex = str(entry.get("nonce") or "")
        ct_hex = str(entry.get("ciphertext") or "")
        if not nonce_hex or not ct_hex:
            return None
        master = self._load_or_create_master()
        nonce = bytes.fromhex(nonce_hex)
        ciphertext = bytes.fromhex(ct_hex)
        plain = _xor(ciphertext, _keystream(master, nonce, len(ciphertext)))
        return plain.decode("utf-8")

    def fingerprint(self, provider: str) -> str:
        name = (provider or "").strip().lower()
        entry = self._entry(name)
        if entry is None:
            return ""
        stored = str(entry.get("fingerprint") or "")
        if stored:
            return stored
        secret = self.get(name)
        return fingerprint_secret(secret or "")

    def list_public(self) -> list[dict[str, str]]:
        payload = self._read_payload()
        entries = payload.get("entries") or {}
        if not isinstance(entries, Mapping):
            return []
        out: list[dict[str, str]] = []
        for provider, raw in entries.items():
            if not isinstance(raw, Mapping):
                continue
            out.append(
                {
                    "provider": str(provider),
                    "source": str(raw.get("source") or ""),
                    "fingerprint": str(raw.get("fingerprint") or ""),
                    "created_at": str(raw.get("created_at") or ""),
                }
            )
        out.sort(key=lambda item: item["provider"])
        return out

    def _entry(self, provider: str) -> Optional[Mapping[str, Any]]:
        payload = self._read_payload()
        entries = payload.get("entries") or {}
        if not isinstance(entries, Mapping):
            return None
        raw = entries.get(provider)
        return raw if isinstance(raw, Mapping) else None

    def _load_or_create_master(self) -> bytes:
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        if self.master_path.is_file():
            data = self.master_path.read_bytes()
            if len(data) == 32:
                return data
        data = os.urandom(32)
        self.master_path.write_bytes(data)
        try:
            os.chmod(self.master_path, 0o600)
        except OSError:
            pass
        return data

    def _read_payload(self) -> dict[str, Any]:
        if not self.vault_path.is_file():
            return {"version": 1, "entries": {}}
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"version": 1, "entries": {}}
        return raw if isinstance(raw, dict) else {"version": 1, "entries": {}}

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
        self.vault_path.write_text(text, encoding="utf-8")
        try:
            os.chmod(self.vault_path, 0o600)
        except OSError:
            pass
