# KeyKosh Python SDK (`keykosh-sdk`)

Read configuration from your **self-hosted KeyKosh (K2) platform**. One dependency
(`cryptography`); HTTP via the standard library. Python 3.9+.

The SDK talks to **exactly one host — your own platform**. There is no vendor default URL
and no callback home; `base_url` is required.

## Install

```bash
pip install keykosh-sdk
```

## Quick start

```python
from keykosh import create_client

# Reads K2_BASE_URL / K2_TOKEN / K2_ENV from the environment; kwargs override.
k2 = create_client(
    base_url="https://k2.acme.com",
    env="prod",
    cache_ttl_seconds=30,     # optional in-memory TTL cache
    offline_cache=True,       # optional last-known-good fallback (~/.k2/cache)
)

cfg = k2.get_configuration()                       # full config for the default env
db_url = cfg.get_string("db.url", "postgresql://localhost/app")
debug = cfg.get_bool("feature.debug", False)

pool_size = k2.get_property("prod", "pool.size")   # single property
```

## What it does

- **Token-scoped reads** against `GET /api/config/token/{env}/current` and
  `/properties/{key}`, authenticated with `Authorization: Bearer <token>` (and `X-API-Token`).
- **TTL cache** (`cache_ttl_seconds`) so repeated reads don't hit the network each call; serves
  the last value through a failed refresh.
- **Encrypted offline cache** (`offline_cache=True`, **Pro & Pro+**) — on a successful fetch the resolved config
  is written to disk **AES-256-GCM-encrypted, HMAC-sealed, and TTL-bounded**, with keys derived
  from the SDK token (rotating the token invalidates the snapshot). If the platform is
  unreachable, the last-known-good snapshot is served instead of raising. Auth/not-found errors
  (401/403/404/421) always surface. The on-disk format is byte-compatible with the Java and
  Node SDK `K2C1` cache. On the FREE tier the server signals `offlineCacheAllowed: false` and
  the SDK skips the cache write.

## Errors

All failures raise `K2Error` with a `status_code` (HTTP status, or `-1` for transport/config
errors). `err.is_availability_error()` is true for transport failures and 5xx.

## Configuration via environment

| Var | Meaning |
|---|---|
| `K2_BASE_URL` | platform URL, e.g. `https://k2.acme.com` |
| `K2_TOKEN` | SDK token (the one secret — never commit it) |
| `K2_ENV` | default environment for the no-arg reads |
| `K2_CACHE_DIR` | offline cache directory (default `~/.k2/cache`) |

## Test

```bash
python tests/test_smoke.py   # offline-cache crypto + config parsing, no network needed
```
