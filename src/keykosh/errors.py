"""Errors raised by the KeyKosh SDK."""

from __future__ import annotations


class K2Error(Exception):
    """A KeyKosh SDK error.

    ``status_code`` is the HTTP status when the failure came from the platform,
    or ``-1`` for a transport/configuration error.
    """

    def __init__(self, message: str, status_code: int = -1, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        if cause is not None:
            self.__cause__ = cause

    def is_availability_error(self) -> bool:
        """Transport failures and 5xx are eligible for offline-cache fallback."""
        return self.status_code == -1 or self.status_code >= 500
