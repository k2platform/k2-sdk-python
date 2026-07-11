"""No-network smoke tests: offline-cache crypto, config accessors, client validation.

Run with: python -m pytest  (or: python tests/test_smoke.py)
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from keykosh import (  # noqa: E402
    K2Client, K2ConfigFileSource, K2Configuration, K2Error, OfflineConfigCache,
)


def test_offline_cache_round_trip():
    with tempfile.TemporaryDirectory() as d:
        cache = OfflineConfigCache(cache_dir=d)
        token = "k2_live_abc123"
        props = {"db.url": "jdbc:postgresql://db/app", "feature.x": True, "pool.size": 10}
        cache.save("prod", token, props)
        assert cache.load("prod", token) == props

        raw = open(os.path.join(d, "config-prod.json.enc"), "rb").read()
        assert b"jdbc:postgresql" not in raw  # encrypted at rest
        assert raw[:4] == b"K2C1"  # framing


def test_wrong_token_refused():
    with tempfile.TemporaryDirectory() as d:
        cache = OfflineConfigCache(cache_dir=d)
        cache.save("stg", "k2_live_one", {"a": 1})
        assert cache.load("stg", "k2_live_TWO") is None


def test_ttl_expiry_refused():
    with tempfile.TemporaryDirectory() as d:
        cache = OfflineConfigCache(cache_dir=d, ttl_millis=1)
        cache.save("ttl", "k2_live_x", {"a": 1})
        time.sleep(0.01)
        assert cache.load("ttl", "k2_live_x") is None


def test_tamper_refused():
    with tempfile.TemporaryDirectory() as d:
        cache = OfflineConfigCache(cache_dir=d)
        cache.save("tam", "k2_live_x", {"a": "secret-value"})
        path = os.path.join(d, "config-tam.json.enc")
        raw = bytearray(open(path, "rb").read())
        raw[-1] ^= 0xFF  # flip a byte in the HMAC
        open(path, "wb").write(raw)
        assert cache.load("tam", "k2_live_x") is None


def test_configuration_accessors():
    cfg = K2Configuration.from_response(
        {"environment": "prod", "properties": {"name": "svc", "debug": "true", "max": "50"}}
    )
    assert cfg.get_string("name") == "svc"
    assert cfg.get_bool("debug") is True
    assert cfg.get_int("max") == 50
    assert cfg.get_string("missing", "def") == "def"


def test_client_validation():
    try:
        K2Client(base_url="", token="t")
        assert False, "missing base_url must raise"
    except K2Error:
        pass
    # Missing token no longer raises at construction (file source / K2_TOKEN_ENC may satisfy it).
    c = K2Client(base_url="http://localhost:8080/", token="t", env="prod")
    assert c.base_url == "http://localhost:8080"  # trailing slash stripped


def _write_doc(d):
    with open(os.path.join(d, "test-app.k2.json"), "w") as f:
        json.dump({
            "org": "acme", "app": "test-app",
            "environments": {"dev": {"app.name": "test11", "app.status": 4}, "prod": {"app.name": "prod-11"}},
        }, f)


def test_file_source():
    with tempfile.TemporaryDirectory() as d:
        _write_doc(d)
        fs = K2ConfigFileSource(dir=d)
        assert fs.has_file_for("test-app") is True
        assert fs.load("acme", "test-app", "dev") == {"app.name": "test11", "app.status": 4}
        assert fs.load("acme", "test-app", "staging") is None
        try:
            fs.load("wrongco", "test-app", "dev")
            assert False, "org mismatch must raise"
        except K2Error:
            pass


def test_file_mode_offline():
    with tempfile.TemporaryDirectory() as d:
        _write_doc(d)
        client = K2Client(source="file", org="acme", app="test-app", config_dir=d)
        cfg = client.get_configuration("dev")
        assert cfg.get_string("app.name") == "test11"
        assert cfg.organization == "acme"


class _CaptureHandler(BaseHTTPRequestHandler):
    captured = {}

    def do_GET(self):  # noqa: N802
        _CaptureHandler.captured["path"] = self.path
        _CaptureHandler.captured["auth"] = self.headers.get("Authorization")
        body = json.dumps({"organization": "acme", "application": "billing",
                           "environment": "prod", "properties": {"k": "v"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_org_app_path_and_token_enc():
    server, base = _serve()
    try:
        # app set ⇒ client-named-app contract (§2b)
        K2Client(base_url=base, token="t", app="billing", source="server").get_configuration("prod")
        assert _CaptureHandler.captured["path"] == "/api/config/token/billing/prod/current"

        # K2_TOKEN_ENC decrypted via injected decryptor and sent as Bearer
        K2Client(base_url=base, source="server",
                 token_enc="Y2lwaGVy", token_decryptor=lambda ct: "tok_decrypted.secret").get_configuration("prod")
        assert _CaptureHandler.captured["auth"] == "Bearer tok_decrypted.secret"
    finally:
        server.shutdown()


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
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_sts_posts_signed_envelope():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        signer = lambda: {  # noqa: E731 — a fake signer, no real AWS
            "url": "https://sts.us-east-1.amazonaws.com/",
            "headers": {"Authorization": ["AWS4-HMAC-SHA256 ..."]},
            "body": "Action=GetCallerIdentity&Version=2011-06-15",
        }
        client = K2Client(base_url=base, app="billing", sts_signer=signer)
        cfg = client.get_configuration("prod")
        assert cfg.get_string("db") == "x"
        assert _StsHandler.captured["path"] == "/api/config/sts/billing/prod/current"
        assert "GetCallerIdentity" in _StsHandler.captured["body"]

        # STS without an app fails fast
        try:
            K2Client(base_url=base, sts_signer=signer).get_configuration("prod")
            assert False, "STS without app must raise"
        except K2Error:
            pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"  ✓ {name}")
    print(f"\n{n} checks passed")
