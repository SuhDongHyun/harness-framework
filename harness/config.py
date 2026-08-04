from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

from .domain import ValidationError

REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


@dataclass(frozen=True)
class AgentProfile:
    model: str
    reasoning_effort: str

    def validate(self, label: str) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValidationError(f"{label}.model must be a non-empty string")
        if self.reasoning_effort not in REASONING_EFFORTS:
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValidationError(
                f"{label}.reasoning_effort must be one of: {choices}"
            )


DEFAULT_PLANNER_PROFILE = AgentProfile("gpt-5.6-sol", "high")
DEFAULT_EXECUTOR_PROFILE = AgentProfile("gpt-5.6-luna", "xhigh")
DEFAULT_REVIEWER_PROFILE = AgentProfile("gpt-5.6-sol", "high")


@dataclass(frozen=True)
class ParallelReaderConfig:
    enabled: bool = True
    max_workers: int = 3
    profile: AgentProfile = AgentProfile("gpt-5.6-luna", "medium")

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidationError("parallel_readers.enabled must be boolean")
        _bounded_int(self.max_workers, "parallel_readers.max_workers", 1, 8)
        self.profile.validate("parallel_readers")


DEFAULT_PARALLEL_READERS = ParallelReaderConfig()


@dataclass(frozen=True)
class ParallelWriterConfig:
    enabled: bool = False
    max_workers: int = 2

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidationError("parallel_writers.enabled must be boolean")
        _bounded_int(self.max_workers, "parallel_writers.max_workers", 1, 4)


DEFAULT_PARALLEL_WRITERS = ParallelWriterConfig()


@dataclass(frozen=True)
class HarnessConfig:
    max_retries: int = 3
    timeout_seconds: int = 1800
    verification_timeout_seconds: int = 900
    max_output_bytes: int = 200_000
    codex_command: str = "codex"
    planner: AgentProfile = DEFAULT_PLANNER_PROFILE
    executor: AgentProfile = DEFAULT_EXECUTOR_PROFILE
    reviewer: AgentProfile = DEFAULT_REVIEWER_PROFILE
    parallel_readers: ParallelReaderConfig = DEFAULT_PARALLEL_READERS
    parallel_writers: ParallelWriterConfig = DEFAULT_PARALLEL_WRITERS

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
            "planner",
            "executor",
            "reviewer",
            "parallel_readers",
            "parallel_writers",
        }
        extra = set(values) - allowed
        if extra:
            raise ValidationError(f"unknown config fields: {', '.join(sorted(extra))}")
        scalar_values = {
            key: value
            for key, value in values.items()
            if key
            not in {
                "planner",
                "executor",
                "reviewer",
                "parallel_readers",
                "parallel_writers",
            }
        }
        config = cls(
            **scalar_values,
            planner=_load_profile(
                values.get("planner"), "planner", DEFAULT_PLANNER_PROFILE
            ),
            executor=_load_profile(
                values.get("executor"), "executor", DEFAULT_EXECUTOR_PROFILE
            ),
            reviewer=_load_profile(
                values.get("reviewer"), "reviewer", DEFAULT_REVIEWER_PROFILE
            ),
            parallel_readers=_load_parallel_readers(values.get("parallel_readers")),
            parallel_writers=_load_parallel_writers(values.get("parallel_writers")),
        )
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
        self.planner.validate("planner")
        self.executor.validate("executor")
        self.reviewer.validate("reviewer")
        self.parallel_readers.validate()
        self.parallel_writers.validate()


def _load_profile(
    raw: object, label: str, default: AgentProfile
) -> AgentProfile:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must be a table")
    extra = set(raw) - {"model", "reasoning_effort"}
    if extra:
        raise ValidationError(
            f"unknown {label} fields: {', '.join(sorted(extra))}"
        )
    return AgentProfile(
        model=raw.get("model", default.model),
        reasoning_effort=raw.get("reasoning_effort", default.reasoning_effort),
    )


def _load_parallel_readers(raw: object) -> ParallelReaderConfig:
    if raw is None:
        return DEFAULT_PARALLEL_READERS
    if not isinstance(raw, dict):
        raise ValidationError("parallel_readers must be a table")
    allowed = {"enabled", "max_workers", "model", "reasoning_effort"}
    extra = set(raw) - allowed
    if extra:
        raise ValidationError(
            "unknown parallel_readers fields: " + ", ".join(sorted(extra))
        )
    default = DEFAULT_PARALLEL_READERS
    return ParallelReaderConfig(
        enabled=raw.get("enabled", default.enabled),
        max_workers=raw.get("max_workers", default.max_workers),
        profile=AgentProfile(
            model=raw.get("model", default.profile.model),
            reasoning_effort=raw.get(
                "reasoning_effort", default.profile.reasoning_effort
            ),
        ),
    )


def _load_parallel_writers(raw: object) -> ParallelWriterConfig:
    if raw is None:
        return DEFAULT_PARALLEL_WRITERS
    if not isinstance(raw, dict):
        raise ValidationError("parallel_writers must be a table")
    extra = set(raw) - {"enabled", "max_workers"}
    if extra:
        raise ValidationError(
            "unknown parallel_writers fields: " + ", ".join(sorted(extra))
        )
    return ParallelWriterConfig(
        enabled=raw.get("enabled", DEFAULT_PARALLEL_WRITERS.enabled),
        max_workers=raw.get("max_workers", DEFAULT_PARALLEL_WRITERS.max_workers),
    )


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError(f"{label} must be between {minimum} and {maximum}")
