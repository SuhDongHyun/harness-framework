from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ValidationError
from .validation import (
    RUN_STATUSES,
    require_exact_keys,
    require_string,
    validate_relative_path,
)

REVIEW_SEVERITIES = frozenset({"info", "warning", "error", "critical"})


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    title: str
    path: str | None
    evidence: str
    recommendation: str

    @classmethod
    def from_dict(cls, data: object) -> ReviewFinding:
        if not isinstance(data, Mapping):
            raise ValidationError("review finding must be an object")
        require_exact_keys(
            data,
            {"severity", "title", "path", "evidence", "recommendation"},
        )
        severity = data["severity"]
        if severity not in REVIEW_SEVERITIES:
            raise ValidationError(f"invalid review severity: {severity!r}")
        raw_path = data["path"]
        path = (
            None
            if raw_path is None
            else validate_relative_path(raw_path, "review finding path")
        )
        return cls(
            severity=str(severity),
            title=require_string(data["title"], "review finding title"),
            path=path,
            evidence=require_string(data["evidence"], "review finding evidence"),
            recommendation=require_string(
                data["recommendation"], "review finding recommendation"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "title": self.title,
            "path": self.path,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class ReviewResult:
    version: int
    observed_status: str
    summary: str
    findings: tuple[ReviewFinding, ...]

    @classmethod
    def from_dict(cls, data: object) -> ReviewResult:
        if not isinstance(data, Mapping):
            raise ValidationError("review result must be an object")
        require_exact_keys(
            data, {"version", "observed_status", "summary", "findings"}
        )
        if data["version"] != 1:
            raise ValidationError("review result version must be 1")
        status = data["observed_status"]
        if status not in RUN_STATUSES:
            raise ValidationError(f"invalid observed run status: {status!r}")
        raw_findings = data["findings"]
        if not isinstance(raw_findings, list):
            raise ValidationError("review findings must be an array")
        return cls(
            version=1,
            observed_status=str(status),
            summary=require_string(data["summary"], "review summary"),
            findings=tuple(
                ReviewFinding.from_dict(finding) for finding in raw_findings
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "observed_status": self.observed_status,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
        }
