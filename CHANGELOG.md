# Changelog

All notable changes to `keykosh-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  server-driven `offlineCacheAllowed` paid-tier gate.
- `K2Configuration` typed accessors: `get_string` / `get_int` / `get_float` /
  `get_bool` / `has` / `to_dict`.
- `K2Error` with `status_code` and `is_availability_error()`; auth/not-found/host
  errors (401/403/404/421) always surface, transport failures and 5xx are
  offline-cache-eligible.
- `py.typed` marker — the package ships inline type hints (PEP 561).

[Unreleased]: https://github.com/k2platform/k2-sdk-python/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/k2platform/k2-sdk-python/releases/tag/v1.0.0
