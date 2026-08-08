from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

from .agents import (
    CodexRunner,
    CodexSandbox,
    codex_available_models,
    codex_login_status,
    harness_codex_home_status,
    prepare_harness_codex_home,
    resolve_harness_codex_home,
)
from .config import HarnessConfig
from .domain import ValidationError
from .orchestration import HarnessController, HarnessError
from .safety import GitError, GitGuard, Verifier
from .storage import RunStore
from .ui import DashboardServer, ProgressBroker

ControllerFactory = Callable[[Path], HarnessController]


def build_controller(
    root: Path,
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> HarnessController:
    config = HarnessConfig.load(root / ".harness" / "config.toml")
    codex_home = resolve_harness_codex_home()
    return HarnessController(
        root=root,
        store=RunStore(root / ".harness" / "runs"),
        runner=CodexRunner(
            config.codex_command,
            event_sink=event_sink,
            codex_home=codex_home,
        ),
        verifier=Verifier(
            timeout_seconds=config.verification_timeout_seconds,
            max_output_bytes=config.max_verification_output_bytes,
            sandbox=CodexSandbox(config.codex_command, codex_home),
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
    run.add_argument("--ui", action="store_true", help="show live localhost UI")
    run.add_argument("--ui-port", type=int, default=0)
    review = commands.add_parser("review", help="review a run without changing state")
    review.add_argument("run_id")
    status = commands.add_parser("status", help="read run state")
    status.add_argument("run_id")
    ui = commands.add_parser("ui", help="show a read-only run dashboard")
    ui.add_argument("run_id")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument(
        "--open-browser",
        action="store_true",
        help="open the dashboard in the default browser",
    )
    commands.add_parser("doctor", help="diagnose local harness prerequisites")
    commands.add_parser(
        "setup",
        help="prepare the Linux/WSL Codex runtime home and print setup steps",
    )
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
        if args.command == "setup":
            _print_json(run_setup(project_root))
            return 0
        if (
            controller_factory is build_controller
            and args.command in {"plan", "approve", "run", "review"}
        ):
            _require_codex_runtime_home(project_root)
        if args.command == "ui":
            config = HarnessConfig.load(project_root / ".harness" / "config.toml")
            server = DashboardServer(
                root=project_root,
                run_id=args.run_id,
                config=config,
                port=args.port,
            ).start()
            _print_json({"run_id": args.run_id, "ui": server.url})
            if args.open_browser:
                _open_browser(server.url)
            _wait_for_dashboard(server)
            return 0
        server: DashboardServer | None = None
        if args.command == "run" and args.ui and controller_factory is build_controller:
            config = HarnessConfig.load(project_root / ".harness" / "config.toml")
            broker = ProgressBroker()
            controller = build_controller(project_root, event_sink=broker.publish)
            server = DashboardServer(
                root=project_root,
                run_id=args.run_id,
                config=config,
                broker=broker,
                port=args.ui_port,
            ).start()
            print(f"harness UI: {server.url}", file=sys.stderr)
        else:
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
        if args.command == "review":
            result = controller.review(args.run_id)
            _print_json({"run_id": args.run_id, "review": result.to_dict()})
            return 0
        state = controller.run(args.run_id)
        _print_json(state.to_dict())
        exit_code = 1
        if state.status == "completed":
            exit_code = 0
        elif state.status == "blocked":
            exit_code = 2
        if server is not None:
            print("harness UI remains available; press Ctrl-C to stop", file=sys.stderr)
            _wait_for_dashboard(server)
        return exit_code
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
        config_detail = (
            f"valid; command={config.codex_command}; "
            f"planner={config.planner.model}/{config.planner.reasoning_effort}; "
            f"executor={config.executor.model}/{config.executor.reasoning_effort}; "
            f"reviewer={config.reviewer.model}/{config.reviewer.reasoning_effort}"
            f"; parallel_readers={config.parallel_readers.enabled}/"
            f"{config.parallel_readers.max_workers}/"
            f"{config.parallel_readers.profile.model}/"
            f"{config.parallel_readers.profile.reasoning_effort}; "
            f"parallel_writers={config.parallel_writers.enabled}/"
            f"{config.parallel_writers.max_workers}; "
            f"executor_network={config.network.executor_enabled}; "
            f"output_limits={config.max_event_log_bytes}/"
            f"{config.max_final_payload_bytes}/"
            f"{config.max_tool_output_bytes}/"
            f"{config.max_verification_output_bytes}"
        )
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
    codex_home: Path | None = None
    try:
        codex_home = resolve_harness_codex_home()
        codex_home_ok, codex_home_detail = harness_codex_home_status(codex_home)
    except (OSError, ValueError) as error:
        codex_home_ok = False
        codex_home_detail = str(error)
    checks.append(
        _check("Harness Codex runtime home", codex_home_ok, codex_home_detail)
    )
    if codex_home_ok:
        login_ok, login_detail = codex_login_status(codex_command, codex_home)
    else:
        login_ok = False
        login_detail = "Harness Codex runtime home must be ready first"
    checks.append(_check("Harness Codex login", login_ok, login_detail))
    if config is not None and codex_path is not None and codex_home_ok:
        assert codex_home is not None
        catalog_ok, available_models, catalog_detail = codex_available_models(
            config.codex_command, codex_home
        )
        configured_models = {
            "planner": config.planner.model,
            "executor": config.executor.model,
            "reviewer": config.reviewer.model,
        }
        if config.parallel_readers.enabled:
            configured_models["parallel_readers"] = (
                config.parallel_readers.profile.model
            )
        missing = {
            role: model
            for role, model in configured_models.items()
            if model not in available_models
        }
        model_ok = catalog_ok and not missing
        if not catalog_ok:
            model_detail = catalog_detail
        elif missing:
            model_detail = "unavailable configured models: " + ", ".join(
                f"{role}={model}" for role, model in sorted(missing.items())
            )
        else:
            model_detail = (
                f"all configured models available; {catalog_detail}"
            )
    else:
        model_ok = False
        model_detail = "Codex command, config, and runtime home must be ready first"
    checks.append(_check("Configured models", model_ok, model_detail))

    schema_paths = [
        root / "schemas" / "plan.schema.json",
        root / "schemas" / "step-result.schema.json",
        root / "schemas" / "review-result.schema.json",
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
            "4 valid schemas" if not schema_errors else "; ".join(schema_errors),
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


def run_setup(root: Path) -> dict[str, object]:
    if not sys.platform.startswith("linux"):
        raise HarnessError("setup supports Linux and WSL only")
    config = HarnessConfig.load(root / ".harness" / "config.toml")
    codex_home = resolve_harness_codex_home()
    try:
        codex_home.relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise HarnessError("Harness Codex runtime home must remain outside the repository")
    prepare_harness_codex_home(codex_home)
    config_snippet = (
        "[sandbox_workspace_write]\n"
        f"writable_roots = [{json.dumps(str(codex_home))}]"
    )
    login_command = shlex.join(
        ["env", f"CODEX_HOME={codex_home}", config.codex_command, "login"]
    )
    return {
        "platform": "linux-wsl",
        "codex_home": str(codex_home),
        "status": "directory-ready",
        "login_command": login_command,
        "codex_config_snippet": config_snippet,
        "next_steps": [
            "Run login_command in a trusted terminal",
            "Add codex_config_snippet to the Codex config used for this project",
            "Restart Codex so the writable root becomes active",
            "Run `python3 scripts/harness.py doctor`",
        ],
    }


def _open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error) as error:
        print(f"harness UI browser launch failed: {error}; open {url}", file=sys.stderr)
        return
    if not opened:
        print(f"harness UI browser launch unavailable; open {url}", file=sys.stderr)


def _require_codex_runtime_home(root: Path) -> None:
    config = HarnessConfig.load(root / ".harness" / "config.toml")
    codex_home = resolve_harness_codex_home()
    ok, detail = harness_codex_home_status(codex_home)
    if not ok:
        raise HarnessError(
            f"Harness Codex runtime home is not ready: {detail}. "
            "Run `python3 scripts/harness.py setup`, complete the printed login and "
            "writable-root steps, restart Codex, and run doctor again."
        )
    login_ok, login_detail = codex_login_status(
        config.codex_command,
        codex_home,
    )
    if not login_ok:
        raise HarnessError(
            f"Harness Codex login is not ready: {login_detail}. "
            "Run the login command printed by `python3 scripts/harness.py setup` "
            "and run doctor again."
        )


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _wait_for_dashboard(server: DashboardServer) -> None:
    try:
        threading_event = threading.Event()
        while True:
            threading_event.wait(60)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
