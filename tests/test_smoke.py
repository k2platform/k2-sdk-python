"""No-network smoke tests: the local config file, flags, error codes, and hot reload.

Run with: python -m pytest  (or: python tests/test_smoke.py)
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from keykosh import (  # noqa: E402
    K2Client, K2ConfigStore, K2Configuration, K2Error, K2ErrorCode,
)

QUIET = lambda _line: None  # noqa: E731 — keep the test output clean


def _raises(fn) -> K2Error:
    """Run ``fn``, require a K2Error, and return it."""
    try:
        fn()
    except K2Error as e:
        return e
    raise AssertionError("expected a K2Error, got none")


# --- the local config file ---------------------------------------------------------------


def test_store_round_trip_is_plaintext_and_self_describing():
    with tempfile.TemporaryDirectory() as d:
        store = K2ConfigStore(dir=d)
        props = {"db.url": "jdbc:postgresql://db/app", "feature.x": True, "pool.size": 10}
        written = store.save("prod", props, org="acme", app="billing")
        assert str(written) == os.path.join(d, "k2config-prod.json")

        doc = json.loads(open(written).read())
        assert doc["properties"] == props
        assert doc["_k2"]["app"] == "billing"
        assert doc["_k2"]["org"] == "acme"
        assert doc["_k2"]["env"] == "prod"
        assert doc["_k2"]["managed"] is True, "SDK-written files are managed"
        assert doc["_k2"]["fetchedAt"]

        assert store.load("prod", app="billing").properties == props
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(written).st_mode) == 0o600


def test_resolution_order_and_explicit_locations_are_exclusive():
    # Nothing configured: repo-local, then the machine default.
    implicit = [str(p) for p in K2ConfigStore().candidates("dev")]
    assert implicit[0] == os.path.join(os.getcwd(), "k2config-dev.json"), "repo-local first"
    assert implicit[1].endswith(os.path.join(".k2", "config", "k2config-dev.json"))
    assert len(implicit) == 2

    with tempfile.TemporaryDirectory() as d:
        # An explicit dir REPLACES the machine default rather than preceding it — a stale file in
        # ~/.k2/config must not quietly satisfy a read that should fail loudly.
        order = [str(p) for p in K2ConfigStore(dir=d).candidates("dev")]
        assert order == [os.path.join(os.getcwd(), "k2config-dev.json"),
                         os.path.join(d, "k2config-dev.json")]

        exact = os.path.join(d, "somewhere-else.json")
        assert [str(p) for p in K2ConfigStore(dir=d, file=exact).candidates("dev")] == [exact], \
            "K2_CONFIG_FILE means that file and nothing else"


def test_managed_guard_refuses_to_clobber_hand_edits():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "k2config-dev.json")
        with open(path, "w") as f:
            json.dump({"_k2": {"app": "billing", "env": "dev", "managed": False},
                       "properties": {"hand.edited": "yes"}}, f)
        err = _raises(lambda: K2ConfigStore(dir=d).save("dev", {"server": "wins"}, app="billing"))
        assert err.code == K2ErrorCode.FILE_UNMANAGED
        assert json.loads(open(path).read())["properties"] == {"hand.edited": "yes"}


def test_app_mismatch_is_loud_and_names_both_apps():
    with tempfile.TemporaryDirectory() as d:
        store = K2ConfigStore(dir=d)
        store.save("dev", {"a": 1}, app="shipping")
        err = _raises(lambda: store.load("dev", app="billing"))
        assert err.code == K2ErrorCode.FILE_APP_MISMATCH
        assert "shipping" in str(err) and "billing" in str(err)


def test_malformed_file_is_a_bug_not_an_outage():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "k2config-dev.json"), "w") as f:
            f.write("{ not json")
        assert _raises(lambda: K2ConfigStore(dir=d).load("dev")).code == K2ErrorCode.FILE_MALFORMED

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "k2config-dev.json"), "w") as f:
            json.dump({"_k2": {"app": "x"}}, f)
        assert _raises(lambda: K2ConfigStore(dir=d).load("dev")).code == K2ErrorCode.FILE_MALFORMED


def test_offline_max_age():
    with tempfile.TemporaryDirectory() as d:
        old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(d, "k2config-dev.json"), "w") as f:
            json.dump({"_k2": {"app": "billing", "env": "dev", "managed": True, "fetchedAt": old},
                       "properties": {"a": 1}}, f)
        err = _raises(lambda: K2ConfigStore(dir=d, max_age_seconds=7 * 86400).load("dev"))
        assert err.code == K2ErrorCode.FILE_STALE
        assert "8 days" in str(err), "the stale message carries the age"
        # No limit configured ⇒ old files still serve; refusing to boot mid-outage is worse.
        assert K2ConfigStore(dir=d).load("dev") is not None


# --- the flags ---------------------------------------------------------------------------


def test_contradictory_flags_fail_at_construction():
    err = _raises(lambda: K2Client(offline=True, offline_cache=False, env="dev"))
    assert err.code == K2ErrorCode.INVALID_MODE


def test_offline_true_needs_no_server_no_url_no_token():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "k2config-dev.json"), "w") as f:
            json.dump({"_k2": {"org": "acme", "app": "billing", "env": "dev", "managed": False},
                       "properties": {"app.name": "local"}}, f)
        cfg = K2Client(offline=True, app="billing", config_dir=d, logger=QUIET).get_configuration("dev")
        assert cfg.get_string("app.name") == "local"
        assert cfg.organization == "acme", "org comes from the file header"


def test_offline_true_without_a_file_lists_where_it_looked():
    with tempfile.TemporaryDirectory() as d:
        err = _raises(lambda: K2Client(offline=True, config_dir=d, logger=QUIET).get_configuration("dev"))
        assert err.code == K2ErrorCode.FILE_NOT_FOUND
        assert "k2config-dev.json" in str(err)
        assert "K2_OFFLINE=false" in str(err), "and how to generate one"


def test_offline_cache_false_keeps_nothing_on_disk():
    client = K2Client(base_url="http://127.0.0.1:1", token="t", offline_cache=False, logger=QUIET)
    assert client.store is None


def test_missing_base_url_and_token_fail_at_construction():
    assert _raises(lambda: K2Client(token="t")).code == K2ErrorCode.MISSING_BASE_URL
    assert _raises(lambda: K2Client(base_url="http://x")).code == K2ErrorCode.MISSING_TOKEN
    assert K2Client(base_url="http://localhost:8080/", token="t").base_url == "http://localhost:8080"


def test_missing_env_raises_its_own_code():
    client = K2Client(base_url="http://127.0.0.1:1", token="t", offline_cache=False, logger=QUIET)
    assert _raises(client.get_configuration).code == K2ErrorCode.MISSING_ENV


# --- server reads, fallback, and error codes ---------------------------------------------


class _Fake(BaseHTTPRequestHandler):
    """A stand-in k2-app: /current, plus an SSE /stream that the test can push to."""

    properties = {"k": "v"}
    status = 200
    captured = {}
    streams = []
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802
        _Fake.captured["path"] = self.path
        _Fake.captured["auth"] = self.headers.get("Authorization")
        if self.path.endswith("/stream"):
            if _Fake.status != 200:
                self.send_response(_Fake.status)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            with _Fake.lock:
                _Fake.streams.append(self.wfile)
            # Hold the connection open until the test tears the server down.
            while not getattr(self.server, "_stopping", False):
                time.sleep(0.02)
            return
        if _Fake.status != 200:
            self.send_response(_Fake.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"nope"}')
            return
        body = json.dumps({"organization": "acme", "application": "billing",
                           "environment": "prod", "properties": _Fake.properties}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass

    @classmethod
    def push(cls, event):
        with cls.lock:
            for w in list(cls.streams):
                try:
                    w.write(f"event: config.changed\ndata: {json.dumps(event)}\n\n".encode())
                    w.flush()
                except OSError:
                    cls.streams.remove(w)


def _serve(status=200, properties=None):
    _Fake.status = status
    _Fake.properties = properties if properties is not None else {"k": "v"}
    _Fake.streams = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    server._stopping = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _shutdown(server):
    server._stopping = True
    server.shutdown()
    server.server_close()


def test_successful_fetch_writes_the_file_and_an_outage_serves_it():
    with tempfile.TemporaryDirectory() as d:
        server, base = _serve(properties={"db.url": "postgres://real", "n": 42})
        try:
            client = K2Client(base_url=base, token="t", app="billing", config_dir=d, logger=QUIET)
            assert client.get_configuration("prod").get_string("db.url") == "postgres://real"
            assert os.path.exists(os.path.join(d, "k2config-prod.json"))
        finally:
            _shutdown(server)

        # Platform genuinely gone — the file is served.
        offline = K2Client(base_url=base, token="t", app="billing", config_dir=d, logger=QUIET)
        assert offline.get_configuration("prod").get_string("db.url") == "postgres://real"


def test_outage_with_an_unusable_file_raises_the_files_own_code():
    """The file exists but belongs to another app.

    The outage path must surface THAT — the named, actionable diagnosis — not swallow it
    into a WARN and report the generic "no offline file exists", which is untrue and is
    exactly the silence K2C1 was replaced to remove.
    """
    with tempfile.TemporaryDirectory() as d:
        K2ConfigStore(dir=d).save("prod", {"db.url": "postgres://real"}, app="billing")
        client = K2Client(base_url="http://127.0.0.1:1", token="t", app="shipping",
                          config_dir=d, logger=QUIET)
        err = _raises(lambda: client.get_configuration("prod"))
        assert err.code == K2ErrorCode.FILE_APP_MISMATCH, \
            "an unusable file must not be downgraded to K2_UNREACHABLE"
        assert "billing" in str(err) and "shipping" in str(err), "name both apps"
        assert "also unreachable" in str(err), "and state that the platform was gone too"


def test_auth_failures_are_never_served_from_the_file():
    for status, code in ((401, K2ErrorCode.UNAUTHORIZED), (403, K2ErrorCode.FORBIDDEN),
                         (404, K2ErrorCode.NOT_FOUND), (421, K2ErrorCode.HOST_NOT_LICENSED)):
        with tempfile.TemporaryDirectory() as d:
            K2ConfigStore(dir=d).save("prod", {"from.file": True}, app="billing")
            server, base = _serve(status=status)
            try:
                client = K2Client(base_url=base, token="t", app="billing", config_dir=d, logger=QUIET)
                err = _raises(lambda: client.get_configuration("prod"))
                assert err.code == code, f"HTTP {status} → {code}"
                assert err.is_availability_error() is False
                assert "NOT used" in str(err), "say the file was deliberately declined"
            finally:
                _shutdown(server)


def test_5xx_and_dead_host_are_availability_errors():
    with tempfile.TemporaryDirectory() as d:
        server, base = _serve(status=503)
        try:
            client = K2Client(base_url=base, token="t", config_dir=d, logger=QUIET)
            err = _raises(lambda: client.get_configuration("prod"))
            assert err.code == K2ErrorCode.SERVER_ERROR
            assert err.is_availability_error() is True
        finally:
            _shutdown(server)

    with tempfile.TemporaryDirectory() as d:
        client = K2Client(base_url="http://127.0.0.1:1", token="t", config_dir=d, logger=QUIET)
        err = _raises(lambda: client.get_configuration("prod"))
        assert err.code == K2ErrorCode.UNREACHABLE
        assert "has no configuration" in str(err)


def test_startup_logging_is_never_silent():
    with tempfile.TemporaryDirectory() as d:
        server, base = _serve(properties={"a": 1, "b": 2})
        lines = []
        try:
            K2Client(base_url=base, token="t", app="billing", config_dir=d,
                     logger=lines.append).get_configuration("prod")
        finally:
            _shutdown(server)
        startup = next(line for line in lines if line.startswith("K2: loaded"))
        assert "2 keys" in startup
        assert "app 'billing'" in startup and "env 'prod'" in startup
        assert "hot reload:" in startup


# --- hot reload --------------------------------------------------------------------------


def test_sse_change_triggers_refetch_and_rewrites_the_file():
    with tempfile.TemporaryDirectory() as d:
        server, base = _serve(properties={"pool.size": 10})
        try:
            client = K2Client(base_url=base, token="t", app="billing", config_dir=d, logger=QUIET)
            client.get_configuration("prod")

            updates = []
            unsubscribe = client.watch("prod", updates.append)
            for _ in range(100):  # wait for the stream to be established
                if _Fake.streams:
                    break
                time.sleep(0.02)
            assert _Fake.streams, "watch() must open a stream"

            _Fake.properties = {"pool.size": 25}
            _Fake.push({"environment": "prod", "changedProperties": ["pool.size"], "action": "UPDATE"})

            for _ in range(200):
                if updates:
                    break
                time.sleep(0.02)
            assert len(updates) == 1, "the handler fires once per event"
            assert updates[0].get_int("pool.size") == 25, "the SDK re-fetched /current"

            on_disk = json.loads(open(os.path.join(d, "k2config-prod.json")).read())
            assert on_disk["properties"]["pool.size"] == 25, "the file is rewritten on push"
            unsubscribe()
        finally:
            _shutdown(server)


def test_blocked_stream_degrades_to_polling_instead_of_failing():
    with tempfile.TemporaryDirectory() as d:
        # A server that answers /current but 404s /stream — the pre-SSE server case.
        class _NoStream(_Fake):
            def do_GET(self):  # noqa: N802
                if self.path.endswith("/stream"):
                    self.send_response(404)
                    self.end_headers()
                    return
                _Fake.do_GET(self)

        _Fake.status = 200
        _Fake.properties = {"a": 1}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _NoStream)
        server._stopping = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        lines = []
        try:
            client = K2Client(base_url=base, token="t", config_dir=d, cache_ttl_seconds=0.05,
                              logger=lines.append)
            assert client.get_configuration("prod").get_int("a") == 1
            unsubscribe = client.watch("prod", lambda _cfg: None)
            for _ in range(100):
                if any("falling back to polling" in line for line in lines):
                    break
                time.sleep(0.02)
            assert any("falling back to polling" in line for line in lines), \
                "an unavailable stream must degrade to polling — never fail the app"
            unsubscribe()
        finally:
            _shutdown(server)


def test_offline_true_never_opens_a_stream():
    with tempfile.TemporaryDirectory() as d:
        K2ConfigStore(dir=d).save("dev", {"a": 1}, app="billing")
        client = K2Client(offline=True, config_dir=d, logger=QUIET)
        unsubscribe = client.watch("dev", lambda _cfg: (_ for _ in ()).throw(AssertionError("must not fire")))
        time.sleep(0.05)
        unsubscribe()


# --- unchanged surface -------------------------------------------------------------------


def test_configuration_accessors():
    cfg = K2Configuration.from_response(
        {"environment": "prod", "properties": {"name": "svc", "debug": "true", "max": "50"}}
    )
    assert cfg.get_string("name") == "svc"
    assert cfg.get_bool("debug") is True
    assert cfg.get_int("max") == 50
    assert cfg.get_string("missing", "def") == "def"


def test_org_app_path_and_token_enc():
    with tempfile.TemporaryDirectory() as d:
        server, base = _serve()
        try:
            K2Client(base_url=base, token="t", app="billing", config_dir=d,
                     logger=QUIET).get_configuration("prod")
            assert _Fake.captured["path"] == "/api/config/token/billing/prod/current"

            K2Client(base_url=base, config_dir=d, logger=QUIET, token_enc="Y2lwaGVy",
                     token_decryptor=lambda ct: "tok_decrypted.secret").get_configuration("prod")
            assert _Fake.captured["auth"] == "Bearer tok_decrypted.secret"
        finally:
            _shutdown(server)


class _StsHandler(BaseHTTPRequestHandler):
    captured = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _StsHandler.captured["path"] = self.path
        _StsHandler.captured["body"] = self.rfile.read(length).decode()
        body = json.dumps({"organization": "acme", "application": "billing",
                           "environment": "prod", "properties": {"db": "x"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_sts_posts_signed_envelope_and_gets_an_offline_file():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    signer = lambda: {  # noqa: E731 — a fake signer, no real AWS
        "url": "https://sts.us-east-1.amazonaws.com/",
        "headers": {"Authorization": ["AWS4-HMAC-SHA256 ..."]},
        "body": "Action=GetCallerIdentity&Version=2011-06-15",
    }
    try:
        with tempfile.TemporaryDirectory() as d:
            client = K2Client(base_url=base, app="billing", sts_signer=signer, config_dir=d, logger=QUIET)
            assert client.get_configuration("prod").get_string("db") == "x"
            assert _StsHandler.captured["path"] == "/api/config/sts/billing/prod/current"
            assert "GetCallerIdentity" in _StsHandler.captured["body"]
            # Unlike K2C1 (token-derived keys), the plaintext file works for STS reads too.
            assert os.path.exists(os.path.join(d, "k2config-prod.json"))

            no_app = K2Client(base_url=base, sts_signer=signer, config_dir=d, logger=QUIET)
            assert "K2_APP" in str(_raises(lambda: no_app.get_configuration("prod")))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ✓ {name}")
    print(f"\n{n} checks passed")
