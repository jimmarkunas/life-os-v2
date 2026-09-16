"""Runtime-only configuration boundary for public-code/private-data deployments."""
from __future__ import annotations

from dataclasses import dataclass
from os import environ as process_environ
from types import MappingProxyType
from typing import Mapping, Sequence


class ConfigurationError(RuntimeError):
    """Configuration failure that names keys but never includes private values."""


@dataclass(frozen=True, slots=True)
class ConfigField:
    name: str
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("configuration field name is required")
        if self.name != self.name.strip():
            raise ValueError("configuration field name may not contain surrounding whitespace")


class RuntimeConfig:
    """Validated in-memory config. Values are intentionally hidden from repr."""

    __slots__ = ("_values", "_declared")

    def __init__(self, values: Mapping[str, str], declared: Sequence[ConfigField]) -> None:
        self._values = MappingProxyType(dict(values))
        self._declared = tuple(declared)

    @classmethod
    def load(
        cls,
        fields: Sequence[ConfigField],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeConfig":
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise ConfigurationError("duplicate configuration field name")
        source = process_environ if environ is None else environ
        missing = [field.name for field in fields if field.required and not str(source.get(field.name, "")).strip()]
        if missing:
            raise ConfigurationError("missing required configuration: " + ", ".join(sorted(missing)))
        values = {field.name: str(source[field.name]) for field in fields if field.name in source}
        return cls(values, fields)

    def require(self, name: str) -> str:
        value = self._values.get(name)
        if value is None or not value.strip():
            raise ConfigurationError(f"missing required configuration: {name}")
        return value

    def optional(self, name: str) -> str | None:
        return self._values.get(name)

    def declared_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self._declared)

    def __repr__(self) -> str:
        names = ", ".join(self.declared_names())
        return f"RuntimeConfig(fields=[{names}])"
