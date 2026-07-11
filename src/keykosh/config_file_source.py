"""Human-authored, offline-first local config source (design §3b).

A plaintext JSON file the customer places on the box — read directly, no server contact. Distinct
from the managed encrypted last-known-good cache (§3a): that is SDK-written and token-sealed; this
is operator-written and read first (per ``source``).

Layout — one file per app, per-env inside::

    <config_dir>/<app>.k2.json
    { "org": "acme", "app": "test-app",
      "environments": { "dev": {"app.name": "x"}, "prod": {...} } }
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .errors import K2Error

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class K2ConfigFileSource:
    def __init__(self, dir: Optional[str] = None, file: Optional[str] = None) -> None:
        self.config_dir = dir.strip() if dir and dir.strip() else None
        self.config_file = file.strip() if file and file.strip() else None

    def file_for(self, app: Optional[str]) -> Optional[Path]:
        """The file that would be read for ``app`` (or the explicit config_file)."""
        if self.config_file:
            return Path(self.config_file)
        if not app or not str(app).strip():
            return None
        base = Path(self.config_dir) if self.config_dir else self.default_dir()
        safe = _SAFE.sub("_", str(app).strip())
        return base / f"{safe}.k2.json"

    def has_file_for(self, app: Optional[str]) -> bool:
        """Whether a config file for ``app`` exists (used by source=auto)."""
        f = self.file_for(app)
        return f is not None and f.is_file()

    def load(self, expected_org: Optional[str], expected_app: Optional[str],
             env: str) -> Optional[dict[str, Any]]:
        """Properties for (org, app, env), or ``None`` when the file/env-block is absent.

        Raises ``K2Error`` when the file is malformed or its org/app disagree with the configured
        ``expected_org``/``expected_app`` (a loud mismatch beats silently wrong config).
        """
        f = self.file_for(expected_app)
        if f is None:
            raise K2Error(
                "K2 file source needs an app name — set K2_APP (the k2 app slug), "
                "or point K2_CONFIG_FILE at one file"
            )
        if not f.is_file():
            return None
        try:
            doc = json.loads(f.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise K2Error(f"K2 config file '{f}' could not be parsed: {e}") from e
        if not isinstance(doc, dict):
            return None
        _require_match("org", doc.get("org"), expected_org, f)
        _require_match("app", doc.get("app"), expected_app, f)
        envs = doc.get("environments")
        if not isinstance(envs, dict):
            return None
        return envs.get(env)

    @staticmethod
    def default_dir() -> Path:
        """OS-default config dir: K2_CONFIG_DIR if set, else $XDG_CONFIG_HOME/k2/config or
        ~/.k2/config on POSIX, and %APPDATA%\\k2\\config on Windows."""
        override = os.environ.get("K2_CONFIG_DIR")
        if override and override.strip():
            return Path(override.strip())
        if os.name == "nt" and os.environ.get("APPDATA"):
            return Path(os.environ["APPDATA"]) / "k2" / "config"
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "k2" / "config"
        return Path.home() / ".k2" / "config"


def _require_match(field: str, actual: Any, expected: Optional[str], f: Path) -> None:
    if (expected and str(expected).strip() and actual and str(actual).strip()
            and str(actual).strip().lower() != str(expected).strip().lower()):
        raise K2Error(
            f"K2 config file '{f}' {field}='{actual}' does not match the configured "
            f"{field}='{expected}' — check K2_{field.upper()} (the k2 slug, not the repo name)"
        )
