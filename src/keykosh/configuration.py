"""Typed view over a resolved configuration response."""

from __future__ import annotations

from typing import Any, Mapping, Optional


class K2Configuration:
    """Resolved configuration for one environment.

    Wraps the property map returned by ``GET /api/config/token/{env}/current``.
    """

    def __init__(self, environment: Optional[str], properties: Mapping[str, Any],
                 metadata: Optional[Mapping[str, Any]] = None) -> None:
        self.environment = environment
        self.properties: dict[str, Any] = dict(properties or {})
        self.metadata: dict[str, Any] = dict(metadata or {})

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "K2Configuration":
        body = body or {}
        return cls(body.get("environment"), body.get("properties") or {}, body.get("metadata") or {})

    @classmethod
    def from_properties(cls, environment: Optional[str], properties: Mapping[str, Any]) -> "K2Configuration":
        return cls(environment, properties or {})

    @classmethod
    def from_file(cls, organization: Optional[str], application: Optional[str],
                  environment: Optional[str], properties: Mapping[str, Any]) -> "K2Configuration":
        """Configuration read from a local file source (§3b), carrying org/app coordinates."""
        cfg = cls(environment, properties or {})
        cfg.organization = organization
        cfg.application = application
        return cfg

    def has(self, key: str) -> bool:
        return key in self.properties

    def get_string(self, key: str, default: Optional[str] = None) -> Optional[str]:
        v = self.properties.get(key)
        return default if v is None else str(v)

    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        v = self.properties.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        v = self.properties.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: Optional[bool] = None) -> Optional[bool]:
        v = self.properties.get(key)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() == "true"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.properties)
