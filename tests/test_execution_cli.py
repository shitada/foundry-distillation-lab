"""Offline checks for explicit operation commands and bounded collection plans."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from foundry_distillation_lab.io import read_json, write_json, write_jsonl
from foundry_distillation_lab.training.collection import prepare_collection


ROOT = Path(__file__).resolve().parents[1]


class ExecutionCliTests(unittest.TestCase):
    def test_collection_and_deployment_no_longer_require_execution_approval(self):
        for command in (["collect.py"], ["deployment.py", "deploy"],
                        ["deployment.py", "status"], ["deployment.py", "cleanup"]):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / command[0]), *command[1:], "--help"],
                    capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                for removed in ("--approval", "--execute", "--observation-id"):
                    self.assertNotIn(removed, result.stdout)

    def test_collection_limits_and_snapshot_are_explicit(self):
        config = read_json(ROOT / "configs" / "examples" / "cloud-collection.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "config.json", config)
            write_jsonl(root / "inputs.jsonl", [
                {"conversation_id": "case-1", "category": "fixture", "prompt": "注文を確認してください。"}])
            plan = prepare_collection(root / "inputs.jsonl", root / "config.json", root / "prepared")
            self.assertEqual(plan["config"], config)
            self.assertEqual(plan["config"]["max_model_calls"], 12)
            self.assertEqual(plan["config"]["max_tool_calls"], 24)
            self.assertEqual(len(plan["inputs"][0]["input_sha256"]), 64)
            self.assertEqual(read_json(root / "prepared" / "collection-plan.json"), plan)
            self.assertNotIn("estimated_cost_per_model_request", plan["config"])
            with self.assertRaises(FileExistsError):
                prepare_collection(root / "inputs.jsonl", root / "config.json", root / "prepared")

    def test_unbounded_collection_configuration_is_rejected_offline(self):
        template = read_json(ROOT / "configs" / "examples" / "cloud-collection.json")
        for key in ("max_model_calls", "max_tool_calls", "max_output_tokens", "conversation_seconds"):
            for value in (0, -1, None, True, 1.5):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    config = deepcopy(template)
                    config[key] = value
                    write_json(root / "config.json", config)
                    write_jsonl(root / "inputs.jsonl", [
                        {"conversation_id": "fixture", "category": "fixture", "prompt": "fixture"}])
                    with self.assertRaisesRegex(ValueError, key):
                        prepare_collection(root / "inputs.jsonl", root / "config.json", root / "prepared")
                    self.assertFalse((root / "prepared").exists())


if __name__ == "__main__":
    unittest.main()
