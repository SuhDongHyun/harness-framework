import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_SCHEMAS = (
    ROOT / "schemas" / "plan.schema.json",
    ROOT / "schemas" / "step-result.schema.json",
    ROOT / "schemas" / "review-result.schema.json",
)


class CodexOutputSchemaTests(unittest.TestCase):
    def test_uses_supported_structured_output_subset(self):
        for path in OUTPUT_SCHEMAS:
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self._check_node(schema, path.name)

    def _check_node(self, node, location):
        if isinstance(node, dict):
            self.assertNotIn("allOf", node, location)
            self.assertNotIn("uniqueItems", node, location)
            if "const" in node or "enum" in node:
                self.assertIn("type", node, location)
            for key, value in node.items():
                self._check_node(value, f"{location}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._check_node(value, f"{location}[{index}]")


if __name__ == "__main__":
    unittest.main()
