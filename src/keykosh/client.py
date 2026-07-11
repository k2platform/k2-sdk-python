"""Client for reading configuration from a self-hosted KeyKosh (K2) platform."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Union

from .config_file_source import K2ConfigFileSource
from .configuration import K2Configuration
from .errors import K2Error
from .kms_decrypt import kms_decrypt
from .offline_cache import OfflineConfigCache
from .sts_signer import aws_sts_signer


class K2Client:
    """Reads token-scoped config.

    Auth: an SDK token (app- or org-scoped), or a KMS-envelope token (``token_enc``, §1b). Reads
    resolve **local file → server → managed cache → defaults** (§3c).

    - app-scoped token : ``GET /api/config/token/{env}/current``
    - org-scoped token : ``GET /api/config/token/{app}/{env}/current``   (app named via ``app``/K2_APP, §2b)

    The SDK talks to exactly one host — your own platform. ``base_url`` is required (unless
    ``source='file'``); there is no vendor default and no callback home.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        env: Optional[str] = None,
        request_timeout: float = 10.0,
        cache_ttl_seconds: Optional[float] = None,
        offline_cache: Union[bool, OfflineConfigCache, None] = None,
        *,
        token_enc: Optional[str] = None,
        token_decryptor: Optional[Callable[[str], str]] = None,
        org: Optional[str] = None,
        app: Optional[str] = None,
        source: str = "auto",
        config_dir: Optional[str] = None,
        config_file: Optional[str] = None,
        file_source: Optional[K2ConfigFileSource] = None,
        sts: bool = False,
        sts_signer: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self.source = (source or "auto").lower()
        self.organization = org or None
        self.application = app or None
        self.file_source = file_source or K2ConfigFileSource(dir=config_dir, file=config_file)
        self.sts_signer: Optional[Callable[[], dict[str, Any]]] = sts_signer or (aws_sts_signer if sts else None)

        server_possible = self.source != "file"
        if server_possible and (not base_url or not str(base_url).strip()):
            raise K2Error(
                "K2 base_url is required — set it to your platform URL (e.g. http://localhost:8080 "
                "or https://k2.acme.com). The SDK never falls back to a vendor URL."
            )
        self.base_url = str(base_url).rstrip("/") if base_url else None
        self.token = token or None
        self._token_enc = token_enc or None
        self._token_decryptor = token_decryptor or kms_decrypt
        self.default_environment = env or None
        self.request_timeout = request_timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        if offline_cache is True:
            self.offline_cache: Optional[OfflineConfigCache] = OfflineConfigCache()
        else:
            self.offline_cache = offline_cache or None
        self._ttl_cache: dict[str, tuple[K2Configuration, float]] = {}

    @classmethod
    def from_env(cls, **overrides: Any) -> "K2Client":
        """Build from the K2_* environment variables, with keyword overrides."""
        env_source = os.environ.get("K2_SOURCE")
        env_sts = os.environ.get("K2_STS_ENABLED")
        return cls(
            base_url=overrides.pop("base_url", os.environ.get("K2_BASE_URL")),
            token=overrides.pop("token", os.environ.get("K2_TOKEN")),
            token_enc=overrides.pop("token_enc", os.environ.get("K2_TOKEN_ENC")),
            env=overrides.pop("env", os.environ.get("K2_ENV")),
            org=overrides.pop("org", os.environ.get("K2_ORG")),
            app=overrides.pop("app", os.environ.get("K2_APP")),
            source=overrides.pop("source", env_source.lower() if env_source else "auto"),
            config_dir=overrides.pop("config_dir", os.environ.get("K2_CONFIG_DIR")),
            config_file=overrides.pop("config_file", os.environ.get("K2_CONFIG_FILE")),
            sts=overrides.pop("sts", env_sts in ("true", "1")),
            **overrides,
        )

    def get_configuration(self, environment: Optional[str] = None) -> K2Configuration:
        """Full config for ``environment``. Resolution: local file → server → managed cache."""
        environment = environment or self._require_env()
        from_file = self._resolve_from_file(environment)
        if from_file is not None:
            return K2Configuration.from_file(self.organization, self.application, environment, from_file)

        if self.sts_signer is not None:
            # STS auth: no static token, so the token-sealed offline cache doesn't apply (STS is online).
            body = self._send_sts(self._sts_current_path(environment))
            return K2Configuration.from_response(body)

        token = self._resolve_token()
        path = self._current_path(environment)
        try:
            body = self._send(path, token)
            cfg = K2Configuration.from_response(body)
            # Offline cache is un-gated (all tiers) — write the last-known-good snapshot on every
            # successful fetch. The server's offlineCacheAllowed flag is ignored (see design §0).
            if self.offline_cache is not None:
                self.offline_cache.save(environment, token, cfg.properties)
            return cfg
        except K2Error as e:
            if self.offline_cache is not None and e.is_availability_error():
                cached = self.offline_cache.load(environment, token)
                if cached is not None:
                    return K2Configuration.from_properties(environment, cached)
            raise

    def get_cached_configuration(self, environment: Optional[str] = None) -> K2Configuration:
        """Served from the in-memory TTL cache when fresh, else re-fetched. Holds the last
        value through a failed refresh."""
        environment = environment or self._require_env()
        if not self.cache_ttl_seconds or self.cache_ttl_seconds <= 0:
            return self.get_configuration(environment)
        now = time.monotonic()
        entry = self._ttl_cache.get(environment)
        if entry and now < entry[1]:
            return entry[0]
        try:
            fresh = self.get_configuration(environment)
            self._ttl_cache[environment] = (fresh, now + self.cache_ttl_seconds)
            return fresh
        except K2Error:
            if entry:
                return entry[0]
            raise

    def get_property(self, environment: str, key: str) -> Any:
        """Single property value for ``key``, or ``None`` if unset."""
        from_file = self._resolve_from_file(environment)
        if from_file is not None:
            return from_file.get(key)
        if self.sts_signer is not None:
            path = f"/api/config/sts/{_enc(self._require_app_for_sts())}/{_enc(environment)}/properties/{_enc(key)}"
            body = self._send_sts(path)
            return None if body is None else body.get("value")
        token = self._resolve_token()
        path = self._property_path(environment, key)
        body = self._send(path, token)
        return None if body is None else body.get("value")

    def get_string(self, environment: str, key: str, default: Optional[str] = None) -> Optional[str]:
        v = self.get_property(environment, key)
        return default if v is None else str(v)

    def _require_env(self) -> str:
        if not self.default_environment:
            raise K2Error("No default environment configured — set env=... or pass an environment argument.")
        return self.default_environment

    def _resolve_from_file(self, environment: str) -> Optional[dict[str, Any]]:
        """Properties for ``environment`` from the local file source, honoring ``source``."""
        if self.file_source is None or self.source == "server":
            return None
        if self.source == "auto" and not self.file_source.has_file_for(self.application):
            return None
        props = self.file_source.load(self.organization, self.application, environment)
        if props is None and self.source == "file":
            raise K2Error(
                f"K2 source=file but no config for env '{environment}' in "
                f"{self.file_source.file_for(self.application)} — add an 'environments.{environment}' block"
            )
        return props

    def _resolve_token(self) -> str:
        """The effective token: explicit, or K2_TOKEN_ENC decrypted via KMS (once)."""
        if self.token:
            return self.token
        if self._token_enc:
            self.token = self._token_decryptor(self._token_enc)
            return self.token
        raise K2Error("K2 token is required — supply K2_TOKEN, K2_TOKEN_ENC, or a local file source.")

    def _current_path(self, environment: str) -> str:
        if self.application:
            return f"/api/config/token/{_enc(self.application)}/{_enc(environment)}/current"
        return f"/api/config/token/{_enc(environment)}/current"

    def _property_path(self, environment: str, key: str) -> str:
        if self.application:
            return f"/api/config/token/{_enc(self.application)}/{_enc(environment)}/properties/{_enc(key)}"
        return f"/api/config/token/{_enc(environment)}/properties/{_enc(key)}"

    def _sts_current_path(self, environment: str) -> str:
        return f"/api/config/sts/{_enc(self._require_app_for_sts())}/{_enc(environment)}/current"

    def _require_app_for_sts(self) -> str:
        if not self.application:
            raise K2Error(
                "STS auth is org-scoped — set K2_APP (the k2 app slug) so the client can name the "
                "app it reads."
            )
        return self.application

    def _send_sts(self, path: str) -> Any:
        """POST a signed sts:GetCallerIdentity envelope (§1a) and return the parsed body."""
        assert self.sts_signer is not None
        signed = self.sts_signer()
        data = json.dumps(signed).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (401, 403):
                raise K2Error(f"K2 credential rejected (HTTP {code}) for {path} — check the identity and its scope", code) from e
            if code == 404:
                raise K2Error(f"K2 config not found (HTTP 404) for {path}", code) from e
            body = _truncate(_safe_read(e))
            raise K2Error(f"K2 STS request to {path} failed: HTTP {code} — {body}", code) from e
        except urllib.error.URLError as e:
            raise K2Error(f"K2 STS request to {path} failed: {e.reason}", -1, e) from e
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise K2Error(f"Failed to parse K2 response from {path}: {e}", -1, e) from e

    def _send(self, path: str, token: str) -> Any:
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-API-Token", token)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (401, 403):
                raise K2Error(
                    f"K2 token rejected (HTTP {code}) for {path} — check the token and its environment scope", code
                ) from e
            if code == 404:
                raise K2Error(f"K2 config not found (HTTP 404) for {path}", code) from e
            if code == 421:
                raise K2Error(
                    f"K2 rejected the request host (HTTP 421) for {self.base_url} — base_url host is not "
                    "licensed; it must match the platform public host (K2_PUBLIC_HOST).", code
                ) from e
            body = _truncate(_safe_read(e))
            raise K2Error(f"K2 request to {path} failed: HTTP {code} — {body}", code) from e
        except urllib.error.URLError as e:
            raise K2Error(f"K2 request to {path} failed: {e.reason}", -1, e) from e
        except Exception as e:  # noqa: BLE001
            raise K2Error(f"K2 request to {path} failed: {e}", -1, e) from e

        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise K2Error(f"Failed to parse K2 response from {path}: {e}", -1, e) from e


def create_client(**kwargs: Any) -> K2Client:
    """Build a client from keyword args, falling back to K2_BASE_URL / K2_TOKEN / K2_ENV."""
    return K2Client.from_env(**kwargs)


def _enc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def _truncate(s: str) -> str:
    if not s:
        return ""
    return s if len(s) <= 300 else s[:300] + "…"


def _safe_read(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
