"""Price estimates use public-rate fixtures, never Azure billing or live models."""

from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from foundry_distillation_lab.datasets.prepare import prepare
from foundry_distillation_lab.evaluation.cases import prepare_cases
from foundry_distillation_lab.evaluation.scoring import prepare_next_actions
from foundry_distillation_lab.evaluation.study import study
from foundry_distillation_lab.evaluation.workflow import run_evaluation
from foundry_distillation_lab.io import read_json, read_jsonl, sha256, write_json
from foundry_distillation_lab.reporting.evidence import discover_evidence
from foundry_distillation_lab.reporting.estimate import estimate, inference_cost, plan_config
from foundry_distillation_lab.retail import RetailSession
from foundry_distillation_lab.training.jobs import start, status


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 27, tzinfo=timezone.utc)
USAGE = {"input_tokens": 20, "output_tokens": 8, "cached_input_tokens": 0}


class Prices:
    def __init__(self):
        self.calls = []
        self.missing_hosting = False

    def deployment(self, endpoint, target):
        self.calls.append((endpoint, target))
        return {"model": target, "version": "2026-01-01", "sku": "GlobalStandard",
                "region": "eastus2", "resource_id": f"fixture:{target}",
                "created_at": "2026-09-26T00:00:00Z"}

    def account(self, endpoint):
        return {"region": "eastus2", "resource_id": "fixture:account"}

    def rates(self, identity, *, training_type=None):
        return {"input_per_million": 1, "output_per_million": 2, "cached_input_per_million": 0.5,
                "hosting_per_hour": (None if self.missing_hosting else 0.5)
                if identity["model"] == "fine" else 0,
                "training_per_million": 2, "sources": {"fixture": True}, "missing": []}


class CostEstimateTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".test-runs").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".test-runs")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = {"additional_initial_cost": 2}
        write_json(self.root / "plan.json", self.plan)
        retail = RetailSession()
        write_json(self.root / "cases.json", {
            "schema": "retail-evaluation-input-v1", "models": ["teacher", "base", "fine_tuned"],
            "records": [], "tools": retail.tools, "cases": [{
                "case_id": "fixture", "user_input": "注文番号を確認したいです。",
                "system_prompt": retail.system_prompt,
                "expected": {"initial_tools": [], "required_calls": [], "allowed_mutations": [],
                             "final_state": {"terminal": "answer_only", "submissions": []}},
            }],
        })
        write_json(self.root / "evaluation.json", {
            "base_url": "https://example.invalid/openai/v1/",
            "targets": {"teacher": "teacher", "base": "base", "fine_tuned": "fine"},
            "max_completion_tokens": 1024, "timeout_seconds": 60, "max_model_calls": 12, "max_tool_calls": 24,
        })
        write_json(self.root / "grading.json", {
            "base_url": "https://example.invalid/openai/v1/", "deployment": "grader",
            "max_completion_tokens": 1024, "timeout_seconds": 60,
        })
        study(self.root / "cases.json", self.root / "evaluation.json", self.root / "grading.json",
              self.root / "e2e",
              invoke=Mock(return_value={"message": {"content": "注文番号を教えてください。"}, "usage": USAGE}),
              judge=Mock(return_value={
                  "message": {"content": '{"decision":"success","reason":"架空の採点です。"}'},
                  "finish_reason": "stop", "response_model": "grader",
                  "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}}))
        self.inventory = {
            "study_dir": str(self.root / "e2e"), "diagnostics": [], "data_dir": str(self.root / "data"),
            "generation": {"known": True}, "calls": [],
            "training": [{"path": "training-fixture", "project_endpoint": "https://example.invalid/project",
                          "model": "base", "training_type": "GlobalStandard",
                          "trained_tokens": 10000, "job_id": "job-fixture", "status": "succeeded"}],
        }
        for label, target in (("teacher", "teacher"), ("base", "base"), ("fine_tuned", "fine")):
            self.inventory["calls"].append({
                "kind": "evaluation", "variant": label, "endpoint": "https://example.invalid/openai/v1/",
                "target": target, "usage": USAGE, "source": f"fixture:{label}"})
            self.inventory["calls"].append({
                "kind": "grading", "variant": label, "endpoint": "https://example.invalid/openai/v1/",
                "target": "grader", "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0},
                "source": f"fixture:{label}-grading"})
        self.inventory["calls"].append({
            "kind": "generation", "variant": "fine_tuned", "endpoint": "https://example.invalid/openai/v1/",
            "target": "teacher", "usage": USAGE, "source": "fixture:generation"})

    def run_estimate(self, provider=None):
        return estimate(self.root / "plan.json", self.root, self.root / "cost-report",
                        pricing=provider or Prices(), discover=lambda _: deepcopy(self.inventory), as_of=AS_OF)

    def test_exact_costs_and_outputs_without_manual_unit_price_inputs(self):
        result = self.run_estimate()
        self.assertTrue(result["complete"])
        folder = Path(result["output_dir"])
        report = result["report"]
        fine = report["variants"]["fine_tuned"]
        self.assertEqual(fine["monthly_fixed"], 365)
        self.assertAlmostEqual(fine["initial_components"]["training"], 0.02)
        self.assertAlmostEqual(fine["initial_components"]["generation"], 0.000036)
        self.assertAlmostEqual(fine["initial_components"]["evaluation"], 12.000176)
        self.assertAlmostEqual(fine["initial_total"], 14.020212)
        self.assertAlmostEqual(fine["monthly_cost"], 365.036)
        self.assertIsNone(fine["projected_cost_per_reviewed_success"])
        self.assertEqual(report["decision_draft"]["decision"], "hold")
        self.assertEqual(report["estimate"]["hours_per_month"], 730)
        self.assertEqual(report["estimate"]["currency"], "USD")
        for name in ("costs.csv", "report.json", "volume.svg", "cumulative.svg", "payback.svg",
                     "inventory.json", "prices.json", "cost-ledger.json", "estimate.md",
                     "graphs.md", "graph-plan.json"):
            self.assertTrue((folder / name).is_file(), name)
        self.assertIn("PUBLIC RETAIL ESTIMATE", (folder / "volume.svg").read_text(encoding="utf-8"))
        self.assertEqual(read_json(folder / "report.json")["estimate"]["actual_invoice_cost"], None)
        self.assertEqual(read_json(folder / "report.json")["graph_plan"], read_json(folder / "graph-plan.json"))

    def test_missing_rate_and_usage_stay_unknown_but_render_partial_report(self):
        provider = Prices()
        provider.missing_hosting = True
        self.inventory["calls"][0]["usage"] = None
        self.inventory["generation"]["known"] = False
        result = self.run_estimate(provider)
        self.assertFalse(result["complete"])
        fine = result["report"]["variants"]["fine_tuned"]
        self.assertIsNone(fine["monthly_fixed"])
        self.assertIsNone(fine["initial_total"])
        self.assertIsNone(result["report"]["comparisons"]["fine_tuned"]["payback_months"])
        self.assertTrue(result["report"]["estimate"]["diagnostics"])

    def test_metadata_failure_is_explicit_and_never_defaults_to_a_region(self):
        provider = Prices()
        provider.deployment = Mock(side_effect=RuntimeError("not authenticated"))
        result = self.run_estimate(provider)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["report"]["variants"]["base"]["source"]["region"])
        self.assertIn("not authenticated", (Path(result["output_dir"]) / "estimate.md").read_text(encoding="utf-8"))

    def test_rerun_does_not_overwrite_existing_cost_evidence(self):
        first = self.run_estimate()
        content = (Path(first["output_dir"]) / "report.json").read_bytes()
        second = self.run_estimate()
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertEqual((Path(first["output_dir"]) / "report.json").read_bytes(), content)

    def test_invalid_user_plan_fails_before_metadata_lookup(self):
        for key, value in (("monthly_requests", 1000), ("horizon_months", 12),
                           ("additional_costs", {}), ("graph_range", 100), ("volumes", [100, 1000]),
                           ("additional_initial_cost", True), ("additional_monthly_cost", None),
                           ("additional_initial_cost", float("inf")), ("additional_monthly_cost", float("nan")),
                           ("additional_monthly_cost", -1), ("hosting_hours_per_day", -1),
                           ("hosting_hours_per_day", 24.1), ("hosting_hours_per_day", False)):
            invalid = deepcopy(self.plan)
            invalid[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                plan_config(invalid)

    def test_optional_plan_merges_defaults_and_explicit_missing_file_fails(self):
        self.assertEqual(plan_config({}), {"additional_initial_cost": 0, "additional_monthly_cost": 0,
                                          "hosting_hours_per_day": 24})
        self.assertEqual(plan_config({"hosting_hours_per_day": 0})["additional_initial_cost"], 0)
        provider = Prices()
        with self.assertRaises(OSError):
            estimate(self.root / "missing.json", self.root, self.root / "missing",
                     pricing=provider, discover=lambda _: self.inventory)
        self.assertEqual(provider.calls, [])

    def test_hosting_schedule_only_changes_future_cost_and_extra_costs_only_fine_tuned(self):
        original = self.run_estimate()
        self.plan.update(hosting_hours_per_day=8, additional_initial_cost=5, additional_monthly_cost=3)
        (self.root / "plan.json").unlink()
        write_json(self.root / "plan.json", self.plan)
        result = self.run_estimate()
        variants = result["report"]["variants"]
        fine = variants["fine_tuned"]
        self.assertAlmostEqual(fine["monthly_fixed"], 0.5 * (365 / 12) * 8 + 3)
        graph_summary = (Path(result["output_dir"]) / "graphs.md").read_text(encoding="utf-8")
        self.assertIn("1日 8 時間", graph_summary)
        self.assertIn("追加初期費用: 5 USD", graph_summary)
        self.assertIn("追加月額費用: 3 USD / 月", graph_summary)
        self.assertIn("未計上", graph_summary)
        self.assertEqual(fine["initial_components"]["other"], 5)
        for name in ("teacher", "base"):
            self.assertEqual(variants[name]["initial_components"]["other"], 0)
            self.assertEqual(variants[name]["monthly_fixed_components"]["tools_logs_storage"], 0)
        for name in ("teacher", "base", "fine_tuned"):
            self.assertEqual(variants[name]["initial_components"]["evaluation"],
                             original["report"]["variants"][name]["initial_components"]["evaluation"])
        old = read_json(Path(original["output_dir"]) / "cost-ledger.json")["items"]
        new = read_json(Path(result["output_dir"]) / "cost-ledger.json")["items"]
        self.assertEqual([r for r in old if r["kind"] == "experiment_hosting"],
                         [r for r in new if r["kind"] == "experiment_hosting"])
        self.plan["hosting_hours_per_day"] = 0
        (self.root / "plan.json").unlink()
        write_json(self.root / "plan.json", self.plan)
        zero = self.run_estimate()["report"]["variants"]["fine_tuned"]
        self.assertEqual(zero["monthly_fixed_components"]["model_hosting"], 0)
        provider = Prices()
        provider.missing_hosting = True
        unknown = self.run_estimate(provider)["report"]["variants"]["fine_tuned"]
        self.assertIsNone(unknown["monthly_fixed_components"]["model_hosting"])

    def test_no_config_and_empty_config_produce_full_graph_package(self):
        no_config = estimate(None, self.root, self.root / "defaults", pricing=Prices(),
                             discover=lambda _: deepcopy(self.inventory), as_of=AS_OF)
        (self.root / "plan.json").unlink()
        write_json(self.root / "plan.json", {})
        empty_config = self.run_estimate()
        self.assertEqual(no_config["report"]["graph_plan"], empty_config["report"]["graph_plan"])
        self.assertIsNone(no_config["report"]["estimate"]["plan_sha256"])
        self.assertEqual(no_config["report"]["estimate"]["plan_source"], "defaults")
        self.assertEqual(no_config["report"]["variants"]["fine_tuned"]["initial_components"]["other"], 0)
        self.assertTrue((Path(no_config["output_dir"]) / "graphs.md").is_file())

    def test_cli_combines_fetches_and_writes_reports_with_one_command(self):
        spec = importlib.util.spec_from_file_location("cost_report_cli", ROOT / "scripts" / "report.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = StringIO()
        with patch("foundry_distillation_lab.reporting.estimate.AzurePricing", return_value=Prices()), \
                patch("foundry_distillation_lab.reporting.estimate.discover_evidence",
                      return_value=self.inventory), redirect_stdout(output):
            code = cli.main(["--estimate", "--config", str(self.root / "plan.json"),
                             "--runs-dir", str(self.root), "--output", str(self.root / "cli-cost")])
        self.assertEqual(code, 0)
        self.assertIn("not invoiced cost", output.getvalue())
        self.assertTrue((self.root / "cli-cost" / "costs.csv").exists())
        self.assertTrue((self.root / "cli-cost" / "combined" / "scores.json").exists())
        with patch("foundry_distillation_lab.reporting.estimate.AzurePricing", return_value=Prices()), \
                patch("foundry_distillation_lab.reporting.estimate.discover_evidence",
                      return_value=self.inventory), redirect_stdout(output):
            self.assertEqual(cli.main(["--estimate", "--runs-dir", str(self.root),
                                       "--output", str(self.root / "cli-no-config")]), 0)
        self.assertTrue((self.root / "cli-no-config" / "graphs.md").is_file())
        with self.assertRaises(SystemExit) as error:
            cli.main(["--prepare-evaluation", "--input", str(self.root / "cases.json"),
                      "--output", str(self.root / "offline")])
        self.assertEqual(error.exception.code, 2)

    def test_corrupt_linked_evidence_cannot_leave_numeric_initial_totals(self):
        self.inventory.update(evaluation_complete=False, training_complete=False)
        result = self.run_estimate()
        self.assertFalse(result["complete"])
        for variant in result["report"]["variants"].values():
            self.assertIsNone(variant["initial_total"])

    def test_changed_deployment_identity_does_not_price_old_calls_as_current_model(self):
        self.inventory["calls"][0]["response_model"] = "different-model-version"
        result = self.run_estimate()
        self.assertFalse(result["complete"])
        self.assertIsNone(result["report"]["variants"]["teacher"]["initial_total"])
        self.assertIn("differs from this recorded call", str(result["report"]["estimate"]["diagnostics"]))

    def test_unknown_cache_usage_is_not_free_and_zero_usage_does_not_need_price(self):
        self.assertIsNone(inference_cost({"input_tokens": 20, "output_tokens": 8},
                                        Prices().rates({"model": "base"})))
        self.assertEqual(inference_cost({"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}, {}), 0)
        self.assertAlmostEqual(inference_cost(
            {"input_tokens": 20.5, "output_tokens": 8.5, "cached_input_tokens": 0.0},
            Prices().rates({"model": "base"}), averaged=True), 0.0000375)

    def test_standard_chapter_artifacts_are_discovered_and_priced(self):
        data = self.root / "data"
        prepare(ROOT / "data" / "samples" / "traces.jsonl", data)
        next_input = self.root / "next-action-input.json"
        write_json(next_input, prepare_next_actions(read_jsonl(data / "next-actions.jsonl"),
                                                    sha256(data / "next-actions.jsonl")))
        invoke = Mock(return_value={"message": {"content": "注文番号を教えてください。"}, "usage": USAGE})
        chapter6_config = read_json(self.root / "evaluation.json")
        del chapter6_config["targets"]["teacher"]
        write_json(self.root / "chapter6-config.json", chapter6_config)
        for model in ("base", "fine_tuned"):
            run_evaluation(next_input, self.root / "chapter6-config.json", "next-action", model,
                           self.root / f"chapter6-{model}", invoke=invoke)
        write_json(self.root / "training-config.json", {
            "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
            "model": "base", "training_type": "Standard", "hyperparameters": {"n_epochs": 2}})

        class Training:
            def upload(self, name, raw):
                return {"id": "file-" + name.replace(".", "-"), "status": "processed"}

            def submit(self, payload):
                return {"id": "job-fixture", "status": "succeeded", "trained_tokens": 10000,
                        "model": "base", "fine_tuned_model": "fine"}

            def status(self, kind, identifier):
                return {"id": identifier, "status": "processed" if kind == "file" else "succeeded",
                        "trained_tokens": 10000, "fine_tuned_model": "fine"}

        start(data / "train.jsonl", data / "validation.jsonl", self.root / "training-config.json",
              self.root / "training", transport=Training())
        status(self.root / "training", transport=Training())
        status(self.root / "training", transport=Training())
        prepare_cases(data, self.root / "chapter7-input.json")
        study(self.root / "chapter7-input.json", self.root / "evaluation.json", self.root / "grading.json",
              self.root / "real-study", invoke=invoke, judge=Mock(return_value={
                  "message": {"content": '{"decision":"success","reason":"架空の採点です。"}'},
                  "finish_reason": "stop", "response_model": "grader",
                  "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}}))
        inventory = discover_evidence(self.root)
        self.assertEqual(Path(inventory["study_dir"]), self.root / "real-study")
        self.assertTrue(inventory["generation"]["known"])
        self.assertEqual(len(inventory["training"]), 1, "Status polls must not multiply training cost")
        self.assertEqual(inventory["training"][0]["trained_tokens"], 10000)
        self.assertTrue(any("chapter6-base" in item["source"] for item in inventory["calls"]))
        self.assertTrue(any("chapter6-fine_tuned" in item["source"] for item in inventory["calls"]))
        result = estimate(self.root / "plan.json", self.root, self.root / "automatic-cost",
                          pricing=Prices(), as_of=AS_OF)
        self.assertEqual(result["report"]["variants"]["fine_tuned"]["initial_components"]["generation"], 0)
        self.assertAlmostEqual(result["report"]["variants"]["fine_tuned"]["initial_components"]["training"], .02)
        self.assertTrue((Path(result["output_dir"]) / "costs.csv").is_file())


if __name__ == "__main__":
    unittest.main()
