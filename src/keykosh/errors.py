"""Errors raised by the KeyKosh SDK."""

from __future__ import annotations


class K2ErrorCode:
    """Stable error codes (OFFLINE_AND_HOTRELOAD_DESIGN.md §3).

    These are the SDK's public debugging surface: greppable, documentable, and stable
    across message rewording. All three SDKs use the identical set.

    Classes:

    - ``config`` — a misconfiguration; raised when the client is CONSTRUCTED, not on first read
    - ``file`` — something about the local ``k2config-<env>.json``
    - ``auth`` — the platform rejected the credential; NEVER falls back to the local file
    - ``availability`` — the platform was unreachable; falls back to the local file when present
    """

    MISSING_BASE_URL = "K2_MISSING_BASE_URL"
    MISSING_TOKEN = "K2_MISSING_TOKEN"
    TOKEN_FILE_UNREADABLE = "K2_TOKEN_FILE_UNREADABLE"
    MISSING_ENV = "K2_MISSING_ENV"
    INVALID_MODE = "K2_INVALID_MODE"

    FILE_NOT_FOUND = "K2_FILE_NOT_FOUND"
    FILE_MALFORMED = "K2_FILE_MALFORMED"
    FILE_APP_MISMATCH = "K2_FILE_APP_MISMATCH"
    FILE_STALE = "K2_FILE_STALE"
    FILE_UNMANAGED = "K2_FILE_UNMANAGED"
    FILE_NOT_WRITABLE = "K2_FILE_NOT_WRITABLE"

    UNAUTHORIZED = "K2_UNAUTHORIZED"
    FORBIDDEN = "K2_FORBIDDEN"
    NOT_FOUND = "K2_NOT_FOUND"
    HOST_NOT_LICENSED = "K2_HOST_NOT_LICENSED"

    UNREACHABLE = "K2_UNREACHABLE"
    TIMEOUT = "K2_TIMEOUT"
    SERVER_ERROR = "K2_SERVER_ERROR"

    #: Residual bucket: a non-2xx the table above doesn't name (an unexpected 4xx).
    #: Reachable-and-refusing, so NOT an availability error — never serves the local file.
    REQUEST_FAILED = "K2_REQUEST_FAILED"


#: The only codes eligible for local-file fallback — everything else is a real failure.
_AVAILABILITY_CODES = frozenset(
    {K2ErrorCode.UNREACHABLE, K2ErrorCode.TIMEOUT, K2ErrorCode.SERVER_ERROR}
)


class K2Error(Exception):
    """A KeyKosh SDK error.

    ``code`` is the stable :class:`K2ErrorCode`; ``status_code`` is the HTTP status when the
    failure came from the platform, or ``-1`` for a transport/configuration/file error.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = -1,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        if cause is not None:
            self.__cause__ = cause

    def is_availability_error(self) -> bool:
        """Whether the platform was unreachable (as opposed to reachable and refusing).

        Only these are eligible for the local-file fallback — an auth failure is not an
        outage, so a valid file sitting on disk is deliberately declined.
        """
        return self.code in _AVAILABILITY_CODES

    @property
    def outage_detail(self) -> str:
        """A short ``CODE: reason`` for logs, naming the underlying transport failure.

        The degraded paths — serving the local file, or reporting that the file was
        unusable — used to log the code alone. ``K2_UNREACHABLE`` covers a DNS failure, a
        refused connection and a **TLS verification failure** alike, so the operator saw
        "unreachable" for what was really an expired certificate or an internal CA missing
        from the trust store, with nothing to tell them apart. That is a misconfiguration
        wearing an outage's clothes.

        Falls back to the bare code when there is no cause to name (a 5xx, say).
        """
        cause = self.__cause__
        if cause is None or isinstance(cause, K2Error):
            return self.code
        reason = getattr(cause, "reason", None)  # urllib wraps the real error here
        detail = str(reason if reason is not None else cause).strip()
        return f"{self.code}: {type(cause).__name__}: {detail}" if detail else self.code
