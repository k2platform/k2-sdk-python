"""The local config file — ``k2config-<env>.json`` (OFFLINE_AND_HOTRELOAD_DESIGN.md §2)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import K2Error, K2ErrorCode

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


@dataclass
class K2FileHit:
    """A successfully read local config file."""

    properties: dict[str, Any]
    header: dict[str, Any]
    path: Path
    age_seconds: Optional[float]


class K2ConfigStore:
    """One plaintext, self-describing file per environment, serving both personas.

    - **prod** (``offline=False``, the default): the SDK writes it on every successful fetch
      and reads it only when the platform is unreachable. The server is the source of truth.
    - **dev** (``offline=True``): the developer owns the file and the SDK never contacts a
      server. Flip ``_k2.managed`` to ``false`` and the SDK refuses to overwrite it.

    Resolution order, first hit wins::

        $K2_CONFIG_FILE                     exact path — alone when set
        ./k2config-<env>.json               repo-local — the dev case
        $K2_CONFIG_DIR/k2config-<env>.json  or, when unset, ~/.k2/config/k2config-<env>.json

    The app identity lives *inside* the file (``_k2.app``), not in its name — so ``K2_APP``
    stays optional (it is needed to *validate*, not to *locate*), the filename is one
    predictable string to gitignore, and two apps sharing a directory fail loudly with
    ``K2_FILE_APP_MISMATCH`` instead of silently serving each other's config.

    Mechanics: file mode ``0600``, directory ``0700``, atomic write (temp + rename) so a
    concurrent reader sees the old or the new file, never a partial one.
    """

    def __init__(
        self,
        dir: Optional[str] = None,
        file: Optional[str] = None,
        max_age_seconds: Optional[float] = None,
        sdk_version: str = "python",
    ) -> None:
        self.config_dir = _trimmed(dir)
        self.config_file = _trimmed(file)
        self.max_age_seconds = max_age_seconds
        self.sdk_version = sdk_version
        self._warned_not_writable = False

    # ---- locating ----

    def candidates(self, environment: str) -> list[Path]:
        """Candidate paths for ``environment``, in resolution order.

        **An explicit location is exclusive.** ``K2_CONFIG_FILE`` means *that* file and nothing
        else; ``K2_CONFIG_DIR`` means that directory (plus the repo-local override, which is
        per-repo so it can never be another app's file). Only when neither is set does the
        machine default ``~/.k2/config`` come into play. Falling back to a directory the operator
        did not name is exactly the silent surprise this format exists to remove: a stale file
        left in a home directory would quietly satisfy a read that should have failed loudly.
        """
        name = file_name(environment)
        if self.config_file:
            return [Path(self.config_file)]
        out = [Path.cwd() / name, Path(self.config_dir or default_dir()) / name]
        seen: set[Path] = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def resolve(self, environment: str) -> Optional[Path]:
        """The first candidate that exists, or ``None``."""
        return next((p for p in self.candidates(environment) if p.is_file()), None)

    def write_target(self, environment: str) -> Path:
        """Where a write goes.

        The file that already resolves (so a repo-local file is updated in place rather than
        shadowed by a stale one under ``~/.k2/config``), else the explicit ``K2_CONFIG_FILE`` /
        ``K2_CONFIG_DIR``, else the machine default. The SDK never *creates* a repo-local file —
        that is the developer's deliberate ``cp``.
        """
        existing = self.resolve(environment)
        if existing is not None:
            return existing
        if self.config_file:
            return Path(self.config_file)
        base = Path(self.config_dir) if self.config_dir else default_dir()
        return base / file_name(environment)

    # ---- reading ----

    def load(
        self,
        environment: str,
        app: Optional[str] = None,
        required: bool = False,
    ) -> Optional[K2FileHit]:
        """Read the file for ``environment``.

        ``app`` (when set) is validated against ``_k2.app``; ``required`` is the
        ``offline=True`` case, where a missing file is a hard error.
        """
        path = self.resolve(environment)
        if path is None:
            if not required:
                return None
            looked = "\n".join(f"  {p}" for p in self.candidates(environment))
            raise K2Error(
                K2ErrorCode.FILE_NOT_FOUND,
                f"K2: offline=True but no config file was found for env '{environment}'. "
                f"Looked in:\n{looked}\n"
                "Run once with K2_OFFLINE=false to have the SDK write this file for you, "
                "then set _k2.managed to false to take ownership of it.",
            )

        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise K2Error(
                K2ErrorCode.FILE_MALFORMED,
                f"K2: {path} could not be read as JSON ({e}).\n"
                "A broken config file is a bug, not an outage — the SDK will not fall back to "
                "the server or serve partial values. Fix or delete the file.",
                -1,
                e,
            ) from e

        if not isinstance(doc, dict):
            raise K2Error(
                K2ErrorCode.FILE_MALFORMED,
                f"K2: {path} is not a K2 config document — expected an object with a "
                "'properties' key.",
            )

        header = doc.get("_k2") if isinstance(doc.get("_k2"), dict) else {}
        properties = doc.get("properties")
        if not isinstance(properties, dict):
            raise K2Error(
                K2ErrorCode.FILE_MALFORMED,
                f"K2: {path} has no 'properties' object. The expected shape is\n"
                '  {"_k2": {"org": …, "app": …, "env": …}, "properties": {"key": "value"}}',
            )

        file_app = header.get("app")
        if app and file_app and str(file_app) != str(app):
            raise K2Error(
                K2ErrorCode.FILE_APP_MISMATCH,
                f"K2: {path} belongs to app '{file_app}', but this client is configured for "
                f"app '{app}' (K2_APP).\n"
                "Two apps cannot share one config directory — set K2_CONFIG_DIR per app, or "
                "keep the file in each repo.",
            )

        age = _age_of(header.get("fetchedAt"))
        if self.max_age_seconds and age is not None and age > self.max_age_seconds:
            raise K2Error(
                K2ErrorCode.FILE_STALE,
                f"K2: {path} was written {human_age(age)} ago, beyond the K2_OFFLINE_MAX_AGE "
                f"limit of {human_age(self.max_age_seconds)}.\n"
                "Refresh it by running once against a reachable platform, or raise/unset "
                "K2_OFFLINE_MAX_AGE.",
            )

        return K2FileHit(properties=properties, header=header, path=path, age_seconds=age)

    # ---- writing ----

    def save(
        self,
        environment: str,
        properties: dict[str, Any],
        org: Optional[str] = None,
        app: Optional[str] = None,
    ) -> Optional[Path]:
        """Write the resolved properties for ``environment``; return the path, or ``None``.

        Best-effort by design: a read-only root filesystem (``readOnlyRootFilesystem: true``
        is common in hardened Docker/Kubernetes) must never break the app. A failed write is
        logged once at WARN and then suppressed — never retried per-fetch, never raised. The
        app runs normally with no offline fallback, which is what the operator chose.

        Refuses to overwrite a file the developer owns (``_k2.managed`` absent or false) —
        that is what kills "my hand-edits vanished".
        """
        if properties is None:
            return None
        target = self.write_target(environment)

        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None  # unreadable ⇒ treat as ours to replace
            if (
                isinstance(existing, dict)
                and isinstance(existing.get("_k2"), dict)
                and existing["_k2"].get("managed") is not True
            ):
                raise K2Error(
                    K2ErrorCode.FILE_UNMANAGED,
                    f"K2: refusing to overwrite {target} — its _k2.managed is not true, so it "
                    "is owned by you, not by the SDK.\n"
                    "Set K2_OFFLINE=true to read it and never write it, or set _k2.managed to "
                    "true to hand the file back to the SDK.",
                )

        doc = {
            "_k2": {
                "org": org,
                "app": app,
                "env": environment,
                "managed": True,
                "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sdk": self.sdk_version,
            },
            "properties": properties,
        }

        tmp = target.with_name(f".k2config-{os.getpid()}-{int(time.time() * 1000)}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _try_chmod(target.parent, 0o700)
            # Created 0600 by os.open, NOT written-then-chmod'ed: the file holds secrets in
            # clear on the token read path, and write_text would create it 0644-by-umask,
            # leaving a window in which any local user could read it.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, indent=2) + "\n")
            tmp.replace(target)  # atomic
            return target
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if not self._warned_not_writable:
                self._warned_not_writable = True
                warn(
                    f"{K2ErrorCode.FILE_NOT_WRITABLE}: cannot write {target} ({e}). Running "
                    "with no offline fallback. Mount a writable volume at that path, or set "
                    "K2_OFFLINE_CACHE=false to silence this."
                )
            return None

    @staticmethod
    def unlink_legacy(environment: str) -> None:
        """Remove legacy K2C1 / pre-K2C1 files left in ``~/.k2/cache`` by older SDKs.

        One opportunistic sweep on the first successful write; failures are ignored.
        """
        safe = _SAFE.sub("_", str(environment))
        cache_dir = Path(os.environ.get("K2_CACHE_DIR") or (Path.home() / ".k2" / "cache"))
        for name in (f"config-{safe}.json.enc", f"config-{safe}.json"):
            try:
                (cache_dir / name).unlink(missing_ok=True)
            except OSError:
                pass


def default_dir() -> Path:
    """``~/.k2/config``, or the platform equivalent. ``K2_CONFIG_DIR`` is applied by the caller."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "k2" / "config"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "k2" / "config"
    return Path.home() / ".k2" / "config"


def file_name(environment: str) -> str:
    return f"k2config-{_SAFE.sub('_', str(environment))}.json"


def parse_duration(value: Any) -> Optional[float]:
    """``7d`` / ``12h`` / ``30m`` / ``45s`` / bare seconds → seconds. ``None`` when unset."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = _DURATION.match(str(value).strip().lower())
    if not m:
        return None
    return float(m.group(1)) * _UNITS[m.group(2) or "s"]


def human_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{round(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)}m"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)}h"
    return f"{round(hours / 24)} days"


def warn(message: str) -> None:
    print(f"[k2-sdk] WARN {message}", file=sys.stderr)


def _age_of(fetched_at: Any) -> Optional[float]:
    if not fetched_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _trimmed(value: Optional[str]) -> Optional[str]:
    return str(value).strip() if value and str(value).strip() else None


def _try_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass  # non-POSIX; ignore
