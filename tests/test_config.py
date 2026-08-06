import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.domain import ValidationError


class HarnessConfigTests(unittest.TestCase):
    def load(self, text: str) -> HarnessConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return HarnessConfig.load(path)

    def test_default_role_profiles(self):
        config = self.load("[harness]\n")
        self.assertEqual(("gpt-5.6-sol", "high"), (
            config.planner.model,
            config.planner.reasoning_effort,
        ))
        self.assertEqual(("gpt-5.6-terra", "xhigh"), (
            config.executor.model,
            config.executor.reasoning_effort,
        ))
        self.assertEqual(("gpt-5.6-sol", "high"), (
            config.reviewer.model,
            config.reviewer.reasoning_effort,
        ))
        self.assertTrue(config.parallel_readers.enabled)
        self.assertEqual(3, config.parallel_readers.max_workers)
        self.assertEqual("gpt-5.6-terra", config.parallel_readers.profile.model)
        self.assertEqual("medium", config.parallel_readers.profile.reasoning_effort)
        self.assertFalse(config.parallel_writers.enabled)
        self.assertEqual(2, config.parallel_writers.max_workers)
        self.assertEqual(1_000_000, config.max_event_log_bytes)
        self.assertEqual(200_000, config.max_final_payload_bytes)
        self.assertEqual(20_000, config.max_tool_output_bytes)
        self.assertEqual(200_000, config.max_verification_output_bytes)

    def test_role_profiles_can_be_overridden_independently(self):
        config = self.load(
            "[harness]\n"
            "[harness.planner]\n"
            'model = "planner-model"\n'
            "[harness.executor]\n"
            'reasoning_effort = "medium"\n'
            "[harness.reviewer]\n"
            'model = "reviewer-model"\n'
            'reasoning_effort = "xhigh"\n'
        )
        self.assertEqual("planner-model", config.planner.model)
        self.assertEqual("high", config.planner.reasoning_effort)
        self.assertEqual("gpt-5.6-terra", config.executor.model)
        self.assertEqual("medium", config.executor.reasoning_effort)
        self.assertEqual("reviewer-model", config.reviewer.model)
        self.assertEqual("xhigh", config.reviewer.reasoning_effort)

    def test_rejects_unknown_role_field(self):
        with self.assertRaisesRegex(ValidationError, "unknown planner fields"):
            self.load("[harness.planner]\nunknown = true\n")

    def test_rejects_unsupported_reasoning_effort(self):
        with self.assertRaisesRegex(ValidationError, "reasoning_effort"):
            self.load('[harness.executor]\nreasoning_effort = "ultra"\n')

    def test_parallel_readers_can_be_overridden(self):
        config = self.load(
            "[harness.parallel_readers]\n"
            "enabled = false\n"
            "max_workers = 2\n"
            'model = "reader-model"\n'
            'reasoning_effort = "low"\n'
        )
        self.assertFalse(config.parallel_readers.enabled)
        self.assertEqual(2, config.parallel_readers.max_workers)
        self.assertEqual("reader-model", config.parallel_readers.profile.model)
        self.assertEqual("low", config.parallel_readers.profile.reasoning_effort)

    def test_parallel_writers_can_be_enabled(self):
        config = self.load(
            "[harness.parallel_writers]\n"
            "enabled = true\n"
            "max_workers = 3\n"
        )
        self.assertTrue(config.parallel_writers.enabled)
        self.assertEqual(3, config.parallel_writers.max_workers)

    def test_output_limits_can_be_configured_independently(self):
        config = self.load(
            "[harness]\n"
            "max_event_log_bytes = 500000\n"
            "max_final_payload_bytes = 100000\n"
            "max_tool_output_bytes = 10000\n"
            "max_verification_output_bytes = 300000\n"
        )
        self.assertEqual(500_000, config.max_event_log_bytes)
        self.assertEqual(100_000, config.max_final_payload_bytes)
        self.assertEqual(10_000, config.max_tool_output_bytes)
        self.assertEqual(300_000, config.max_verification_output_bytes)

    def test_legacy_output_limit_populates_all_split_limits(self):
        config = self.load("[harness]\nmax_output_bytes = 4096\n")
        self.assertEqual(4096, config.max_event_log_bytes)
        self.assertEqual(4096, config.max_final_payload_bytes)
        self.assertEqual(4096, config.max_tool_output_bytes)
        self.assertEqual(4096, config.max_verification_output_bytes)

    def test_legacy_and_split_output_limits_cannot_be_combined(self):
        with self.assertRaisesRegex(ValidationError, "cannot be combined"):
            self.load(
                "[harness]\n"
                "max_output_bytes = 4096\n"
                "max_event_log_bytes = 8192\n"
            )


if __name__ == "__main__":
    unittest.main()
