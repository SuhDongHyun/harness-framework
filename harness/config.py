from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

from .models import ValidationError


@dataclass(frozen=True)
class HarnessConfig:
    max_retries: int = 3
    timeout_seconds: int = 1800
    verification_timeout_seconds: int = 900
    max_output_bytes: int = 200_000
    codex_command: str = "codex"

    @classmethod
    def load(cls, path: Path) -> HarnessConfig:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValidationError(f"cannot load config {path}: {error}") from error
        if set(raw) != {"harness"} or not isinstance(raw["harness"], dict):
            raise ValidationError("config must contain only a [harness] table")
        values = raw["harness"]
        allowed = {
            "max_retries",
            "timeout_seconds",
            "verification_timeout_seconds",
            "max_output_bytes",
            "codex_command",
        }
        extra = set(values) - allowed
        if extra:
            raise ValidationError(f"unknown config fields: {', '.join(sorted(extra))}")
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        _bounded_int(self.max_retries, "max_retries", 1, 10)
        _bounded_int(self.timeout_seconds, "timeout_seconds", 1, 7200)
        _bounded_int(
            self.verification_timeout_seconds,
            "verification_timeout_seconds",
            1,
            7200,
        )
        _bounded_int(
            self.max_output_bytes,
            "max_output_bytes",
            1024,
            10_000_000,
        )
        if not isinstance(self.codex_command, str) or not self.codex_command.strip():
            raise ValidationError("codex_command must be a non-empty string")


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError(f"{label} must be between {minimum} and {maximum}")
