# KeyKosh Python SDK — User Manual

`keykosh-sdk` reads token-scoped configuration from **your own self-hosted
KeyKosh (K2) platform**. It has a single runtime dependency (`cryptography`);
all HTTP is done with the Python standard library. Python 3.9+.

The SDK talks to **exactly one host — the platform you point it at**. There is
no vendor default URL and no callback home: `base_url` is required and the SDK
never phones anywhere else.

- Import package: `keykosh`
- Distribution name (PyPI): `keykosh-sdk`
- License: MIT

---

## Table of contents

1. [Installation](#1-installation)
2. [Quick start](#2-quick-start)
3. [Getting a token and base URL](#3-getting-a-token-and-base-url)
4. [Usage patterns](#4-usage-patterns)
5. [Configuration reference](#5-configuration-reference)
6. [Reading values (`K2Configuration`)](#6-reading-values-k2configuration)
7. [Error handling](#7-error-handling)
8. [Offline / last-known-good behavior](#8-offline--last-known-good-behavior)
9. [Environment variables & Docker](#9-environment-variables--docker)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Installation

```bash
pip install keykosh-sdk
```

Requires Python 3.9 or newer. The only runtime dependency, `cryptography`, is
pulled in automatically.

To install for local development (tests + build + publish tooling):

```bash
pip install -e ".[dev]"
```

---

## 2. Quick start

```python
from keykosh import create_client

# kwargs override; anything omitted falls back to K2_BASE_URL / K2_TOKEN / K2_ENV.
k2 = create_client(
    base_url="https://k2.acme.com",
    token="k2_live_xxxxxxxx",
    env="prod",
    cache_ttl_seconds=30,     # optional in-memory TTL cache
    offline_cache=True,       # optional encrypted last-known-good fallback
)

# Full config for the default environment
cfg = k2.get_configuration()
db_url  = cfg.get_string("db.url", "postgresql://localhost/app")
debug   = cfg.get_bool("feature.debug", False)
pool    = cfg.get_int("pool.size", 10)

# A single property (explicit environment + key)
pool_size = k2.get_property("prod", "pool.size")
```

---

## 3. Getting a token and base URL

- **`base_url`** — the URL of your running K2 platform, e.g.
  `http://localhost:8080` for a local install or `https://k2.acme.com` for an
  AWS install. Must be the platform's licensed public host.
- **`token`** — an SDK token minted in the K2 admin UI (**Tokens** tab). It is
  scoped to a specific org/app; treat it as a secret and never commit it. Read
  it from an environment variable or your secret manager.

---

## 4. Usage patterns

### 4a. Construct from keyword args

```python
from keykosh import K2Client

k2 = K2Client(
    base_url="https://k2.acme.com",
    token="k2_live_xxxxxxxx",
    env="prod",
)
```

### 4b. Construct from the environment

`create_client()` and `K2Client.from_env()` both read `K2_BASE_URL`,
`K2_TOKEN`, and `K2_ENV`, and let keyword args override any of them:

```python
from keykosh import create_client

# All three read from the environment
k2 = create_client()

# Override just the environment, keep base_url/token from env
k2_staging = create_client(env="staging")
```

`create_client(**kwargs)` is a thin alias for `K2Client.from_env(**kwargs)`.

### 4c. Fresh reads

```python
cfg = k2.get_configuration()            # uses the default env
cfg = k2.get_configuration("staging")   # explicit env
```

`get_configuration()` always attempts a live fetch. On success it (optionally)
refreshes the offline cache; on an availability failure it falls back to the
last-known-good snapshot if one exists (see §8).

### 4d. TTL-cached reads

```python
k2 = create_client(base_url=..., token=..., env="prod", cache_ttl_seconds=30)

cfg = k2.get_cached_configuration()     # served from memory while fresh
```

Within the TTL window the in-memory copy is returned without touching the
network. When the window expires the SDK re-fetches; if that refresh fails it
holds and returns the previous value rather than raising. With
`cache_ttl_seconds` unset (or `<= 0`) this method just delegates to
`get_configuration()`.

### 4e. Single property

```python
raw   = k2.get_property("prod", "pool.size")           # value or None
value = k2.get_string("prod", "pool.size", "10")       # coerced to str, with default
```

---

## 5. Configuration reference

All options are constructor arguments of `K2Client` (and therefore of
`create_client(...)` / `from_env(...)`).

| Option | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | `str` | — (**required**) | Platform URL. Trailing slash is stripped. Empty/blank raises `K2Error`. Env: `K2_BASE_URL`. |
| `token` | `str` | — (**required**) | SDK token used as `Authorization: Bearer` and `X-API-Token`. Empty raises `K2Error`. Env: `K2_TOKEN`. |
| `env` | `str \| None` | `None` | Default environment for the no-arg reads. If unset, no-arg reads raise `K2Error`. Env: `K2_ENV`. |
| `request_timeout` | `float` | `10.0` | Per-request HTTP timeout in **seconds**. |
| `cache_ttl_seconds` | `float \| None` | `None` | In-memory TTL (seconds) for `get_cached_configuration()`. `None`/`<=0` disables it. |
| `offline_cache` | `bool \| OfflineConfigCache \| None` | `None` | `True` enables the default encrypted disk cache; pass an `OfflineConfigCache` instance to customize dir/TTL; `None`/`False` disables it. See §8. |

### Offline cache options (`OfflineConfigCache`)

| Option | Type | Default | Meaning |
|---|---|---|---|
| `cache_dir` | `str \| None` | `~/.k2/cache` (or `$K2_CACHE_DIR`) | Directory for encrypted snapshots (one file per environment). |
| `ttl_millis` | `int` | `86_400_000` (24 h) | On-disk snapshot TTL in **milliseconds**. `0` = no expiry. |

```python
from keykosh import K2Client, OfflineConfigCache

k2 = K2Client(
    base_url="https://k2.acme.com",
    token="k2_live_xxxxxxxx",
    env="prod",
    offline_cache=OfflineConfigCache(cache_dir="/var/lib/myapp/k2", ttl_millis=6 * 60 * 60 * 1000),
)
```

---

## 6. Reading values (`K2Configuration`)

`get_configuration()` returns a `K2Configuration` — a typed view over the
resolved property map. Accessors return `default` when a key is missing.

| Method | Returns |
|---|---|
| `has(key)` | `bool` — key present |
| `get_string(key, default=None)` | value coerced to `str` |
| `get_int(key, default=None)` | `int`, or `default` if missing/unparsable |
| `get_float(key, default=None)` | `float`, or `default` if missing/unparsable |
| `get_bool(key, default=None)` | `True` only for a real `True` or the string `"true"` (case-insensitive) |
| `to_dict()` | a plain `dict` copy of all properties |
| `.environment` | the environment name |
| `.properties` | the underlying `dict` |

```python
cfg = k2.get_configuration()
if cfg.has("feature.new_ui"):
    enabled = cfg.get_bool("feature.new_ui", False)
timeout = cfg.get_float("http.timeout", 2.5)
all_props = cfg.to_dict()
```

---

## 7. Error handling

Every failure raises a single exception type, `K2Error` (importable from
`keykosh`):

```python
from keykosh import create_client, K2Error

k2 = create_client(base_url="https://k2.acme.com", token="k2_live_xxxx", env="prod")
try:
    cfg = k2.get_configuration()
except K2Error as e:
    print(e)                       # human-readable message
    print(e.status_code)           # HTTP status, or -1 for transport/config errors
    if e.is_availability_error():  # transport failure or 5xx
        ...  # platform down / unreachable — safe to retry or degrade
    else:
        ...  # auth/not-found/host errors — a config or permission problem
```

`K2Error` attributes:

| Attribute / method | Meaning |
|---|---|
| `str(err)` / `err.args[0]` | the message |
| `err.status_code` | HTTP status from the platform, or `-1` for transport/config errors |
| `err.__cause__` | the underlying exception, when chained |
| `err.is_availability_error()` | `True` for `status_code == -1` or `>= 500` — these are eligible for offline-cache fallback |

Status codes you may see:

| Code | Meaning | Availability error? |
|---|---|---|
| `-1` | Transport/config error (bad URL, DNS, connection refused, timeout, JSON parse) | Yes |
| `401` / `403` | Token rejected — wrong token or wrong environment scope | No |
| `404` | Config not found for that env/key | No |
| `421` | Host not licensed — `base_url` host does not match the platform's `K2_PUBLIC_HOST` | No |
| `5xx` | Platform-side error | Yes |

Auth/not-found/host errors (`401/403/404/421`) **always surface** — they are
never masked by the offline cache, because a stale snapshot would hide a real
misconfiguration.

---

## 8. Offline / last-known-good behavior

When `offline_cache` is enabled, a **successful** `get_configuration()` writes
the resolved property map to disk. If a later fetch fails with an
**availability error** (transport failure or 5xx), the SDK serves the
last-known-good snapshot instead of raising — your app keeps its most recent
good config through a platform outage.

At rest each snapshot is:

- **AES-256-GCM encrypted** — secret values are never on disk in plaintext.
- **HMAC-SHA256 sealed** — tampering is detected and the file is refused.
- **TTL-bounded** — a snapshot past `ttl_millis` (default 24 h) is refused as stale.
- **Token-keyed** — both keys are derived from the SDK token, so **rotating the
  token invalidates every existing snapshot**, and a file written under one
  token is unreadable under another.

The on-disk `K2C1` format is **byte-compatible with the Java and Node SDKs** —
a snapshot written by one can be read by the others (given the same token).

Snapshot files live at `<cache_dir>/config-<env>.json.enc`, one per
environment. Writes are atomic (temp file + `os.replace`) and best-effort — a
cache-write failure logs to stderr but never breaks your app.

### Compatibility flag (`offlineCacheAllowed`)

The offline cache is available on **every tier** — current platform versions
send `offlineCacheAllowed: true` on all tiers, including Free. The flag remains
in the config response for compatibility with older platform builds:

- `offlineCacheAllowed: false` (older platform build) → the SDK **skips the
  disk write** even if `offline_cache=True`. Live reads still work; there is
  simply no local snapshot to fall back on.
- flag `true` or absent → the SDK honors your `offline_cache` setting
  (absent = back-compat with older servers).

No client change is needed to move between tiers; the behavior follows the
server signal automatically.

---

## 9. Environment variables & Docker

The SDK reads these environment variables (kwargs always override):

| Var | Meaning | Default |
|---|---|---|
| `K2_BASE_URL` | Platform URL (`https://k2.acme.com`) | — (required unless passed) |
| `K2_TOKEN` | SDK token — the one secret; never commit it | — (required unless passed) |
| `K2_ENV` | Default environment for no-arg reads | — |
| `K2_CACHE_DIR` | Offline cache directory | `~/.k2/cache` |

### Docker / 12-factor

Bootstrap entirely from the environment — no config file needed:

```python
from keykosh import create_client
k2 = create_client()   # reads K2_BASE_URL / K2_TOKEN / K2_ENV
cfg = k2.get_configuration()
```

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir keykosh-sdk
COPY app.py .
# Provide K2_BASE_URL / K2_TOKEN / K2_ENV at runtime (env / secrets), not baked in.
CMD ["python", "app.py"]
```

```bash
docker run --rm \
  -e K2_BASE_URL=https://k2.acme.com \
  -e K2_TOKEN="$K2_TOKEN" \
  -e K2_ENV=prod \
  myapp
```

For the offline cache to survive container restarts, mount a volume and point
`K2_CACHE_DIR` at it (e.g. `-e K2_CACHE_DIR=/data/k2 -v k2cache:/data/k2`).
Inject `K2_TOKEN` from a secret store — never bake it into the image.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `K2Error: K2 base_url is required` | `base_url`/`K2_BASE_URL` empty | Set the platform URL. |
| `K2Error: K2 token is required` | `token`/`K2_TOKEN` empty | Mint a token in the admin UI (Tokens tab). |
| `K2Error: No default environment configured` | No-arg read without `env` | Pass `env=...` or call with an explicit environment. |
| `HTTP 401/403 … token rejected` | Wrong token, or token not scoped to that env | Verify the token and its environment scope. |
| `HTTP 404 … config not found` | Env or key doesn't exist | Check the environment name / property key. |
| `HTTP 421 … rejected the request host` | `base_url` host ≠ platform `K2_PUBLIC_HOST` | Use the licensed public host in `base_url`. |
| `status_code == -1` (transport) | DNS/connection/timeout/JSON error | Check network reachability and `request_timeout`. |
| Offline fallback not kicking in | Cache disabled, an older platform build sending `offlineCacheAllowed: false`, no prior successful fetch, or a non-availability error | Enable `offline_cache`, ensure at least one prior good fetch; auth/404/421 never fall back. |
| `[k2-sdk] offline snapshot … refusing` on stderr | Snapshot tampered, wrong token, past TTL, or truncated | Expected safety behavior — a fresh successful fetch rewrites it. |
| Snapshot not read after token rotation | Snapshots are token-keyed | Expected — a new token invalidates old snapshots; the next good fetch rewrites them. |

### Verifying locally

```bash
python -m pytest        # no-network smoke tests (crypto + parsing + validation)
```
