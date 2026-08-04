from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .plan import Plan
from .validation import (
    RUN_STATUSES,
    STEP_STATUSES,
    optional_id,
    optional_string,
    require_exact_keys,
    terminal_result,
    validate_id,
)


@dataclass
class RunState:
    version: int
    run_id: str
    status: str
    plan_sha256: str | None
    approved_git_fingerprint: str | None
    current_step: str | None
    steps: list[dict[str, Any]]
    terminal_result: str | None
    blocked_reason: str | None
    required_action: str | None

    @classmethod
    def from_plan(cls, run_id: str, plan: Plan) -> RunState:
        validate_id(run_id, "run id")
        return cls(
            version=1,
            run_id=run_id,
            status="draft",
            plan_sha256=None,
            approved_git_fingerprint=None,
            current_step=None,
            steps=[
                {
                    "id": step.id,
                    "name": step.name,
                    "status": "pending",
                    "attempts": 0,
                    "summary": None,
                    "error": None,
                }
                for step in plan.steps
            ],
            terminal_result=None,
            blocked_reason=None,
            required_action=None,
        )

    @classmethod
    def from_dict(cls, data: object) -> RunState:
        if not isinstance(data, Mapping):
            raise ValidationError("run state must be an object")
        require_exact_keys(
            data,
            {
                "version",
                "run_id",
                "status",
                "plan_sha256",
                "approved_git_fingerprint",
                "current_step",
                "steps",
                "terminal_result",
                "blocked_reason",
                "required_action",
            },
        )
        if data["version"] != 1:
            raise ValidationError("state version must be 1")
        run_id = validate_id(data["run_id"], "run id")
        status = data["status"]
        if status not in RUN_STATUSES:
            raise ValidationError(f"invalid run status: {status!r}")
        raw_steps = data["steps"]
        if not isinstance(raw_steps, list):
            raise ValidationError("state steps must be an array")
        steps: list[dict[str, Any]] = []
        for raw in raw_steps:
            if not isinstance(raw, Mapping):
                raise ValidationError("state step must be an object")
            require_exact_keys(
                raw, {"id", "name", "status", "attempts", "summary", "error"}
            )
            step_status = raw["status"]
            if step_status not in STEP_STATUSES:
                raise ValidationError(f"invalid step status: {step_status!r}")
            attempts = raw["attempts"]
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 0
            ):
                raise ValidationError("step attempts must be a non-negative integer")
            validate_id(raw["id"], "state step id")
            validate_id(raw["name"], "state step name")
            optional_string(raw["summary"], "state step summary")
            optional_string(raw["error"], "state step error")
            steps.append(dict(raw))
        return cls(
            version=1,
            run_id=run_id,
            status=str(status),
            plan_sha256=optional_string(data["plan_sha256"], "plan_sha256"),
            approved_git_fingerprint=optional_string(
                data["approved_git_fingerprint"], "approved_git_fingerprint"
            ),
            current_step=optional_id(data["current_step"], "current_step"),
            steps=steps,
            terminal_result=terminal_result(data["terminal_result"]),
            blocked_reason=optional_string(data["blocked_reason"], "blocked_reason"),
            required_action=optional_string(
                data["required_action"], "required_action"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "status": self.status,
            "plan_sha256": self.plan_sha256,
            "approved_git_fingerprint": self.approved_git_fingerprint,
            "current_step": self.current_step,
            "steps": self.steps,
            "terminal_result": self.terminal_result,
            "blocked_reason": self.blocked_reason,
            "required_action": self.required_action,
        }

    def step(self, step_id: str) -> dict[str, Any]:
        for step in self.steps:
            if step["id"] == step_id:
                return step
        raise ValidationError(f"state does not contain step {step_id!r}")
