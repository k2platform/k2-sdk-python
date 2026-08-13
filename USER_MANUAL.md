# KeyKosh Python SDK — User Manual

`keykosh-sdk` reads token-scoped configuration from **your own self-hosted
KeyKosh (K2) platform**. It has **zero runtime dependencies** — HTTP, crypto and
JSON all come from the Python standard library. Python 3.9+.

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
8. [Offline behavior and the local config file](#8-offline-behavior-and-the-local-config-file)
9. [Hot reload](#9-hot-reload)
10. [Environment variables & Docker](#10-environment-variables--docker)
11. [Troubleshooting](#11-troubleshooting)
12. [Cross-language differences](#12-cross-language-differences)

---

## 0. The two ways to run

Almost everything in this manual follows from which of these you are, so start here.

| | **Production** — app + SDK in a container | **Development** — your laptop |
|---|---|---|
| Setting | `K2_OFFLINE=false` (the default) | `K2_OFFLINE=true` |
| Source of truth | the platform | `k2config-<env>.json` |
| Who edits config | the K2 admin UI, only | you, in the file |
| Who writes the file | the **SDK**, on every successful fetch | **you** |
| Network | fetches; falls back to the file when the platform is unreachable | none, ever |

> **`K2_OFFLINE=false` does not mean "no offline support".** It is the setting that *gives*
> you the offline fallback: the SDK keeps a local file current so a K2 outage cannot stop your
> app from booting. `K2_OFFLINE=true` means "never contact the server" — a different thing.

A third combination exists for short-lived jobs that must leave nothing on disk:
`K2_OFFLINE=false` with `K2_OFFLINE_CACHE=false` — the platform is the only source and no file
is written. (`K2_OFFLINE=true` with `K2_OFFLINE_CACHE=false` is contradictory and raises
`K2_INVALID_MODE` at construction.)

---

## 1. Installation

```bash
pip install keykosh-sdk
```

Requires Python 3.9 or newer, and nothing else — the SDK has no runtime
dependencies. (`cryptography` was required before 1.1.0, solely for the retired
encrypted `K2C1` cache format; the local config file is now plaintext JSON.)

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
)

# Full config for the default environment. This also writes the local
# k2config-prod.json that will be served if the platform is later unreachable.
cfg = k2.get_configuration()
db_url  = cfg.get_string("db.url", "postgresql://localhost/app")
debug   = cfg.get_bool("feature.debug", False)
pool    = cfg.get_int("pool.size", 10)

# A single property (explicit environment + key)
pool_size = k2.get_property("prod", "pool.size")

# Live updates — fires whenever an admin changes this environment's config.
unsubscribe = k2.watch("prod", lambda fresh: apply_config(fresh))
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
| `token_file` | `str \| None` | `None` | Path to a file holding the token (Docker/Kubernetes secret mount). Used only when `token` is blank. Env: `K2_TOKEN_FILE`. See [credential resolution](#credential-resolution). |
| `env` | `str \| None` | `None` | Default environment for the no-arg reads. If unset, no-arg reads raise `K2Error`. Env: `K2_ENV`. |
| `request_timeout` | `float` | `10.0` | Per-request HTTP timeout in **seconds**. |
| `cache_ttl_seconds` | `float \| None` | `None` | In-memory TTL (seconds) for `get_cached_configuration()`, and the `watch()` polling interval when the change stream is unavailable. `None`/`<=0` disables the TTL cache. |
| `app` | `str \| None` | `None` | App slug. Optional — the SDK doesn't need it to *find* the local file, only to *validate* it. Set it anyway (see §8). Env: `K2_APP`. |
| `org` | `str \| None` | `None` | Org slug; stamped into the file's `_k2.org`. Env: `K2_ORG`. |
| `offline` | `bool` | `False` | `True` ⇒ never contact the server; the local file is the source of truth. Env: `K2_OFFLINE`. |
| `offline_cache` | `bool` | `True` | Keep a local `k2config-<env>.json` at all. `False` ⇒ nothing is written to disk. Env: `K2_OFFLINE_CACHE`. |
| `hot_reload` | `bool \| None` | `True` when online | Whether `watch()` subscribes to the change stream. `False` ⇒ it polls instead. Env: `K2_HOT_RELOAD`. |
| `config_dir` | `str \| None` | `~/.k2/config` | Directory holding `k2config-<env>.json`. Env: `K2_CONFIG_DIR`. |
| `config_file` | `str \| None` | `None` | One exact path — wins over every other candidate. Env: `K2_CONFIG_FILE`. |
| `offline_max_age` | `str \| float \| None` | `None` | e.g. `"7d"`. Hard-refuse a file older than this. Unset ⇒ no limit. Env: `K2_OFFLINE_MAX_AGE`. |
| `token_enc` | `str \| None` | `None` | KMS-encrypted token ciphertext, decrypted at first use. Env: `K2_TOKEN_ENC`. |
| `sts` | `bool` | `False` | Authenticate with an AWS STS workload identity instead of a token. Org-scoped, so `app` is required. Env: `K2_STS_ENABLED`. |
| `logger` | `Callable[[str], None] \| None` | writes to `stderr` | Where the SDK's INFO/WARN lines go. |

```python
from keykosh import K2Client

k2 = K2Client(
    base_url="https://k2.acme.com",
    token="k2_live_xxxxxxxx",
    env="prod",
    app="billing",
    config_dir="/var/lib/myapp/k2",   # one directory per app
    offline_max_age="7d",             # optional hard staleness limit
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
from keykosh import create_client, K2Error, K2ErrorCode

k2 = create_client(base_url="https://k2.acme.com", token="k2_live_xxxx", env="prod")
try:
    cfg = k2.get_configuration()
except K2Error as e:
    print(e.code)                  # a stable K2ErrorCode — branch on this
    print(e)                       # the message: what was tried, what happened, what to do
    if e.code == K2ErrorCode.MISSING_TOKEN:
        ...
    if e.is_availability_error():  # unreachable / timeout / 5xx
        ...  # platform down — safe to retry or degrade
    else:
        ...  # config / file / auth — a real problem to fix
```

`K2Error` attributes:

| Attribute / method | Meaning |
|---|---|
| `err.code` | a stable `K2ErrorCode` — greppable, and unchanged when messages are reworded. **Branch on this, not on the message.** |
| `str(err)` | the message |
| `err.status_code` | HTTP status from the platform, or `-1` for transport/config/file errors |
| `err.__cause__` | the underlying exception, when chained |
| `err.is_availability_error()` | `True` only for `K2_UNREACHABLE`, `K2_TIMEOUT` and `K2_SERVER_ERROR` — the codes eligible for the local file |

### The codes

**Config** — a misconfiguration. Raised when the client is **constructed**, so a bad
deployment fails immediately rather than an hour later on the first read.

| Code | Meaning |
|---|---|
| `K2_MISSING_BASE_URL` | no `base_url` and no `K2_BASE_URL` (and not `offline`) |
| `K2_MISSING_TOKEN` | no `token`, `token_file`, `token_enc` or `sts` credential |
| `K2_TOKEN_FILE_UNREADABLE` | `token_file` / `K2_TOKEN_FILE` names a file that is missing, unreadable, or empty once stripped. Never downgraded to `K2_MISSING_TOKEN` — the operator named a file, so the file is what the error reports |
| `K2_INVALID_MODE` | `offline=True` with `offline_cache=False` — contradictory |
| `K2_MISSING_ENV` | no environment passed and no `K2_ENV`. **Raised on read, not construction** — one client can legitimately serve several environments via `get_configuration(env)`, so requiring a default up front would break that API. |

**File** — something about the local `k2config-<env>.json`. None of these fall back to the
server: a broken or foreign file is a bug to fix, not an outage to route around.

| Code | Meaning |
|---|---|
| `K2_FILE_NOT_FOUND` | `offline=True` and no file exists; the message lists every path searched |
| `K2_FILE_MALFORMED` | not valid JSON, or no `properties` object |
| `K2_FILE_APP_MISMATCH` | the file's `_k2.app` is a different app — see §8 |
| `K2_FILE_STALE` | older than `offline_max_age` |
| `K2_FILE_UNMANAGED` | `_k2.managed` is not `true`, so you own the file and the SDK refused to overwrite it |
| `K2_FILE_NOT_WRITABLE` | the directory isn't writable. Logged **once** at WARN and suppressed — never raised |

**Auth** — the platform is reachable and refusing. A valid file on disk is deliberately
**declined**, because an auth failure is not an outage; the message says so.

| Code | `status_code` | Meaning |
|---|---|---|
| `K2_UNAUTHORIZED` | 401 | token rejected — bad or expired |
| `K2_FORBIDDEN` | 403 | token has no access to that environment |
| `K2_NOT_FOUND` | 404 | no config for that environment/key |
| `K2_HOST_NOT_LICENSED` | 421 | `base_url` host doesn't match the platform's licensed host (`K2_PUBLIC_HOST`) |

**Other** — reachable and refusing, for a reason none of the above names.

| Code | `status_code` | Meaning |
|---|---|---|
| `K2_REQUEST_FAILED` | an unexpected non-2xx (a 400 or 429, say) | the residual bucket. Like the auth codes it never serves the local file; **unlike** them it says nothing about your credential, so don't treat it as a token problem |

**Availability** — the platform could not be reached. These, and only these, serve the local
file when one is present.

| Code | `status_code` | Meaning |
|---|---|---|
| `K2_UNREACHABLE` | `-1` | DNS failure, connection refused, bad `base_url` |
| `K2_TIMEOUT` | `-1` | exceeded `request_timeout` |
| `K2_SERVER_ERROR` | 5xx | platform-side failure, or an unparseable response |

If the platform is unreachable **and** a file exists but cannot be used, you get the *file's*
code (`K2_FILE_APP_MISMATCH`, `K2_FILE_STALE`, …) with the outage noted in the message — the
specific diagnosis, not a generic "unreachable".

---

## 8. Offline behavior and the local config file

One plaintext file per environment, serving both personas from §0:

```json
{
  "_k2": {
    "org": "acme",
    "app": "billing",
    "env": "prod",
    "managed": true,
    "fetchedAt": "2026-08-03T18:04:11Z",
    "sdk": "python/1.2.0"
  },
  "properties": {
    "db.url": "postgresql://localhost:5432/billing",
    "db.pool": 20,
    "feature.x": true
  }
}
```

`properties` is nested under its own key so a config key literally named `_k2` can't collide
with the header. Files are written mode `0600` in a `0700` directory, via temp file +
`os.replace`, so a concurrent reader sees the old file or the new one — never a partial one.

### Where it lives

First hit wins:

```
$K2_CONFIG_FILE                     exact path — and nothing else is consulted
./k2config-<env>.json               repo-local — the dev case
$K2_CONFIG_DIR/k2config-<env>.json  or, when unset, ~/.k2/config/k2config-<env>.json
```

**Naming an explicit location is exclusive of the machine default — not of the working
directory.** `K2_CONFIG_FILE` means *that* file and nothing else. `K2_CONFIG_DIR` **replaces**
`~/.k2/config` rather than preceding it, so a stale file in a home directory can never quietly
satisfy a read that should have failed loudly — but it does **not** suppress
`./k2config-<env>.json`, which is always searched first. Only `K2_CONFIG_FILE` does that. The
repo-local candidate is kept deliberately: a repo is per-app, so it can't hold a foreign app's
file. If your process's working directory might contain a `k2config-<env>.json` you don't want
used, point `K2_CONFIG_FILE` at the exact file rather than setting a directory.

All three K2 SDKs order these candidates identically.

The SDK never *creates* a repo-local file — that is your deliberate `cp` (see below).

### Why the app name is inside the file, not in its name

- **`K2_APP` stays optional** — the SDK doesn't need it to *locate* the file, only to
  *validate* it.
- **One predictable string** to document and gitignore: `k2config-*.json`.
- **A collision becomes detectable.** Two apps sharing a config directory both write
  `k2config-prod.json`; the second overwrites the first. The loser then reads a file whose
  `_k2.app` doesn't match and fails with `K2_FILE_APP_MISMATCH` **naming both apps and the
  fix**, instead of silently serving the wrong app's config.

> The collision is *diagnosable*, not *eliminated* — the filename still has no app segment.
> The fix is one `K2_CONFIG_DIR` per app, or a repo-local file per repo.

**Set `K2_APP` even though it is optional.** With `K2_OFFLINE=true` and no `K2_APP` the SDK
has nothing to validate against — it never contacts the server, so it cannot know which app it
*should* be, and will trust whatever the file says. Setting `K2_APP` turns that into a checked
invariant from the very first cold read. (When online, the SDK remembers the app the server
resolved and validates against that.)

### Secrets

**The file is plaintext, and on the token read path it holds secret values in clear.** This is
deliberate: production's file is machine-written and never opened, and a developer's holds test
values. It also means an AWS STS workload identity — which has no static token — can keep an
offline file, which the old token-derived encryption made impossible.

> **Add `k2config-*.json` to your `.gitignore`.** Commit one only when it holds no real
> secrets.

### Staleness

There is **no hard TTL by default**. Refusing to boot during an outage is a worse failure than
booting slightly stale config, especially now that hot reload keeps the file current. Every
file-served read logs a WARN carrying the file's age. If you want a hard limit, set
`offline_max_age` / `K2_OFFLINE_MAX_AGE` (e.g. `"7d"`) and an older file is refused with
`K2_FILE_STALE`.

### Read-only filesystems

`readOnlyRootFilesystem: true` is common in hardened Docker and Kubernetes, and `~/.k2/config`
will not be writable there. A failed write is logged **once** at WARN as `K2_FILE_NOT_WRITABLE`
— naming the path and suggesting a mounted volume or `K2_OFFLINE_CACHE=false` — and then
suppressed. It is never retried per-fetch and never raised. The app runs normally with no
offline fallback, which is exactly what the operator chose.

### Taking ownership of a file (the dev workflow)

No CLI is involved: the SDK is the generator.

```bash
# 1. Run once online — the SDK writes the file as a side effect of the first fetch.
K2_BASE_URL=https://k2.acme.com K2_TOKEN=… K2_ENV=dev python app.py

# 2. Move it into the repo (repo-local wins the resolution order).
cp ~/.k2/config/k2config-dev.json ./k2config-dev.json
#    then edit "_k2": { "managed": false } and change values freely

# 3. From now on, no server.
K2_OFFLINE=true python app.py
```

`_k2.managed` is the ownership guard. It is present only on SDK-written files; set it to
`false` and the SDK **refuses to write** that file, raising `K2_FILE_UNMANAGED` instead. That
is what makes "my hand-edits vanished" impossible.

### Using `K2ConfigStore` directly

The file store is exported if you need to inspect or pre-seed a file yourself:

```python
from keykosh import K2ConfigStore

store = K2ConfigStore(dir="/etc/myapp")
store.candidates("prod")                      # every path searched, in order
store.resolve("prod")                         # the one that exists, or None
hit = store.load("prod", app="billing")       # raises K2_FILE_* on a bad file
store.save("prod", {"db.url": "…"}, app="billing")
```

---

## 9. Hot reload

`watch(environment=None, handler=…)` calls `handler(config)` whenever the platform reports a
change to that environment, and returns an unsubscribe callable.

```python
k2 = create_client(base_url="https://k2.acme.com", token=os.environ["K2_TOKEN"])

def on_change(cfg):
    pool.resize(cfg.get_int("pool.size", 10))
    flags.replace(cfg.to_dict())

unsubscribe = k2.watch("prod", on_change)
...
unsubscribe()
```

The subscription runs on a **daemon thread**, so it never keeps your process alive on its own.

**Transport is Server-Sent Events** over `urllib` — no new dependency, and it survives ALBs and
proxies that would block a WebSocket. The stream carries a **signal, not values**: on
`config.changed` the SDK re-fetches `/current`. That keeps secrets off a long-lived connection,
exercises the authorization path on every update, and means a client that missed events while
disconnected self-heals on reconnect with a full fetch.

Each push also **rewrites the local config file**, so the snapshot you would fall back to
during an outage is the one hot reload last delivered.

**It degrades rather than fails.** A dropped connection reconnects with backoff. If the stream
is unavailable entirely — an older platform that has no `/stream` endpoint, or a proxy that
strips SSE — `watch()` logs that once and **falls back to polling** on `cache_ttl_seconds`.
Your app still gets updates; they just arrive on the poll interval. Pass `hot_reload=False` to
choose polling outright.

With `offline=True` no stream is opened at all: there is no server to subscribe to.

> **Server requirement:** the change stream needs a platform built on or after 2026-08-03.
> Against an older one the endpoint 404s and the SDK polls — no error, no upgrade required.

---

## 10. Environment variables & Docker

The SDK reads these environment variables (kwargs always override):

| Var | Meaning | Default |
|---|---|---|
| `K2_BASE_URL` | Platform URL (`https://k2.acme.com`) | — (required unless passed) |
| `K2_TOKEN` | SDK token — the one secret; never commit it | — (required unless passed) |
| `K2_TOKEN_FILE` | Path to a file holding the token — the Docker/Kubernetes secret-mount shape. **Since 1.2.0** | — |
| `K2_TOKEN_ENC` | KMS-encrypted token ciphertext, decrypted at first use | — |
| `K2_ENV` | Default environment for no-arg reads | — |
| `K2_ORG` / `K2_APP` | Org and app slugs | — |
| `K2_OFFLINE` | `true` ⇒ never contact the server | `false` |
| `K2_OFFLINE_CACHE` | `false` ⇒ keep nothing on disk | `true` |
| `K2_HOT_RELOAD` | `false` ⇒ `watch()` polls instead of subscribing | on when online |
| `K2_CONFIG_DIR` | Directory holding `k2config-<env>.json` | `~/.k2/config` |
| `K2_CONFIG_FILE` | One exact path — wins over everything else | — |
| `K2_OFFLINE_MAX_AGE` | e.g. `7d` — hard-refuse an older file | no limit |
| `K2_CACHE_TTL` | In-memory TTL / `watch()` poll interval | — |
| `K2_STS_ENABLED` | `true` ⇒ authenticate with an AWS STS workload identity | `false` |

**Deprecated, honored for one minor release** (each logs a WARN on use):

| Old | New | Mapping |
|---|---|---|
| `K2_SOURCE` | `K2_OFFLINE` | `file`→`true`, `server`→`false`, `auto`→`true` iff a file exists for the env |
| `K2_CACHE_DIR` | `K2_CONFIG_DIR` | direct |

### Credential resolution

The first of these that is set wins:

1. `token=` passed to `K2Client` / `create_client` / `from_env`;
2. `K2_TOKEN`;
3. `K2_TOKEN_FILE` — **since 1.2.0**;
4. `K2_TOKEN_ENC`.

(`sts=` / `K2_STS_ENABLED` is a different mechanism — an AWS workload identity instead of a
static token — and is not part of this chain.)

`K2_TOKEN_FILE` exists for mounted secrets: Docker `secrets:`, a Kubernetes `Secret` mounted as a
volume, or a Vault agent template. The file's contents are **stripped** of leading and trailing
whitespace, so the trailing newline such a mount almost always carries is harmless. It is read
**once**, when the client resolves its token — not per request — so rotating the file needs a
restart or a fresh client.

A path that is **missing, unreadable, or empty after stripping** raises
`K2_TOKEN_FILE_UNREADABLE` naming the path. It never falls through silently to "no token": that
would surface as `K2_MISSING_TOKEN` and point you at `K2_TOKEN`, which was never the problem.

> On `keykosh-sdk` **1.1.x and earlier the variable is ignored entirely**, so an app relying on it
> fails at construction with `K2_MISSING_TOKEN`. The Java and Node SDKs honour it from
> `k2-sdk-java` 1.1.1 and `@keykosh/sdk` 1.2.0 respectively.

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
  -e K2_APP=billing \
  -e K2_CONFIG_DIR=/var/lib/k2 \
  -v k2config:/var/lib/k2 \
  myapp
```

Notes:

- **Never bake the token into the image** — inject `K2_TOKEN` at runtime (env, Docker secret,
  or your orchestrator's secret store). To keep it out of the process environment entirely,
  mount it as a file and set `K2_TOKEN_FILE=/run/secrets/k2_token` instead (**1.2.0+**).
- To make the offline file survive container restarts, mount a **volume** at
  `K2_CONFIG_DIR`. Without a persistent mount, each new container starts with no
  last-known-good config and a platform outage at boot has nothing to fall back to.
- **One `K2_CONFIG_DIR` per app.** If two apps share a mounted volume they will overwrite
  each other's `k2config-<env>.json`; with `K2_APP` set, the loser fails loudly with
  `K2_FILE_APP_MISMATCH` rather than serving the wrong config.
- If the container runs with a **read-only root filesystem**, either mount a writable volume
  at `K2_CONFIG_DIR` or set `K2_OFFLINE_CACHE=false` to opt out and silence the warning.

---

## 11. Troubleshooting

Every row is keyed by the error `code`, which is stable — match on that rather than on message
text.

| Code / symptom | Likely cause | Fix |
|---|---|---|
| `K2_MISSING_BASE_URL` at construction | `base_url`/`K2_BASE_URL` empty | Set the platform URL. (Not needed with `offline=True`.) |
| `K2_MISSING_TOKEN` at construction | `token`/`K2_TOKEN` empty | Mint a token in the admin UI → Tokens. If `K2_TOKEN_FILE` *is* set, check the SDK version — 1.1.x ignores it. |
| `K2_TOKEN_FILE_UNREADABLE` at construction | The path doesn't exist (Secret not mounted, or mounted elsewhere), the process can't read it (permissions / `runAsUser`), or it is blank | The error names the path — check the mount, the file mode, and that the Secret key isn't empty. |
| `K2_INVALID_MODE` at construction | `offline=True` with `offline_cache=False` | Contradictory — offline mode *needs* a file. Drop one of the two. |
| `K2_MISSING_ENV` on a read | No-arg read without `env` | Pass `env=…`, or call with an explicit environment. |
| `K2_UNAUTHORIZED` / `K2_FORBIDDEN` | Wrong token, or the token isn't scoped to that env | Verify the token and its environment scope. |
| `K2_NOT_FOUND` | Env or key doesn't exist | Check the environment name / property key. |
| `K2_HOST_NOT_LICENSED` | `base_url` host ≠ the platform's `K2_PUBLIC_HOST` | Use the licensed public host in `base_url`. |
| `K2_TIMEOUT` / `K2_UNREACHABLE` | DNS/connection failure, or slower than `request_timeout` | Check network reachability; raise `request_timeout`. With a local file present these do not raise at all. |
| The local file was never used during an outage | No prior successful fetch, `offline_cache=False`, or the failure wasn't an availability error | Confirm one successful fetch wrote the file; remember auth failures deliberately never fall back. |
| `K2_FILE_NOT_FOUND` with `offline=True` | No file at any candidate path — the message lists them all | Run once with `K2_OFFLINE=false` to have the SDK write it (§8). |
| `K2_FILE_APP_MISMATCH` | Two apps sharing one config directory; the other wrote last | Give each app its own `K2_CONFIG_DIR`, or keep the file in each repo. |
| `K2_FILE_UNMANAGED` on write | The file's `_k2.managed` is not `true`, so you own it | Intended — it protects your hand-edits. Set `K2_OFFLINE=true` to read-only it, or set `managed` back to `true`. |
| `K2_FILE_STALE` | Older than `offline_max_age` | Refresh it with one online run, or raise/unset the limit. |
| `K2_FILE_NOT_WRITABLE` in the log (once) | Read-only filesystem or wrong permissions | Mount a writable volume at `K2_CONFIG_DIR`, or set `K2_OFFLINE_CACHE=false`. |
| `hot reload … falling back to polling` in the log | The platform predates the change stream, or a proxy strips SSE | Harmless — updates arrive on `cache_ttl_seconds` instead. |

### Inspecting the file

It is plain JSON — read it directly:

```bash
cat ~/.k2/config/k2config-prod.json | jq '._k2'   # which app/env/when, and managed
```

Remember it holds secret values in clear on the token read path: keep it gitignored
(`k2config-*.json`) and treat it like any other credential-bearing file.

### Verifying locally

```bash
python tests/test_smoke.py   # no-network smoke tests (file store, errors, SSE)
```

---

## 12. Cross-language differences

The three K2 SDKs — `keykosh-sdk` (Python), `@keykosh/sdk` (Node) and
`com.k2platform:k2-sdk-java` — share one contract: the same environment variables, the same
credential precedence, the same error **codes**, the same local file format and resolution order,
and the same read endpoints. Env-var bootstrap is shared too: `create_client()` /
`K2Client.from_env()` here, `createClient()` in Node, and `K2Client.fromEnv()` in Java (since
1.1.1) read the same variable set. Five differences are deliberate and are not going to be
reconciled, so check these before porting a snippet between languages.

| | Python | Node | Java |
|---|---|---|---|
| Exception type | `K2Error` | `K2Error` | **`K2Exception`** |
| Error-code classes | prose (§7) | prose | `K2ErrorCode.Kind` enum |
| `snapshot(env)` — raw property map | — | — | ✅ |
| `get_offline_cache_allowed()` | — | — | ✅ (`getOfflineCacheAllowed()`) |
| Deprecated `K2_SOURCE` / `K2_CACHE_DIR` aliases | honoured (WARN) | honoured (WARN) | **not honoured** by `fromEnv()` |
| Runtime dependencies | none | none | Jackson |

- **Catch `K2Error` here, `K2Exception` on Java.** Renaming either would be a breaking change for
  a cosmetic gain, so any cross-language instruction to "catch `K2Error`" is wrong in one of the
  three. The `code` values are identical, so a runbook keyed on codes travels unchanged.
- **There is no `Kind` enum in this SDK.** Java exposes `K2ErrorCode.kind()`
  (`CONFIG`/`FILE`/`AUTH`/`AVAILABILITY`/`OTHER`); here the same grouping is documented in §7 and
  the only programmatic split is `is_availability_error()` — the one that decides whether the
  local file may be served. Branch on the code otherwise.
- **`snapshot(env)` and `getOfflineCacheAllowed()` are Java-only.** `snapshot` returns the raw
  property map that Java's Spring `PropertySource` layers in; use `cfg.to_dict()` for the
  equivalent here. `getOfflineCacheAllowed()` reads a **vestigial** server response field (always
  `true`; no SDK acts on it, and the offline file is available on every licence tier) — this
  SDK's `K2Configuration` has no such attribute.
- **This SDK honours the deprecated `K2_SOURCE` / `K2_CACHE_DIR` aliases; Java's `fromEnv()` does
  not.** They still work here (with a WARN) for one more minor release. A container spec that
  relies on them will not configure the Java SDK — move to `K2_OFFLINE` and `K2_CONFIG_DIR`, which
  all three read.
- **"Zero-dependency" describes this SDK and the Node one.** Java's core needs Jackson, so don't
  carry the phrase across.
