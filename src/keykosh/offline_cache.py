"""Encrypted, integrity-sealed, TTL-bounded last-known-good config cache on local disk."""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"K2C1"
_IV_LEN = 12
_TAG_LEN = 16
_HMAC_LEN = 32
_HEADER_MIN = len(_MAGIC) + 8 + _IV_LEN + 4 + _HMAC_LEN
DEFAULT_TTL_MILLIS = 24 * 60 * 60 * 1000


class OfflineConfigCache:
    """Stores one snapshot per environment under the cache dir (default ``~/.k2/cache``,
    override with ``K2_CACHE_DIR``).

    At rest the snapshot is **AES-256-GCM encrypted, HMAC-SHA256 sealed, and TTL-bounded**,
    with both keys derived from the SDK token — so rotating the token invalidates the
    snapshot and a file written under one token is unreadable under another. The on-disk
    format is byte-compatible with the Java/Node SDK ``K2C1`` cache.
    """

    def __init__(self, cache_dir: Optional[str] = None, ttl_millis: int = DEFAULT_TTL_MILLIS) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else self.default_dir()
        self.ttl_millis = ttl_millis

    @staticmethod
    def default_dir() -> Path:
        override = os.environ.get("K2_CACHE_DIR")
        if override and override.strip():
            return Path(override)
        return Path.home() / ".k2" / "cache"

    def save(self, environment: str, token: str, properties: Mapping[str, Any]) -> None:
        """Best-effort write — never raises; a cache write must not break the app."""
        if properties is None or not token:
            return
        try:
            import json

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _try_chmod(self.cache_dir, 0o700)

            plaintext = json.dumps(properties).encode("utf-8")
            aes_key = _derive_key(token, "k2-cache-aes-v1")
            hmac_key = _derive_key(token, "k2-cache-hmac-v1")

            iv = os.urandom(_IV_LEN)
            ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, None)  # ct || 16-byte tag
            expires_at = int(time.time() * 1000) + self.ttl_millis if self.ttl_millis > 0 else 0

            body = _MAGIC + struct.pack(">q", expires_at) + iv + struct.pack(">I", len(ciphertext)) + ciphertext
            mac = hmac.new(hmac_key, body, hashlib.sha256).digest()
            out = body + mac

            tmp = self.cache_dir / f".k2-{os.getpid()}-{time.time_ns()}.tmp"
            tmp.write_bytes(out)
            _try_chmod(tmp, 0o600)
            os.replace(tmp, self._file_for(environment))  # atomic replace
        except Exception as e:  # noqa: BLE001 - best-effort
            sys.stderr.write(f"[k2-sdk] offline cache write failed for env '{environment}': {e}\n")

    def load(self, environment: str, token: str) -> Optional[dict[str, Any]]:
        """Return the snapshot only if present, untampered, correctly-keyed, and fresh."""
        if not token:
            return None
        file = self._file_for(environment)
        try:
            raw = file.read_bytes()
        except OSError:
            return None
        if len(raw) < _HEADER_MIN:
            _warn(environment, "is truncated; refusing")
            return None
        try:
            import json

            aes_key = _derive_key(token, "k2-cache-aes-v1")
            hmac_key = _derive_key(token, "k2-cache-hmac-v1")

            body_len = len(raw) - _HMAC_LEN
            expected = hmac.new(hmac_key, raw[:body_len], hashlib.sha256).digest()
            actual = raw[body_len:]
            if not hmac.compare_digest(expected, actual):
                _warn(environment, "failed integrity check; refusing (tampered or wrong token)")
                return None

            off = 0
            if raw[: len(_MAGIC)] != _MAGIC:
                return None
            off += len(_MAGIC)
            (expires_at,) = struct.unpack_from(">q", raw, off); off += 8
            if expires_at > 0 and int(time.time() * 1000) > expires_at:
                _warn(environment, "is past its TTL; refusing (stale)")
                return None
            iv = raw[off:off + _IV_LEN]; off += _IV_LEN
            (ct_len,) = struct.unpack_from(">I", raw, off); off += 4
            ciphertext = raw[off:off + ct_len]
            if len(ciphertext) != ct_len:
                return None

            plaintext = AESGCM(aes_key).decrypt(iv, ciphertext, None)
            return json.loads(plaintext.decode("utf-8"))
        except Exception:  # noqa: BLE001 - refuse on any failure
            _warn(environment, "could not be decrypted; refusing (wrong token or corruption)")
            return None

    def _file_for(self, environment: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(environment))
        return self.cache_dir / f"config-{safe}.json.enc"


def _derive_key(token: str, info: str) -> bytes:
    # Single HMAC-SHA256 pass over (token || info); the token is already high-entropy.
    return hmac.new(token.encode("utf-8"), info.encode("utf-8"), hashlib.sha256).digest()


def _try_chmod(target: Path, mode: int) -> None:
    try:
        os.chmod(target, mode)
    except OSError:
        pass  # non-POSIX; ignore


def _warn(environment: str, msg: str) -> None:
    sys.stderr.write(f"[k2-sdk] offline snapshot for env '{environment}' {msg}\n")
