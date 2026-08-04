import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from harness.config import HarnessConfig
from harness.domain import Plan, RunState, ValidationError
from harness.storage import RunStore
from harness.ui import DashboardServer, ProgressBroker
from tests.test_models_store import valid_plan


class ProgressBrokerTests(unittest.TestCase):
    def test_returns_only_events_after_sequence(self):
        broker = ProgressBroker(max_events=3)
        broker.publish({"type": "first"})
        broker.publish({"type": "second"})
        snapshot = broker.snapshot(after=1)
        self.assertEqual(2, snapshot["sequence"])
        self.assertEqual("second", snapshot["events"][0]["event"]["type"])


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "config.toml").write_text("[harness]\n")
        self.store = RunStore(self.root / ".harness" / "runs")
        self.store.create_run("run-1", "goal")
        plan = Plan.from_dict(valid_plan())
        self.store.write_json_atomic("run-1", "plan.json", plan.to_dict())
        self.store.write_json_atomic(
            "run-1", "state.json", RunState.from_plan("run-1", plan).to_dict()
        )

    def test_serves_dashboard_and_auto_refresh_snapshot(self):
        broker = ProgressBroker()
        broker.publish({"type": "turn.started"})
        server = DashboardServer(
            root=self.root,
            run_id="run-1",
            config=HarnessConfig(),
            broker=broker,
        ).start()
        self.addCleanup(server.stop)
        with urllib.request.urlopen(server.url, timeout=2) as response:
            html = response.read().decode()
        self.assertIn("LIVE ACTIVITY", html)
        with urllib.request.urlopen(
            server.url + "api/snapshot?after=0", timeout=2
        ) as response:
            payload = json.loads(response.read())
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual("draft", payload["state"]["status"])
        self.assertEqual("turn.started", payload["live"]["events"][0]["event"]["type"])

        broker.publish(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": "x" * 5000,
                },
            }
        )
        incremental = server.snapshot(after=1, include_history=False)
        self.assertEqual([], incremental["historical_agent_events"])
        output = incremental["live"]["events"][0]["event"]["item"][
            "aggregated_output"
        ]
        self.assertEqual(2000, len(output))

    def test_rejects_mutating_http_method(self):
        server = DashboardServer(
            root=self.root,
            run_id="run-1",
            config=HarnessConfig(),
        ).start()
        self.addCleanup(server.stop)
        request = urllib.request.Request(server.url, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(405, caught.exception.code)

    def test_rejects_unknown_run(self):
        with self.assertRaisesRegex(ValidationError, "unknown run: missing"):
            DashboardServer(
                root=self.root,
                run_id="missing",
                config=HarnessConfig(),
            )


if __name__ == "__main__":
    unittest.main()
