from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ValidationError
from .validation import optional_string, path_tuple, require_exact_keys

STEP_OUTCOMES = frozenset({"completed", "failed", "blocked"})


@dataclass(frozen=True)
class StepResult:
    outcome: str
    summary: str
    changed_files: tuple[str, ...]
    error_message: str | None
    blocked_reason: str | None
    required_action: str | None

    @classmethod
    def from_dict(cls, data: object) -> StepResult:
        if not isinstance(data, Mapping):
            raise ValidationError("step result must be an object")
        require_exact_keys(
            data,
            {
                "outcome",
                "summary",
                "changed_files",
                "error_message",
                "blocked_reason",
                "required_action",
            },
        )
        outcome = data["outcome"]
        if outcome not in STEP_OUTCOMES:
            raise ValidationError(f"invalid step outcome: {outcome!r}")
        summary = data["summary"]
        if not isinstance(summary, str):
            raise ValidationError("step summary must be a string")
        return cls(
            outcome=str(outcome),
            summary=summary,
            changed_files=path_tuple(data["changed_files"], "changed_files"),
            error_message=optional_string(data["error_message"], "error_message"),
            blocked_reason=optional_string(data["blocked_reason"], "blocked_reason"),
            required_action=optional_string(
                data["required_action"], "required_action"
            ),
        )
