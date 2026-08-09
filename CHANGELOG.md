# Changelog

All notable changes to `keykosh-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-08-03

Offline modes and hot reload, per `OFFLINE_AND_HOTRELOAD_DESIGN.md`. No server upgrade is
required: against an older platform the change stream 404s and `watch()` polls instead.

### Added

- **Hot reload** — `watch(env, handler)` subscribes to the platform's change stream over
  Server-Sent Events and fires when an admin edits config, with no polling. Runs on a daemon
  thread, reconnects with backoff, degrades to polling when a proxy strips SSE, and refreshes
  the local file on every push. Returns an unsubscribe callable.
- **Stable error codes** — every `K2Error` now carries a greppable `code`
  (`K2_UNREACHABLE`, `K2_FILE_APP_MISMATCH`, …) alongside `status_code`.
  `is_availability_error()` is now a function of the code.
- `K2_OFFLINE` / `K2_OFFLINE_CACHE` / `K2_HOT_RELOAD` / `K2_OFFLINE_MAX_AGE` environment
  wiring, and `K2ConfigStore` as the public local-file API.

### Changed

- **Zero runtime dependencies.** `cryptography` existed solely for the K2C1 cache's AES/HMAC;
  with the local file plaintext, the standard library covers everything.
- **The offline cache is now a plaintext, self-describing `k2config-<env>.json`** replacing
  the encrypted binary `K2C1` blob. It records its own org/app/env, so two apps sharing a
  directory fail with `K2_FILE_APP_MISMATCH` naming both instead of silently serving each
  other's config, and it can be read and hand-edited. `_k2.managed: false` takes ownership of
  a file and the SDK will never overwrite it.
  **The file holds secret values in clear — gitignore `k2config-*.json`.**
- The token-derived encryption keys are gone, so an STS workload identity (which has no
  static token) can keep an offline file for the first time.
- No hard TTL by default. K2C1's 24h expiry existed to bound how long secrets stayed usable on
  disk; with hot reload keeping the file current, refusing to boot during an outage is the
  worse failure. Set `K2_OFFLINE_MAX_AGE` to reinstate a limit.
- A read-only filesystem no longer breaks the app: a failed write is logged once as
  `K2_FILE_NOT_WRITABLE` and suppressed, never raised.

### Fixed

- During an outage, a local file that exists but **cannot be used** now raises its own
  diagnosis (`K2_FILE_APP_MISMATCH`, `K2_FILE_STALE`, `K2_FILE_MALFORMED`) instead of being
  downgraded to a WARN and reported as the generic — and untrue — "no offline file exists".

### Deprecated

- `K2_SOURCE` (use `K2_OFFLINE`) and `K2_CACHE_DIR` (use `K2_CONFIG_DIR`). Both still work for
  one minor release and log a warning.

### Removed

- `OfflineConfigCache`, `K2ConfigFileSource`, and the `K2C1` binary format. Legacy
  `~/.k2/cache/config-<env>.json.enc` files are unlinked on the first successful write.

## [1.0.0] - 2026-07-03

### Added

- Initial release of the KeyKosh (K2) Python SDK (`keykosh-sdk`, import package
  `keykosh`). Python 3.9+, single runtime dependency (`cryptography`); HTTP via
  the standard library.
- Token-scoped configuration reads against a self-hosted K2 platform:
  - `K2Client.get_configuration([env])` — full config for an environment
    (`GET /api/config/token/{env}/current`).
  - `K2Client.get_property(env, key)` / `get_string(env, key, default)` — single
    property (`GET /api/config/token/{env}/properties/{key}`).
  - `create_client(...)` / `K2Client.from_env(...)` bootstrap from
    `K2_BASE_URL` / `K2_TOKEN` / `K2_ENV`, with keyword overrides. `base_url` is
    required — no vendor default, no callback home.
- In-memory **TTL cache** (`cache_ttl_seconds`) via `get_cached_configuration()`;
  serves the last good value through a failed refresh.
- Encrypted **offline last-known-good cache** (`offline_cache=True` /
  `OfflineConfigCache`): AES-256-GCM encrypted, HMAC-SHA256 sealed, TTL-bounded,
  keys derived from the SDK token (token rotation invalidates snapshots). On-disk
  `K2C1` format is byte-compatible with the Java and Node SDKs. Honors the
  server-driven `offlineCacheAllowed` compatibility flag.
- `K2Configuration` typed accessors: `get_string` / `get_int` / `get_float` /
  `get_bool` / `has` / `to_dict`.
- `K2Error` with `status_code` and `is_availability_error()`; auth/not-found/host
  errors (401/403/404/421) always surface, transport failures and 5xx are
  offline-cache-eligible.
- `py.typed` marker — the package ships inline type hints (PEP 561).

[Unreleased]: https://github.com/k2platform/k2-sdk-python/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/k2platform/k2-sdk-python/releases/tag/v1.1.0
[1.0.0]: https://github.com/k2platform/k2-sdk-python/releases/tag/v1.0.0
