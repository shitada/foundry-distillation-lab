"""Exercise the documented local study without sending model requests."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from foundry_distillation_lab.datasets.prepare import prepare
from foundry_distillation_lab.evaluation.runner import OpenAITransport
from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.reporting.bridge import prepare_from_files


ROOT = Path(__file__).resolve().parents[1]


class ChapterSevenPipelineTests(unittest.TestCase):
    def test_prepare_study_and_next_chapter_cost_input(self):
        from foundry_distillation_lab.evaluation.cases import prepare_cases

        spec = importlib.util.spec_from_file_location("chapter07_evaluate", ROOT / "scripts" / "evaluate.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(ROOT / "data" / "samples" / "traces.jsonl", root / "data")
            input_path = root / "e2e-input.json"
            prepare_cases(root / "data", input_path)
            bundle = read_json(input_path)
            count = len(bundle["cases"])
            self.assertGreaterEqual(count, 8)
            self.assertEqual(bundle["records"], [])
            configuration = {
                "base_url": "https://example.invalid/openai/v1/",
                "targets": {"teacher": "teacher-model", "base": "student-base",
                            "fine_tuned": "student-fine-tuned"},
                "max_completion_tokens": 1024, "timeout_seconds": 60,
                "max_model_calls": 12, "max_tool_calls": 24,
            }
            write_json(root / "evaluation.json", configuration)
            write_json(root / "grading.json", {
                "base_url": "https://example.invalid/openai/v1/", "deployment": "grader",
                "max_completion_tokens": 1024, "timeout_seconds": 60,
            })
            model_requests, judge_requests = [], []

            def respond(payload):
                if payload["target"] == "grader":
                    judge_requests.append(payload)
                    return {
                        "message": {"role": "assistant", "content": json.dumps({
                            "decision": "success",
                            "reason": "架空の採点応答。注文番号の確認質問を返しています。",
                        }, ensure_ascii=False)},
                        "finish_reason": "stop", "response_model": "fixture-judge",
                        "response_id": "fixture-grade",
                        "usage": {"input_tokens": 900, "output_tokens": 30, "cached_input_tokens": 0},
                    }
                model_requests.append(payload)
                self.assertNotIn("expected", payload)
                self.assertNotIn("reference", payload)
                self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
                return {
                    "message": {"role": "assistant", "content": "注文番号を教えてください。"},
                    "usage": {"input_tokens": 20, "output_tokens": 8, "cached_input_tokens": 0},
                }

            study_dir = root / "study"
            with patch.object(OpenAITransport, "__call__", side_effect=respond), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "study", "--input", str(input_path), "--config", str(root / "evaluation.json"),
                    "--grading-config", str(root / "grading.json"), "--output-dir", str(study_dir),
                ])
                self.assertEqual(code, 0)
                self.assertEqual(cli.main([
                    "combine", "--run-dirs", *[
                        str(study_dir / f"{model}-graded") for model in ("teacher", "base", "fine_tuned")],
                    "--output-dir", str(root / "combined"),
                ]), 0)
            self.assertEqual(len(model_requests), count * 3)
            self.assertGreater(len(judge_requests), 0)
            self.assertLess(len(judge_requests), len(model_requests))
            self.assertTrue((study_dir / "comparison.csv").is_file())
            self.assertTrue((study_dir / "summary.json").is_file())
            self.assertFalse(list(study_dir.rglob("reviews.csv")))
            self.assertFalse((study_dir / "combined").exists())
            scores = read_json(root / "combined" / "scores.json")
            self.assertEqual(scores["scheduled_slots"], count * 3)
            self.assertEqual(scores["overall"]["usage_total"]["input_tokens"], count * 3 * 20)
            self.assertTrue(all(not row["confirmed_business_success"] for row in scores["rows"]))
            prepared = prepare_from_files(
                root / "combined" / "scores.json", ROOT / "configs" / "examples" / "cost-evaluation.json",
                root / "cost-input.json")
            self.assertEqual(prepared["evaluation_bridge"]["model_grading"]["judge_calls"], len(judge_requests))
            for variant in prepared["variants"].values():
                self.assertEqual(variant["usage_per_request"]["input_tokens"], 20)
                self.assertFalse(variant["quality"]["review_complete"])
                self.assertIsNone(variant["quality"]["successful_requests"])


if __name__ == "__main__":
    unittest.main()
