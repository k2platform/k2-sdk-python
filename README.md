# KeyKosh Python SDK (`keykosh-sdk`)

Read configuration from your **self-hosted KeyKosh (K2) platform**. **Zero runtime
dependencies** — HTTP, crypto and JSON all come from the standard library. Python 3.9+.

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
)

cfg = k2.get_configuration()                       # full config for the default env
db_url = cfg.get_string("db.url", "postgresql://localhost/app")
debug = cfg.get_bool("feature.debug", False)

pool_size = k2.get_property("prod", "pool.size")   # single property

# Live updates: called whenever an admin changes this env's config.
unsubscribe = k2.watch("prod", lambda fresh: apply_config(fresh))
```

## Two ways to run

Everything below follows from which of these you are:

**In production** (the default — `K2_OFFLINE=false`): the platform is the source of truth. The
SDK fetches from it, and writes what it got to `k2config-<env>.json` as a side effect. If the
platform is later unreachable, that file is served instead of raising, so a K2 outage cannot
stop your app from booting. You don't manage the file; the SDK does.

**On a laptop** (`K2_OFFLINE=true`): there is no server at all. You own
`k2config-<env>.json`, the SDK only reads it, and no network call is ever made.

> `K2_OFFLINE=false` does **not** mean "no offline support". It is the mode that *gives* you
> the offline fallback — the SDK keeps the file current for you. `K2_OFFLINE=true` means
> "never contact the server", which is a different thing entirely.

To go from the first to the second, run once online and take ownership of what the SDK wrote:

```bash
K2_BASE_URL=https://k2.acme.com K2_TOKEN=… K2_ENV=dev python app.py   # writes the file
cp ~/.k2/config/k2config-dev.json ./k2config-dev.json                 # repo-local wins
#   edit "_k2": { "managed": false } — the SDK will now never overwrite it
K2_OFFLINE=true python app.py                                          # no server
```

No CLI is needed for any of this: the SDK is the generator.

## What it does

- **Token-scoped reads** against `GET /api/config/token/{env}/current` and
  `/properties/{key}`, authenticated with `Authorization: Bearer <token>` (and `X-API-Token`).
- **Hot reload** (`watch()`) — subscribes to the platform's change stream over Server-Sent
  Events and calls your handler when an admin edits config, with no polling. Runs on a daemon
  thread, reconnects with backoff, and falls back to polling if a proxy strips SSE, so a
  blocked stream degrades instead of failing. Each push also refreshes the local file.
- **TTL cache** (`cache_ttl_seconds`) so repeated reads don't hit the network each call; serves
  the last value through a failed refresh.
- **Local config file** (`k2config-<env>.json`, on every tier) — plaintext, self-describing,
  mode `0600`, written atomically. Auth errors (401/403/404/421) always surface: they are not
  availability blips, so a file on disk is deliberately declined.

### The file

```json
{
  "_k2": { "org": "acme", "app": "billing", "env": "prod",
           "managed": true, "fetchedAt": "2026-08-03T18:04:11Z", "sdk": "python/1.1.0" },
  "properties": { "db.url": "postgresql://localhost:5432/billing", "db.pool": 20 }
}
```

Resolution order, first hit wins: `$K2_CONFIG_FILE` (alone, when set) → `./k2config-<env>.json`
→ `$K2_CONFIG_DIR` or `~/.k2/config`. Naming an explicit location is **exclusive** — the
machine default is not also consulted, so a stale file in your home directory can never quietly
satisfy a read.

The app identity lives *inside* the file rather than in its name, which keeps `K2_APP` optional
and gives you one predictable string to gitignore. Set `K2_APP` anyway: it is what turns
"which app is this?" into a checked invariant, so two apps sharing a config directory fail with
`K2_FILE_APP_MISMATCH` naming both instead of silently serving each other's config.

> **The file holds secret values in clear.** Add `k2config-*.json` to your `.gitignore`.
> Commit one only when it holds no real secrets.

## Errors

Every failure raises `K2Error` with a stable, greppable `code` (plus `status_code`: the HTTP
status, or `-1` for transport/config/file errors). `err.is_availability_error()` is true only
for `K2_UNREACHABLE`, `K2_TIMEOUT` and `K2_SERVER_ERROR` — the codes eligible for the file.

| Code | Means |
|---|---|
| `K2_MISSING_BASE_URL` / `K2_MISSING_TOKEN` / `K2_INVALID_MODE` | misconfiguration — raised at construction, not on first read |
| `K2_MISSING_ENV` | no environment passed and no `K2_ENV` — raised on read, since one client can serve several envs |
| `K2_FILE_NOT_FOUND` | `K2_OFFLINE=true` and no file; the message lists every path searched |
| `K2_FILE_MALFORMED` | the file isn't valid K2 JSON — a bug, not an outage, so no fallback to the server |
| `K2_FILE_APP_MISMATCH` | the file belongs to another app (set `K2_CONFIG_DIR` per app) |
| `K2_FILE_STALE` | older than `K2_OFFLINE_MAX_AGE` |
| `K2_FILE_UNMANAGED` | `_k2.managed` is false — you own it, so the SDK refused to overwrite |
| `K2_FILE_NOT_WRITABLE` | read-only filesystem; logged once at WARN, never raised |
| `K2_UNAUTHORIZED` / `K2_FORBIDDEN` / `K2_NOT_FOUND` / `K2_HOST_NOT_LICENSED` | the platform refused — never served from the file |
| `K2_UNREACHABLE` / `K2_TIMEOUT` / `K2_SERVER_ERROR` | the platform was unreachable — the file is served when present |
| `K2_REQUEST_FAILED` | an unexpected non-2xx; reachable and refusing, so no fallback |

## Configuration via environment

| Var | Meaning |
|---|---|
| `K2_BASE_URL` | platform URL, e.g. `https://k2.acme.com` |
| `K2_TOKEN` | SDK token (the one secret — never commit it) |
| `K2_ENV` | default environment for the no-arg reads |
| `K2_APP` | app slug — optional, but set it: it makes the file's app a checked invariant |
| `K2_OFFLINE` | `true` ⇒ never contact the server (default `false`) |
| `K2_OFFLINE_CACHE` | `false` ⇒ keep nothing on disk (default `true`) |
| `K2_HOT_RELOAD` | `false` ⇒ `watch()` polls instead of subscribing (default: on when online) |
| `K2_CONFIG_DIR` | directory holding `k2config-<env>.json` (default `~/.k2/config`) |
| `K2_CONFIG_FILE` | one exact path — wins over everything else |
| `K2_OFFLINE_MAX_AGE` | e.g. `7d` — hard-refuse a file older than this (default: no limit) |
| `K2_CACHE_TTL` | in-memory TTL, and the `watch()` polling interval when SSE is unavailable |

`K2_OFFLINE=true` with `K2_OFFLINE_CACHE=false` is contradictory and raises `K2_INVALID_MODE`
at construction.

**Deprecated, honored for one minor release** (each logs a WARN): `K2_SOURCE` → `K2_OFFLINE`
(`file`→`true`, `server`→`false`, `auto`→`true` iff a file exists), and `K2_CACHE_DIR` →
`K2_CONFIG_DIR`.

## Test

```bash
python tests/test_smoke.py   # file store, error taxonomy, SSE hot reload — no network needed
```
