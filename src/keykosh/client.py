"""Client for reading configuration from a self-hosted KeyKosh (K2) platform."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Union

from .config_store import K2ConfigStore, human_age, parse_duration
from .configuration import K2Configuration
from .errors import K2Error, K2ErrorCode
from .kms_decrypt import kms_decrypt
from .stream import ConfigStream
from .sts_signer import aws_sts_signer

SDK_VERSION = "python/1.2.0"
DEFAULT_CACHE_TTL_SECONDS = 300.0


class K2Client:
    """Reads token-scoped config from your own platform.

    Two personas, two booleans (OFFLINE_AND_HOTRELOAD_DESIGN.md §1):

    ==============================================  ==================  ====================
    setting                                         server              local file
    ==============================================  ==================  ====================
    ``offline=False`` (default)                     source of truth     used when unreachable
    ``offline=True``                                never contacted     source of truth
    ``offline=False, offline_cache=False``          only source         none
    ==============================================  ==================  ====================

    ``offline=False`` does **not** mean "no offline support" — it still keeps a local
    ``k2config-<env>.json``, and that is the whole resilience story. It means "the server is
    authoritative", which is what makes it safe for the SDK to overwrite the file.

    Auth: an SDK token (app- or org-scoped), a KMS-envelope token (``token_enc``), or an AWS
    STS workload identity. The SDK talks to exactly one host — your own platform. There is no
    vendor default and no callback home.

    - app-scoped token : ``GET /api/config/token/{env}/current`` · ``/stream``
    - org-scoped token : ``GET /api/config/token/{app}/{env}/current`` · ``/stream``
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        env: Optional[str] = None,
        request_timeout: float = 10.0,
        cache_ttl_seconds: Optional[float] = None,
        *,
        offline: bool = False,
        offline_cache: bool = True,
        hot_reload: Optional[bool] = None,
        token_file: Optional[str] = None,
        token_enc: Optional[str] = None,
        token_decryptor: Optional[Callable[[str], str]] = None,
        org: Optional[str] = None,
        app: Optional[str] = None,
        config_dir: Optional[str] = None,
        config_file: Optional[str] = None,
        offline_max_age: Optional[Union[str, float]] = None,
        config_store: Optional[K2ConfigStore] = None,
        sts: bool = False,
        sts_signer: Optional[Callable[[], dict[str, Any]]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.offline = bool(offline)
        self.offline_cache_enabled = bool(offline_cache)
        self.organization = org or None
        self.application = app or None
        self.default_environment = env or None
        self.request_timeout = request_timeout
        # A non-positive TTL means "no TTL cache", never "poll as fast as possible" — the
        # watch() fallback poller reads this interval too.
        self.cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds and cache_ttl_seconds > 0
            else DEFAULT_CACHE_TTL_SECONDS
        )
        self.hot_reload = (not self.offline) if hot_reload is None else bool(hot_reload)
        self._log = logger or (lambda line: print(line, file=sys.stderr))

        # --- fail fast on configuration errors, at construction, not on first read ---
        if self.offline and not self.offline_cache_enabled:
            raise K2Error(
                K2ErrorCode.INVALID_MODE,
                "K2: offline=True with offline_cache=False is contradictory — offline mode "
                "reads the local file, and offline_cache=False says there is no local file.\n"
                "Set K2_OFFLINE=true (file is the source of truth) or K2_OFFLINE_CACHE=false "
                "(server only, nothing on disk), not both.",
            )

        self.sts_signer: Optional[Callable[[], dict[str, Any]]] = sts_signer or (
            aws_sts_signer if sts else None
        )
        self.base_url = str(base_url).rstrip("/") if base_url else None
        self.token = token or None

        # Credential precedence: explicit token → K2_TOKEN_FILE → K2_TOKEN_ENC. (K2_TOKEN is
        # bound onto ``token`` by from_env, so it shares the first rung.)
        #
        # The file is read even in offline mode: naming an unreadable file is a
        # misconfiguration worth reporting wherever it happens, and reading it costs one
        # syscall. K2_TOKEN_ENC is different — decrypting it is a KMS round trip, so it stays
        # lazy and offline mode, which is fully local, never reaches it.
        if not self.token and token_file:
            self.token = _read_token_file(token_file)

        self._token_enc = token_enc or None
        self._token_decryptor = token_decryptor or kms_decrypt

        if not self.offline:
            if not self.base_url:
                raise K2Error(
                    K2ErrorCode.MISSING_BASE_URL,
                    "K2: no platform URL configured. Set K2_BASE_URL, or pass base_url=, to "
                    "your own platform (e.g. http://localhost:8080 or https://k2.acme.com).\n"
                    "The SDK never falls back to a vendor URL. To run with no server at all, "
                    "set K2_OFFLINE=true.",
                )
            if not self.token and not self._token_enc and self.sts_signer is None:
                raise K2Error(
                    K2ErrorCode.MISSING_TOKEN,
                    "K2: no token configured. Set K2_TOKEN, or K2_TOKEN_FILE, or K2_TOKEN_ENC, "
                    "or pass token=.\n"
                    f"Reading from {self.base_url} needs a token minted in Admin → Tokens, "
                    "scoped to this app and environment.",
                )

        if config_store is not None:
            self.store: Optional[K2ConfigStore] = config_store
        elif self.offline_cache_enabled:
            self.store = K2ConfigStore(
                dir=config_dir,
                file=config_file,
                max_age_seconds=parse_duration(offline_max_age),
                sdk_version=SDK_VERSION,
            )
        else:
            self.store = None

        self._ttl_cache: dict[str, tuple[K2Configuration, float]] = {}
        self._announced: set[str] = set()
        self._legacy_swept = False
        # The app slug the SERVER resolved on the last successful fetch. ``app``/``K2_APP`` is
        # optional, so without this a fallback read would have nothing to validate the file's
        # ``_k2.app`` against — and would serve a co-located app's config silently. Learning it
        # from the server closes that for any process that fetched successfully at least once;
        # setting ``K2_APP`` is still what makes it a checked invariant from the first cold read.
        self._learned_app: Optional[str] = None

    # ---- construction from env ----

    @classmethod
    def from_env(cls, **overrides: Any) -> "K2Client":
        """Build from the ``K2_*`` environment variables, with keyword overrides.

        Env: ``K2_BASE_URL``, ``K2_TOKEN``, ``K2_TOKEN_FILE``, ``K2_TOKEN_ENC``, ``K2_ENV``,
        ``K2_ORG``, ``K2_APP``, ``K2_OFFLINE``, ``K2_OFFLINE_CACHE``, ``K2_HOT_RELOAD``,
        ``K2_CONFIG_DIR``, ``K2_CONFIG_FILE``, ``K2_OFFLINE_MAX_AGE``, ``K2_CACHE_TTL``,
        ``K2_STS_ENABLED``.

        Deprecated one release: ``K2_SOURCE`` (→ ``K2_OFFLINE``) and ``K2_CACHE_DIR``
        (→ ``K2_CONFIG_DIR``).
        """
        e = os.environ
        return cls(
            base_url=overrides.pop("base_url", e.get("K2_BASE_URL")),
            token=overrides.pop("token", e.get("K2_TOKEN")),
            token_file=overrides.pop("token_file", e.get("K2_TOKEN_FILE")),
            token_enc=overrides.pop("token_enc", e.get("K2_TOKEN_ENC")),
            env=overrides.pop("env", e.get("K2_ENV")),
            org=overrides.pop("org", e.get("K2_ORG")),
            app=overrides.pop("app", e.get("K2_APP")),
            offline=overrides.pop("offline", _resolve_offline(e)),
            offline_cache=overrides.pop("offline_cache", _bool(e.get("K2_OFFLINE_CACHE"), True)),
            hot_reload=overrides.pop("hot_reload", _bool_or_none(e.get("K2_HOT_RELOAD"))),
            config_dir=overrides.pop("config_dir", e.get("K2_CONFIG_DIR") or _deprecated_cache_dir(e)),
            config_file=overrides.pop("config_file", e.get("K2_CONFIG_FILE")),
            offline_max_age=overrides.pop("offline_max_age", e.get("K2_OFFLINE_MAX_AGE")),
            cache_ttl_seconds=overrides.pop("cache_ttl_seconds", parse_duration(e.get("K2_CACHE_TTL"))),
            sts=overrides.pop("sts", e.get("K2_STS_ENABLED") in ("true", "1")),
            **overrides,
        )

    # ---- reads ----

    def get_configuration(self, environment: Optional[str] = None) -> K2Configuration:
        """Full config for ``environment``.

        ``offline=True`` reads the local file and never contacts the server. Otherwise the
        server is fetched, the local file refreshed, and — only on an availability failure —
        the local file served as the last-known-good. Auth failures (401/403/404/421) always
        surface: a valid file on disk is deliberately declined, because that is not an outage.
        """
        environment = environment or self._require_env()

        if self.offline:
            hit = self.store.load(environment, app=self.application, required=True)
            self._announce_file(environment, hit)
            return K2Configuration.from_file(
                hit.header.get("org") or self.organization,
                hit.header.get("app") or self.application,
                environment,
                hit.properties,
            )

        try:
            if self.sts_signer is not None:
                body = self._send_sts(self._sts_current_path(environment))
            else:
                body = self._send(self._current_path(environment), self._resolve_token())
            cfg = K2Configuration.from_response(body)
            written = self._persist(environment, cfg)
            self._announce_server(environment, cfg, written)
            return cfg
        except K2Error as e:
            if self.store is not None and e.is_availability_error():
                hit = self._load_for_fallback(environment, e)
                if hit is not None:
                    self._log(
                        f"K2: {self.base_url} unreachable ({e.outage_detail}) — serving "
                        f"{len(hit.properties)} keys for env '{environment}' from {hit.path} "
                        f"(written {human_age(hit.age_seconds)} ago)"
                    )
                    return K2Configuration.from_properties(environment, hit.properties)
                if e.code in (K2ErrorCode.UNREACHABLE, K2ErrorCode.TIMEOUT):
                    raise K2Error(
                        e.code,
                        f"K2: {self.base_url} is unreachable ({e}) and no offline file exists "
                        f"for env '{environment}' at {self.store.write_target(environment)}.\n"
                        "The application has no configuration. Start K2, or provide an "
                        "offline file.",
                        e.status_code,
                        e,
                    ) from e
            raise

    def get_cached_configuration(self, environment: Optional[str] = None) -> K2Configuration:
        """Served from the in-memory TTL cache when fresh, else re-fetched.

        Holds the last value through a failed refresh.
        """
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
        if self.offline:
            hit = self.store.load(environment, app=self.application, required=True)
            return hit.properties.get(key)
        if self.sts_signer is not None:
            path = (
                f"/api/config/sts/{_enc(self._require_app_for_sts())}/{_enc(environment)}"
                f"/properties/{_enc(key)}"
            )
            body = self._send_sts(path)
            return None if body is None else body.get("value")
        body = self._send(self._property_path(environment, key), self._resolve_token())
        return None if body is None else body.get("value")

    def get_string(self, environment: str, key: str, default: Optional[str] = None) -> Optional[str]:
        v = self.get_property(environment, key)
        return default if v is None else str(v)

    # ---- hot reload ----

    def watch(
        self,
        environment: Optional[str] = None,
        handler: Optional[Callable[[K2Configuration], None]] = None,
    ) -> Callable[[], None]:
        """Subscribe to configuration changes; returns an ``unsubscribe()`` callable.

        Transport is Server-Sent Events over ``urllib`` — no new dependency, and it survives
        ALBs and proxies. The stream carries a *signal*, not values: on ``config.changed`` the
        SDK re-fetches ``/current``, so secrets never sit on a long-lived connection and a
        client that missed events while disconnected self-heals on reconnect.

        Never fails the application: if the stream cannot be established (an older server, a
        proxy that strips SSE), the SDK falls back to polling at ``cache_ttl_seconds`` and says
        so once. With ``offline=True`` hot reload is meaningless, so no stream is opened.
        """
        environment = environment or self._require_env()
        handler = handler or (lambda _cfg: None)

        if self.offline:
            self._log(
                f"K2: hot reload is off for env '{environment}' — offline=True means there is "
                "no server to subscribe to."
            )
            return lambda: None

        stopped = threading.Event()
        poll_timer: list[Optional[threading.Timer]] = [None]

        def refresh(_event: Any = None) -> None:
            if stopped.is_set():
                return
            try:
                cfg = self.get_configuration(environment)
                self._ttl_cache[environment] = (cfg, time.monotonic() + self.cache_ttl_seconds)
                handler(cfg)
            except K2Error as e:
                self._log(f"K2: hot-reload refresh for env '{environment}' failed: {e}")

        def poll_once() -> None:
            refresh()
            schedule_poll()

        def schedule_poll() -> None:
            if stopped.is_set():
                return
            timer = threading.Timer(self.cache_ttl_seconds, poll_once)
            timer.daemon = True
            poll_timer[0] = timer
            timer.start()

        def start_polling(why: str) -> None:
            if stopped.is_set() or poll_timer[0] is not None:
                return
            self._log(
                f"K2: change stream unavailable for env '{environment}' ({why}) — falling back "
                f"to polling every {round(self.cache_ttl_seconds)}s."
            )
            schedule_poll()

        stream: list[Optional[ConfigStream]] = [None]
        if not self.hot_reload:
            start_polling("hot_reload disabled")
        else:
            stream[0] = ConfigStream(
                url=self.base_url + self._stream_path(environment),
                token=self._resolve_token,
                on_event=refresh,
                on_unavailable=start_polling,
                on_retry=lambda delay: self._log(
                    f"K2: change stream for env '{environment}' dropped — reconnecting in "
                    f"{round(delay)}s."
                ),
            )
            stream[0].start()

        def unsubscribe() -> None:
            stopped.set()
            if stream[0] is not None:
                stream[0].stop()
            if poll_timer[0] is not None:
                poll_timer[0].cancel()

        return unsubscribe

    # ---- internals ----

    def _persist(self, environment: str, cfg: K2Configuration):
        """Write the file after a successful fetch; return the path written (or ``None``)."""
        if self.store is None:
            return None
        app = cfg.application or self.application
        if app:
            self._learned_app = app
        written = self.store.save(
            environment,
            cfg.properties,
            org=cfg.organization or self.organization,
            app=app,
        )
        if written is not None and not self._legacy_swept:
            self._legacy_swept = True
            K2ConfigStore.unlink_legacy(environment)
        return written

    def _load_for_fallback(self, environment: str, outage: K2Error):
        """Load the file for the outage fallback.

        Returns ``None`` when there is simply **no** file — the caller then reports the
        outage itself. But when a file *is* present and cannot be used, its own error is
        raised rather than swallowed. Reporting the generic "no offline file exists"
        instead would be both untrue (one exists) and precisely the silent failure this
        format replaced K2C1 to remove: a ``K2_FILE_APP_MISMATCH`` naming both apps is the
        whole point of the app-in-the-file design, and it is worthless if the outage path
        quietly downgrades it to a WARN.

        The outage is folded into the message, because both facts matter: the server was
        gone AND the fallback was unusable.
        """
        try:
            return self.store.load(environment, app=self.application or self._learned_app)
        except K2Error as e:
            raise K2Error(
                e.code,
                f"{e}\nThe platform at {self.base_url} was also unreachable "
                f"({outage.outage_detail}), "
                "so this file was the last-known-good the SDK tried to fall back to.",
                e.status_code,
                e,
            ) from e

    def _announce_server(self, environment: str, cfg: K2Configuration, written) -> None:
        """One INFO line per environment on the first successful read — never silent."""
        if environment in self._announced:
            return
        self._announced.add(environment)
        app = cfg.application or self.application or "this token's app"
        where = f"offline file: {written}" if written else "offline file: off"
        self._log(
            f"K2: loaded {len(cfg.properties)} keys for app '{app}' env '{environment}' from "
            f"{self.base_url}\n    ({where}, hot reload: {'on' if self.hot_reload else 'off'})"
        )

    def _announce_file(self, environment: str, hit) -> None:
        if environment in self._announced:
            return
        self._announced.add(environment)
        app = hit.header.get("app") or self.application or "unknown"
        self._log(
            f"K2: loaded {len(hit.properties)} keys for app '{app}' env '{environment}' from "
            f"{hit.path} (offline=True, written {human_age(hit.age_seconds)} ago)"
        )
        if hit.age_seconds is not None and hit.age_seconds > 86400:
            self._log(f"K2: {hit.path} was written {human_age(hit.age_seconds)} ago.")

    def _require_env(self) -> str:
        """The default environment.

        Deliberately raised at read time, not at construction: a client legitimately serves
        several environments via ``get_configuration(env)``, so requiring a default up front
        would break that API. Every other config error does fail at construction.
        """
        if not self.default_environment:
            raise K2Error(
                K2ErrorCode.MISSING_ENV,
                "K2: no environment configured. Set K2_ENV, or pass env=, or call "
                "get_configuration('dev') with an explicit environment.",
            )
        return self.default_environment

    def _resolve_token(self) -> str:
        """The effective token: explicit, ``K2_TOKEN_FILE``, or ``K2_TOKEN_ENC`` (decrypted once).

        The file rung is already resolved at construction — a named-but-unreadable secret mount
        must break at boot, not on the first read an hour later.
        """
        if self.token:
            return self.token
        if self._token_enc:
            self.token = self._token_decryptor(self._token_enc)
            return self.token
        raise K2Error(
            K2ErrorCode.MISSING_TOKEN,
            "K2: no token configured. Set K2_TOKEN, or K2_TOKEN_FILE, or K2_TOKEN_ENC, or "
            "pass token=.",
        )

    def _current_path(self, environment: str) -> str:
        if self.application:
            return f"/api/config/token/{_enc(self.application)}/{_enc(environment)}/current"
        return f"/api/config/token/{_enc(environment)}/current"

    def _stream_path(self, environment: str) -> str:
        if self.application:
            return f"/api/config/token/{_enc(self.application)}/{_enc(environment)}/stream"
        return f"/api/config/token/{_enc(environment)}/stream"

    def _property_path(self, environment: str, key: str) -> str:
        if self.application:
            return (
                f"/api/config/token/{_enc(self.application)}/{_enc(environment)}"
                f"/properties/{_enc(key)}"
            )
        return f"/api/config/token/{_enc(environment)}/properties/{_enc(key)}"

    def _sts_current_path(self, environment: str) -> str:
        return f"/api/config/sts/{_enc(self._require_app_for_sts())}/{_enc(environment)}/current"

    def _require_app_for_sts(self) -> str:
        if not self.application:
            raise K2Error(
                K2ErrorCode.INVALID_MODE,
                "K2: STS auth is org-scoped — set K2_APP (the k2 app slug) so the client can "
                "name the app it reads.",
            )
        return self.application

    def _send_sts(self, path: str) -> Any:
        """POST a signed ``sts:GetCallerIdentity`` envelope and return the parsed body."""
        assert self.sts_signer is not None
        data = json.dumps(self.sts_signer()).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        return self._request(req)

    def _send(self, path: str, token: str) -> Any:
        req = urllib.request.Request(self.base_url + path, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-API-Token", token)
        req.add_header("Accept", "application/json")
        return self._request(req)

    def _request(self, req: urllib.request.Request) -> Any:
        url = req.full_url
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:  # noqa: S310
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise self._http_error(url, e) from e
        except urllib.error.URLError as e:
            timed_out = isinstance(e.reason, TimeoutError) or "timed out" in str(e.reason).lower()
            raise K2Error(
                K2ErrorCode.TIMEOUT if timed_out else K2ErrorCode.UNREACHABLE,
                f"K2: {url} did not respond within {self.request_timeout}s."
                if timed_out
                else f"K2: {url} is unreachable ({e.reason}).",
                -1,
                e,
            ) from e
        except TimeoutError as e:
            raise K2Error(
                K2ErrorCode.TIMEOUT,
                f"K2: {url} did not respond within {self.request_timeout}s.",
                -1,
                e,
            ) from e
        except Exception as e:  # noqa: BLE001
            raise K2Error(K2ErrorCode.UNREACHABLE, f"K2: {url} is unreachable ({e}).", -1, e) from e

        try:
            return json.loads(payload)
        except ValueError as e:
            raise K2Error(
                K2ErrorCode.SERVER_ERROR,
                f"K2: could not parse the response from {url}: {e}",
                -1,
                e,
            ) from e

    def _http_error(self, url: str, e: urllib.error.HTTPError) -> K2Error:
        code = e.code
        if code in (401, 403):
            return K2Error(
                K2ErrorCode.UNAUTHORIZED if code == 401 else K2ErrorCode.FORBIDDEN,
                f"K2: token rejected ({code}) by {url}.\n"
                "The token may be revoked, expired, or scoped to a different app or "
                "environment.\n"
                "The offline file was NOT used — an auth failure is not an outage.",
                code,
            )
        if code == 404:
            return K2Error(
                K2ErrorCode.NOT_FOUND,
                f"K2: {url} returned 404. The app or environment does not exist on this "
                "platform, or the token is scoped elsewhere.\n"
                "The offline file was NOT used — a 404 is not an outage.",
                code,
            )
        if code == 421:
            return K2Error(
                K2ErrorCode.HOST_NOT_LICENSED,
                f"K2: {self.base_url} refused the request host (421) — this host is not "
                "licensed. It must match the platform public host (K2_PUBLIC_HOST). See "
                "LICENSE_BINDING_DESIGN.md.\n"
                "The offline file was NOT used — a licensing refusal is not an outage.",
                code,
            )
        body = _truncate(_safe_read(e))
        return K2Error(
            K2ErrorCode.SERVER_ERROR if code >= 500 else K2ErrorCode.REQUEST_FAILED,
            f"K2: {url} returned HTTP {code} — {body}",
            code,
        )


def create_client(**kwargs: Any) -> K2Client:
    """Build a client from keyword args, falling back to the ``K2_*`` environment variables."""
    return K2Client.from_env(**kwargs)


def _read_token_file(path: str) -> str:
    """The token held in ``path``, stripped of surrounding whitespace.

    Docker and Kubernetes secret mounts almost always end in a newline, and a token never has
    meaningful surrounding whitespace, so the contents are stripped before use.

    A file that cannot be read, or that is empty once stripped, is a
    ``K2_TOKEN_FILE_UNREADABLE`` — never a silent fallthrough to "no token". The operator named
    a file; if it does not hold a token, that is the failure worth reporting, not the generic
    missing-token message it would otherwise become.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            contents = fh.read()
    except OSError as e:
        raise K2Error(
            K2ErrorCode.TOKEN_FILE_UNREADABLE,
            f"K2: the token file '{path}' could not be read ({e}).\n"
            "K2_TOKEN_FILE must name a readable file containing the SDK token.",
            -1,
            e,
        ) from e
    token = contents.strip()
    if not token:
        raise K2Error(
            K2ErrorCode.TOKEN_FILE_UNREADABLE,
            f"K2: the token file '{path}' is empty.\n"
            "K2_TOKEN_FILE must name a file containing the SDK token.",
        )
    return token


def _resolve_offline(env: dict) -> bool:
    """``K2_OFFLINE``, or the deprecated ``K2_SOURCE`` mapped onto it.

    ``file``→True, ``server``→False, ``auto``→True when a file exists for the env, else False.
    """
    if env.get("K2_OFFLINE") is not None:
        return _bool(env.get("K2_OFFLINE"), False)
    source = (env.get("K2_SOURCE") or "").strip().lower()
    if not source:
        return False
    _deprecate(
        f"K2_SOURCE={source} is deprecated — use K2_OFFLINE=true|false. It will be removed "
        "in the next minor release."
    )
    if source == "file":
        return True
    if source == "auto" and env.get("K2_ENV"):
        store = K2ConfigStore(dir=env.get("K2_CONFIG_DIR"), file=env.get("K2_CONFIG_FILE"))
        return store.resolve(env["K2_ENV"]) is not None
    return False


def _deprecated_cache_dir(env: dict) -> Optional[str]:
    if not env.get("K2_CACHE_DIR"):
        return None
    _deprecate(
        "K2_CACHE_DIR is deprecated — use K2_CONFIG_DIR. It will be removed in the next "
        "minor release."
    )
    return env["K2_CACHE_DIR"]


def _bool(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes")


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None or str(value).strip() == "":
        return None
    return _bool(value, False)


def _deprecate(message: str) -> None:
    print(f"[k2-sdk] WARN {message}", file=sys.stderr)


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
