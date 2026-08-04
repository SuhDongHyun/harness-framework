from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import HarnessConfig
from ..domain.errors import ValidationError
from ..domain.validation import validate_id
from ..storage.run_store import RunStore
from .progress import ProgressBroker

ASSET_ROOT = Path(__file__).with_name("assets")


class DashboardServer:
    def __init__(
        self,
        *,
        root: Path,
        run_id: str,
        config: HarnessConfig,
        broker: ProgressBroker | None = None,
        port: int = 0,
    ):
        self.root = root.resolve()
        self.run_id = validate_id(run_id, "run id")
        self.config = config
        self.broker = broker or ProgressBroker()
        self.store = RunStore(self.root / ".harness" / "runs")
        run_dir = self.store.run_dir(run_id)
        run_dir.resolve().relative_to(self.store.runs_root)
        if not run_dir.is_dir():
            raise ValidationError(f"unknown run: {run_id}")
        handler = self._handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    def start(self) -> DashboardServer:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"harness-ui-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def snapshot(
        self, after: int = 0, *, include_history: bool = True
    ) -> dict[str, object]:
        run_dir = self.store.run_dir(self.run_id)
        state = _read_object(run_dir / "state.json")
        plan = _read_object(run_dir / "plan.json")
        controller_events = _read_jsonl(run_dir / "events.jsonl", 250)
        historical_agent_events: list[dict[str, object]] = []
        if include_history:
            for path in sorted((run_dir / "evidence").glob("*.jsonl")):
                historical_agent_events.extend(
                    {"source": path.name, "event": _display_event(event)}
                    for event in _read_jsonl(path, 80)
                )
            historical_agent_events = historical_agent_events[-250:]
        live = self.broker.snapshot(after)
        live["events"] = [
            {
                **entry,
                "event": _display_event(entry.get("event")),
            }
            for entry in live["events"]
        ]
        usage = _aggregate_usage(run_dir, controller_events)
        changed_files = _changed_files(run_dir)
        latest_output = _latest_verification_output(run_dir)
        approved_git = _read_object(run_dir / "approved-git.json")
        return {
            "run_id": self.run_id,
            "state": state,
            "plan": plan,
            "controller_events": controller_events,
            "historical_agent_events": historical_agent_events,
            "live": live,
            "usage": usage,
            "changed_files": changed_files,
            "latest_output": latest_output,
            "branch": approved_git.get("branch") if approved_git else None,
            "profiles": {
                "planner": _profile(self.config.planner),
                "executor": _profile(self.config.executor),
                "reviewer": _profile(self.config.reviewer),
                "reader": _profile(self.config.parallel_readers.profile),
            },
        }

    def _handler(self):
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._asset("dashboard.html", "text/html; charset=utf-8")
                    return
                if parsed.path == "/dashboard.css":
                    self._asset("dashboard.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/dashboard.js":
                    self._asset(
                        "dashboard.js", "text/javascript; charset=utf-8"
                    )
                    return
                if parsed.path == "/api/snapshot":
                    query = parse_qs(parsed.query)
                    raw_after = query.get("after", ["0"])[0]
                    raw_initial = query.get("initial", ["1"])[0]
                    try:
                        after = max(0, int(raw_after))
                        if raw_initial not in {"0", "1"}:
                            raise ValueError
                    except ValueError:
                        self.send_error(
                            HTTPStatus.BAD_REQUEST, "invalid snapshot query"
                        )
                        return
                    try:
                        payload = dashboard.snapshot(
                            after, include_history=raw_initial == "1"
                        )
                    except (OSError, ValidationError, json.JSONDecodeError) as error:
                        self.send_error(
                            HTTPStatus.INTERNAL_SERVER_ERROR, str(error)
                        )
                        return
                    self._send_json(payload)
                    return
                if parsed.path == "/healthz":
                    self._send_json({"ok": True})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _asset(self, name: str, content_type: str) -> None:
                try:
                    payload = (ASSET_ROOT / name).read_bytes()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self._headers(content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_json(self, value: object) -> None:
                payload = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self._headers("application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _headers(self, content_type: str) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
                )

        return Handler


def _profile(value) -> dict[str, str]:
    return {"model": value.model, "reasoning_effort": value.reasoning_effort}


def _read_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events[-limit:]


def _display_event(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {"type": "event"}
    event: dict[str, object] = {}
    for key in ("type", "message", "error"):
        value = raw.get(key)
        if isinstance(value, str):
            event[key] = value[-2000:]
        elif isinstance(value, dict):
            event[key] = {
                nested_key: nested_value[-2000:]
                for nested_key, nested_value in value.items()
                if isinstance(nested_key, str) and isinstance(nested_value, str)
            }
    item = raw.get("item")
    if isinstance(item, dict):
        compact: dict[str, object] = {}
        for key in (
            "id",
            "type",
            "command",
            "status",
            "tool",
            "agentStatus",
        ):
            value = item.get(key)
            if isinstance(value, (str, int, float, bool)):
                compact[key] = value
        for key in ("text", "aggregated_output", "aggregatedOutput"):
            value = item.get(key)
            if isinstance(value, str):
                compact[key] = value[-2000:]
        event["item"] = compact
    return event


def _add_usage(total: dict[str, int], raw: object) -> None:
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        if (
            isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            total[key] = total.get(key, 0) + value


def _aggregate_usage(
    run_dir: Path, controller_events: list[dict[str, object]]
) -> dict[str, int]:
    total: dict[str, int] = {}
    for event in controller_events:
        if event.get("type") == "plan.created":
            _add_usage(total, event.get("usage"))
    evidence_dir = run_dir / "evidence"
    for path in sorted(evidence_dir.glob("*-agent.json")):
        _add_usage(total, _read_object(path).get("usage"))
    for path in sorted(evidence_dir.glob("review-[0-9][0-9].json")):
        _add_usage(total, _read_object(path).get("usage"))
    return total


def _changed_files(run_dir: Path) -> list[str]:
    paths: set[str] = set()
    for path in (run_dir / "evidence").glob("*-agent.json"):
        raw = _read_object(path).get("changed_paths")
        if isinstance(raw, list):
            paths.update(value for value in raw if isinstance(value, str))
    return sorted(paths)


def _latest_verification_output(run_dir: Path) -> str:
    paths = sorted((run_dir / "evidence").glob("*verification.json"))
    if not paths:
        return "Waiting for verification evidence"
    raw = _read_object(paths[-1]).get("commands")
    if not isinstance(raw, list) or not raw:
        return "Verification completed without command output"
    command = raw[-1]
    if not isinstance(command, dict):
        return "Verification evidence is malformed"
    stdout = command.get("stdout")
    stderr = command.get("stderr")
    value = stdout if isinstance(stdout, str) and stdout.strip() else stderr
    return value.strip() if isinstance(value, str) and value.strip() else "No output"
