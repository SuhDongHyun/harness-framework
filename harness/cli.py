from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import HarnessConfig
from .controller import HarnessController, HarnessError
from .git_guard import GitError, GitGuard
from .models import ValidationError
from .runner import CodexRunner
from .store import RunStore
from .verifier import Verifier

ControllerFactory = Callable[[Path], HarnessController]


def build_controller(root: Path) -> HarnessController:
    config = HarnessConfig.load(root / ".harness" / "config.toml")
    return HarnessController(
        root=root,
        store=RunStore(root / ".harness" / "runs"),
        runner=CodexRunner(config.codex_command),
        verifier=Verifier(
            timeout_seconds=config.verification_timeout_seconds,
            max_output_bytes=config.max_output_bytes,
        ),
        git_guard=GitGuard(root),
        config=config,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Plan, approve, execute, and verify Codex work.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="create a read-only draft plan")
    plan.add_argument("goal", nargs="+")
    approve = commands.add_parser("approve", help="approve a draft plan")
    approve.add_argument("run_id")
    run = commands.add_parser("run", help="execute an approved plan")
    run.add_argument("run_id")
    status = commands.add_parser("status", help="read run state")
    status.add_argument("run_id")
    commands.add_parser("doctor", help="diagnose local harness prerequisites")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    controller_factory: ControllerFactory = build_controller,
) -> int:
    args = build_parser().parse_args(argv)
    project_root = (root or Path(__file__).resolve().parent.parent).resolve()
    if args.command == "doctor":
        report = run_doctor(project_root)
        _print_json(report)
        return 0 if report["ok"] else 2
    try:
        controller = controller_factory(project_root)
        if args.command == "plan":
            run_id = controller.plan(" ".join(args.goal))
            _print_json({"run_id": run_id, "status": "draft"})
            return 0
        if args.command == "approve":
            controller.approve(args.run_id)
            _print_json({"run_id": args.run_id, "status": "approved"})
            return 0
        if args.command == "status":
            _print_json(controller.status(args.run_id).to_dict())
            return 0
        state = controller.run(args.run_id)
        _print_json(state.to_dict())
        if state.status == "completed":
            return 0
        if state.status == "blocked":
            return 2
        return 1
    except (HarnessError, ValidationError, GitError, OSError) as error:
        print(f"harness: {error}", file=sys.stderr)
        return 2


def run_doctor(root: Path) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    checks.append(
        _check(
            "Python",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    try:
        git_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        git_ok = git_result.returncode == 0
        git_detail = git_result.stdout.strip() if git_ok else git_result.stderr.strip()
    except OSError as error:
        git_ok = False
        git_detail = str(error)
    checks.append(
        _check(
            "Git repository",
            git_ok,
            git_detail,
        )
    )

    config_path = root / ".harness" / "config.toml"
    try:
        config = HarnessConfig.load(config_path)
        config_ok = True
        config_detail = f"valid; command={config.codex_command}"
    except ValidationError as error:
        config = None
        config_ok = False
        config_detail = str(error)
    checks.append(_check("Config", config_ok, config_detail))
    codex_command = config.codex_command if config is not None else "codex"
    codex_path = shutil.which(codex_command)
    checks.append(
        _check(
            "Codex CLI",
            codex_path is not None,
            codex_path or f"{codex_command} not found",
        )
    )

    schema_paths = [
        root / "schemas" / "plan.schema.json",
        root / "schemas" / "step-result.schema.json",
        root / "schemas" / "state.schema.json",
    ]
    schema_errors: list[str] = []
    for path in schema_paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("root must be an object")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            schema_errors.append(f"{path.name}: {error}")
    checks.append(
        _check(
            "Schemas",
            not schema_errors,
            "3 valid schemas" if not schema_errors else "; ".join(schema_errors),
        )
    )

    state_parent = root / ".harness"
    writable = state_parent.is_dir() and os.access(state_parent, os.W_OK)
    checks.append(
        _check(
            "State root",
            writable,
            str(state_parent) if writable else f"{state_parent} is not writable",
        )
    )
    return {"ok": all(bool(check["ok"]) for check in checks), "checks": checks}


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))
