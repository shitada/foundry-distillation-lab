"""Deterministic offline tests; all written fixtures stay inside this repository."""

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from foundry_distillation_lab.reporting import build_report, write_report
from foundry_distillation_lab.reporting.costs import series_rows
from foundry_distillation_lab.reporting.bridge import prepare_cost_input, prepare_from_files


ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples" / "illustrative" / "cost-input.json").read_text(encoding="utf-8"))
        self.output = ROOT / "runs" / ("reporting-test-" + uuid.uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(self.output, ignore_errors=True))

    def test_intro_crossover_and_payback(self):
        report = build_report(self.data)
        comparison = report["comparisons"]["fine_tuned"]
        self.assertEqual(comparison["crossover_requests"], 500)
        self.assertEqual(comparison["monthly_savings"], 9000)
        self.assertEqual(comparison["payback_months"], 2)
        self.assertEqual(report["variants"]["teacher"]["monthly_cost"], 20000)
        self.assertEqual(report["variants"]["fine_tuned"]["monthly_cost"], 11000)
        self.assertEqual(series_rows(report, "payback")[2]["teacher"], 40000)
        self.assertEqual(series_rows(report, "payback")[2]["fine_tuned"], 40000)
        self.assertEqual(series_rows(report, "cumulative")[0]["fine_tuned"], 0)
        self.assertEqual(series_rows(report, "payback")[0]["fine_tuned"], 18000)

    def test_prices_are_configurable(self):
        self.data["variants"]["teacher"]["prices"]["per_request"] = 32
        self.assertEqual(build_report(self.data)["comparisons"]["fine_tuned"]["crossover_requests"], 300)

    def test_cached_input_is_not_double_counted(self):
        variant = self.data["variants"]["teacher"]
        variant["inference_mode"] = "tokens"
        variant["usage_per_request"] = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}
        variant["prices"] = {"input_per_million": 10, "cached_input_per_million": 2, "output_per_million": 20}
        self.assertAlmostEqual(build_report(self.data)["variants"]["teacher"]["inference_per_request"], .0108)
        variant["usage_per_request"]["cached_input_tokens"] = 1001
        with self.assertRaises(ValueError):
            build_report(self.data)

    def test_cost_components_and_missing_values(self):
        variant = self.data["variants"]["teacher"]
        variant["variable_fees_per_request"] = {"agent": 1, "tools_logs_storage": 2}
        variant["monthly_fixed"] = {"model_hosting": 100, "agent": 200, "tools_logs_storage": 300}
        self.assertEqual(build_report(self.data)["variants"]["teacher"]["monthly_cost"], 23600)
        del variant["monthly_fixed"]["agent"]
        report = build_report(self.data)
        self.assertIsNone(report["variants"]["teacher"]["monthly_cost"])
        self.assertEqual(report["comparisons"]["base"]["status"], "unknown_costs")
        self.assertEqual(report["decision_draft"]["decision"], "hold")

    def test_initial_unknown_does_not_hide_operating_crossover(self):
        self.data["variants"]["fine_tuned"]["initial"]["training"] = None
        result = build_report(self.data)["comparisons"]["fine_tuned"]
        self.assertEqual(result["crossover_requests"], 500)
        self.assertIsNone(result["payback_months"])

    def test_no_savings_has_no_payback(self):
        self.data["variants"]["fine_tuned"]["prices"]["per_request"] = 20
        result = build_report(self.data)["comparisons"]["fine_tuned"]
        self.assertIsNone(result["crossover_requests"])
        self.assertIsNone(result["payback_months"])
        self.data["variants"]["fine_tuned"]["prices"]["per_request"] = 30
        self.assertIsNone(build_report(self.data)["comparisons"]["fine_tuned"]["crossover_requests"])

    def test_zero_volume_and_equal_monthly_cost_have_no_payback(self):
        for volume in (0, 500):
            self.data["monthly_requests"] = volume
            report = build_report(self.data)
            self.assertIsNone(report["comparisons"]["fine_tuned"]["payback_months"])
            self.assertIsNone(report["variants"]["fine_tuned"]["projected_cost_per_reviewed_success"])

    def test_success_cost_requires_review_and_successes(self):
        quality = self.data["variants"]["teacher"]["quality"]
        quality.update(reviewed_requests=100, successful_requests=80)
        self.assertIsNone(build_report(self.data)["variants"]["teacher"]["projected_cost_per_reviewed_success"])
        quality["review_complete"] = True
        self.assertEqual(build_report(self.data)["variants"]["teacher"]["projected_cost_per_reviewed_success"], 25)
        quality["successful_requests"] = 0
        self.assertIsNone(build_report(self.data)["variants"]["teacher"]["projected_cost_per_reviewed_success"])
        quality["successful_requests"] = 101
        with self.assertRaises(ValueError):
            build_report(self.data)

    def test_invalid_numbers(self):
        for value in (-1, float("nan"), float("inf"), True, "20"):
            with self.subTest(value=value):
                data = copy.deepcopy(self.data)
                data["variants"]["teacher"]["prices"]["per_request"] = value
                with self.assertRaises(ValueError):
                    write_report(data, self.output)
                self.assertFalse(self.output.exists())
        self.data["horizon_months"] = 1.5
        with self.assertRaises(ValueError):
            build_report(self.data)

    def test_actual_template_remains_unknown(self):
        data = json.loads((ROOT / "configs" / "examples" / "cost-actual-template.json").read_text(encoding="utf-8"))
        report = build_report(data)
        self.assertEqual(report["decision_draft"]["decision"], "hold")
        self.assertTrue(all(v["monthly_cost"] is None for v in report["variants"].values()))

    def test_unknown_cache_usage_is_not_zero(self):
        variant = self.data["variants"]["teacher"]
        variant["inference_mode"] = "tokens"
        variant["usage_per_request"] = {"input_tokens": 1000, "output_tokens": 200}
        variant["prices"] = {"input_per_million": 10, "cached_input_per_million": 2, "output_per_million": 20}
        self.assertIsNone(build_report(self.data)["variants"]["teacher"]["inference_per_request"])

    def test_invalid_schema_and_counts(self):
        cases = []
        for path, value in (
                (("variants", "teacher", "initial", "other"), -1),
                (("variants", "teacher", "quality", "review_complete"), "true"),
                (("variants", "teacher", "quality", "reviewed_requests"), 1.5),
                (("variants", "teacher", "quality", "quality_gate"), "approved"),
                (("schema_version",), True),
                (("evidence_kind",), "measured"),
                (("currency",), ""),
                (("horizon_months",), 1201)):
            data = copy.deepcopy(self.data)
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    build_report(data)

    def test_no_incremental_initial_means_immediate_payback(self):
        self.assertEqual(build_report(self.data)["comparisons"]["base"]["payback_months"], 0)

    def test_mixed_conditions_and_actual_provenance_require_hold(self):
        self.data["evidence_kind"] = "actual"
        for name in ("teacher", "base", "fine_tuned"):
            self.data["variants"][name]["source"]["conditions_hash"] = name
        report = build_report(self.data)
        self.assertEqual(report["decision_draft"]["decision"], "hold")
        reasons = " ".join(report["decision_draft"]["reasons"])
        self.assertIn("provenance is incomplete", reasons)
        self.assertIn("conditions differ", reasons)

    def test_public_samples_match_generator(self):
        write_report(self.data, self.output)
        assets = ROOT / "examples" / "illustrative" / "assets"
        for generated in self.output.iterdir():
            self.assertEqual(generated.read_text(encoding="utf-8"),
                             (assets / generated.name).read_text(encoding="utf-8"), generated.name)
            if generated.suffix == ".svg":
                ET.parse(generated)

    def test_write_deterministic_outputs_and_refuse_overwrite(self):
        write_report(self.data, self.output)
        before = (self.output / "report.json").read_bytes()
        with self.assertRaises(FileExistsError):
            write_report(self.data, self.output)
        self.assertEqual(before, (self.output / "report.json").read_bytes())
        svg = (self.output / "volume.svg").read_text(encoding="utf-8")
        self.assertIn("ILLUSTRATIVE", svg)
        self.assertIn("stroke-dasharray", svg)
        self.assertIn("Requests / month", svg)
        self.assertIn("fine_tuned", (self.output / "costs.csv").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(before), build_report(self.data))

    def test_cli(self):
        command = [sys.executable, str(ROOT / "scripts" / "report.py"),
                   "--input", str(ROOT / "examples" / "illustrative" / "cost-input.json"),
                   "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 2)


class EvaluationCostBridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "configs" / "examples" / "cost-evaluation.json").read_text(encoding="utf-8"))
        for variant in self.config["variants"].values():
            variant["prices"] = {"input_per_million": 10, "cached_input_per_million": 2, "output_per_million": 20}
            variant["variable_fees_per_request"] = {"agent": 0, "tools_logs_storage": 0}
            variant["monthly_fixed"] = {"model_hosting": 0, "agent": 0, "tools_logs_storage": 0}
            variant["initial"] = {"generation": 0, "training": 0, "evaluation": 0, "other": 0}
        self.evaluation = {
            "schema": "retail-evaluation-report-v1", "mode": "e2e",
            "evidence_kind": "test_observation_not_production",
            "rows": [
                {"case_id": case, "model": name, "status": "confirmed_success",
                 "confirmed_business_success": True, "text_review": "confirmed_success",
                 "deterministic_checks_passed": True,
                 "origin": "local_tool_loop",
                 "provenance": {"runtime": {"kind": "local_direct_model"},
                                "evidence_kind": "test_observation_not_production"},
                 "usage": {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": inp / 10}}
                for name in ("teacher", "base", "fine_tuned") for case, inp in (("a", 100), ("b", 300))
            ],
        }
        for row in self.evaluation["rows"]:
            row["case_sha256"] = hashlib.sha256(row["case_id"].encode()).hexdigest()
            row["tool_contract_sha256"] = hashlib.sha256(b"same tool contract").hexdigest()
            row["evidence_sha256"] = hashlib.sha256((row["case_id"] + row["model"]).encode()).hexdigest()
            row["review"] = {"decision": "confirmed_success", "reviewer": "synthetic-test",
                             "reviewed_at": "2026-09-19T00:00:00Z", "notes": "Explicit test fixture, not real review.",
                             "evidence_sha256": row["evidence_sha256"]}
        self.refresh_summaries()
        self.output = ROOT / "runs" / ("report-bridge-test-" + uuid.uuid4().hex)
        self.output.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.output, ignore_errors=True))

    def refresh_summaries(self):
        def summary(rows):
            keys = ("input_tokens", "cached_input_tokens", "output_tokens")
            known = {key: [r["usage"][key] for r in rows if r["usage"][key] is not None]
                     for key in keys}
            return {
                "denominator": len(rows),
                "status_counts": {key: sum(r["status"] == key for r in rows) for key in
                                  ("technical_failure", "quality_failure", "review_pending", "confirmed_success")},
                "confirmed_business_successes": sum(r["confirmed_business_success"] for r in rows),
                "usage_total": {key: sum(values) if len(values) == len(rows) else None for key, values in known.items()},
                "usage_known_subtotal": {key: sum(values) if values else None for key, values in known.items()},
                "usage_reported_n": {key: len(values) for key, values in known.items()},
            }
        rows = self.evaluation["rows"]
        self.evaluation["scheduled_slots"] = len(rows)
        self.evaluation["overall"] = summary(rows)
        self.evaluation["per_model"] = {
            name: summary([r for r in rows if r["model"] == name])
            for name in ("teacher", "base", "fine_tuned")}

    def prepared(self):
        return prepare_cost_input(self.evaluation, self.config, "a" * 64)

    def test_balanced_cohort_averages_all_scheduled_slots(self):
        prepared = self.prepared()
        teacher = prepared["variants"]["teacher"]
        self.assertEqual(teacher["usage_per_request"]["input_tokens"], 200)
        self.assertEqual(teacher["quality"]["reviewed_requests"], 2)
        self.assertEqual(teacher["quality"]["successful_requests"], 2)
        self.assertTrue(teacher["quality"]["review_complete"])
        self.assertEqual(teacher["source"]["projection_denominator"], 2)
        self.assertTrue(prepared["evaluation_bridge"]["same_cohort"])
        self.assertFalse(prepared["evaluation_bridge"]["production_evidence"])
        self.assertIsNone(prepared["evaluation_bridge"]["observed_category_mix"])
        self.assertEqual(build_report(prepared)["decision_draft"]["decision"], "hold")

    def test_missing_usage_preserves_subtotal_without_zero_filling(self):
        self.evaluation["rows"][0]["usage"]["input_tokens"] = None
        self.refresh_summaries()
        prepared = self.prepared()
        self.assertIsNone(prepared["variants"]["teacher"]["usage_per_request"]["input_tokens"])
        self.assertEqual(prepared["evaluation_bridge"]["per_model"]["teacher"]["usage_known_subtotal"]["input_tokens"], 300)
        self.assertIsNone(build_report(prepared)["variants"]["teacher"]["monthly_cost"])

    def test_unknown_outcomes_do_not_get_projected_success_cost(self):
        self.evaluation["rows"][0].update(status="review_pending", confirmed_business_success=False, text_review="unreviewed")
        self.refresh_summaries()
        prepared = self.prepared()
        self.assertFalse(prepared["variants"]["teacher"]["quality"]["review_complete"])
        self.assertIsNone(build_report(prepared)["variants"]["teacher"]["projected_cost_per_reviewed_success"])

    def test_model_grading_is_retained_but_not_human_confirmation(self):
        for row in self.evaluation["rows"]:
            row.update(status="review_pending", confirmed_business_success=False,
                       text_review="unreviewed", review=None,
                       automatic_decision="success", assessment_source="model")
            row["model_review"] = {
                "decision": "success", "reason": "Synthetic model grading fixture",
                "usage": {"input_tokens": 900, "output_tokens": 100, "cached_input_tokens": 0},
                "evidence_sha256": row["evidence_sha256"],
            }
        grading = {"source": "synthetic_test", "actual_cost": None,
                   "usage": {"input_tokens": 5400, "output_tokens": 600, "cached_input_tokens": 0}}
        self.evaluation["grading"] = grading
        self.refresh_summaries()
        prepared = self.prepared()
        self.assertEqual(prepared["evaluation_bridge"]["model_grading"], grading)
        self.assertIn("Model grading is provisional", " ".join(prepared["evaluation_bridge"]["hold_reasons"]))
        for variant in prepared["variants"].values():
            self.assertFalse(variant["quality"]["review_complete"])
            self.assertIsNone(variant["quality"]["successful_requests"])
            self.assertEqual(variant["usage_per_request"]["input_tokens"], 200)
        audit = prepared["evaluation_bridge"]["per_model"]["teacher"]["row_provenance"][0]
        self.assertEqual(audit["model_review"], self.evaluation["rows"][0]["model_review"])
        self.assertEqual(audit["automatic_decision"], "success")
        self.assertEqual(audit["assessment_source"], "model")
        report = build_report(prepared)
        self.assertIsNone(report["variants"]["teacher"]["projected_cost_per_reviewed_success"])
        self.assertEqual(report["decision_draft"]["decision"], "hold")

    def test_reviewed_failure_and_success_use_all_reviewed_outcomes(self):
        self.evaluation["rows"][0].update(status="quality_failure", confirmed_business_success=False,
                                         text_review="quality_failure")
        self.evaluation["rows"][0]["review"]["decision"] = "quality_failure"
        self.refresh_summaries()
        prepared = self.prepared()
        self.assertTrue(prepared["variants"]["teacher"]["quality"]["review_complete"])
        self.assertEqual(prepared["variants"]["teacher"]["quality"]["successful_requests"], 1)
        self.assertEqual(build_report(prepared)["variants"]["teacher"]["reviewed_success_rate"], .5)

    def test_projection_requires_explicit_assumptions(self):
        del self.config["evaluation_projection"]
        with self.assertRaises(ValueError):
            self.prepared()

    def test_invalid_numeric_usage_rejected(self):
        self.evaluation["rows"][0]["usage"]["input_tokens"] = float("nan")
        with self.assertRaises(ValueError):
            self.prepared()

    def test_runtime_mismatch_blocks_comparison(self):
        self.evaluation["rows"][2]["provenance"]["runtime"]["kind"] = "foundry_hosted_agent"
        report = build_report(self.prepared())
        self.assertFalse(report["evaluation_bridge"]["runtime_matches"])
        self.assertEqual(report["comparisons"]["base"]["status"], "evaluation_evidence_not_comparable")
        self.assertIsNone(report["comparisons"]["base"]["crossover_requests"])

    def test_absent_runtime_is_not_inferred_from_configuration(self):
        for row in self.evaluation["rows"]:
            del row["provenance"]
            del row["origin"]
        prepared = self.prepared()
        self.assertEqual(prepared["variants"]["teacher"]["source"]["evaluation_runtime"], "unknown")
        self.assertFalse(prepared["evaluation_bridge"]["comparable"])

    def test_legacy_reports_without_identity_hashes_stay_on_hold(self):
        for row in self.evaluation["rows"]:
            for key in ("case_sha256", "tool_contract_sha256", "evidence_sha256"):
                del row[key]
        prepared = self.prepared()
        self.assertFalse(prepared["evaluation_bridge"]["identity_complete"])
        self.assertFalse(prepared["evaluation_bridge"]["comparable"])
        self.assertEqual(prepared["evidence_kind"], "illustrative")
        self.assertIsNone(prepared["variants"]["teacher"]["quality"]["successful_requests"])

    def test_matching_case_ids_do_not_hide_changed_tool_contract(self):
        self.evaluation["rows"][2]["tool_contract_sha256"] = "b" * 64
        prepared = self.prepared()
        self.assertTrue(prepared["evaluation_bridge"]["same_cohort"])
        self.assertFalse(prepared["evaluation_bridge"]["same_conditions"])
        self.assertIsNone(build_report(prepared)["comparisons"]["base"]["crossover_requests"])

    def test_unbound_or_historical_review_does_not_confirm_success(self):
        row = self.evaluation["rows"][0]
        row["review"]["evidence_sha256"] = "0" * 64
        prepared = self.prepared()
        self.assertFalse(prepared["variants"]["teacher"]["quality"]["review_complete"])
        self.assertIsNone(prepared["variants"]["teacher"]["quality"]["successful_requests"])
        row["source_review"] = row.pop("review")
        self.assertFalse(self.prepared()["variants"]["teacher"]["quality"]["review_complete"])

    def test_review_cannot_override_failed_deterministic_checks(self):
        self.evaluation["rows"][0]["deterministic_checks_passed"] = False
        prepared = self.prepared()
        self.assertFalse(prepared["variants"]["teacher"]["quality"]["review_complete"])
        self.assertIsNone(prepared["variants"]["teacher"]["quality"]["successful_requests"])
        self.assertIsNone(build_report(prepared)["variants"]["teacher"]["projected_cost_per_reviewed_success"])

    def test_injected_local_origin_without_provenance_is_unknown(self):
        for row in self.evaluation["rows"]:
            row["evidence_kind"] = row["provenance"]["evidence_kind"]
            row["provenance"] = None
        prepared = self.prepared()
        self.assertEqual(prepared["variants"]["teacher"]["source"]["evaluation_runtime"], "unknown")
        self.assertFalse(prepared["evaluation_bridge"]["runtime_matches"])

    def test_unequal_case_balance_holds_comparison(self):
        self.evaluation["rows"][2]["case_id"] = "different-case"
        prepared = self.prepared()
        self.assertFalse(prepared["evaluation_bridge"]["same_cohort"])
        self.assertIsNone(build_report(prepared)["comparisons"]["fine_tuned"]["payback_months"])

    def test_next_action_rejected_as_business_cost_source(self):
        self.evaluation["mode"] = "next-action"
        for row in self.evaluation["rows"]:
            row["confirmed_business_success"] = False
        self.refresh_summaries()
        with self.assertRaisesRegex(ValueError, "requires mode=e2e"):
            self.prepared()

    def test_synthetic_evidence_cannot_be_promoted_by_actual_config(self):
        self.evaluation["evidence_kind"] = "synthetic_illustration_not_measurement"
        prepared = self.prepared()
        self.assertEqual(prepared["evidence_kind"], "illustrative")
        self.assertFalse(prepared["evaluation_bridge"]["production_evidence"])

    def test_row_synthetic_provenance_overrides_mislabeled_root(self):
        self.evaluation["evidence_kind"] = "measured"
        self.evaluation["rows"][0]["provenance"]["source_evidence_kind"] = "measured"
        self.evaluation["rows"][0]["provenance"]["evidence_kind"] = "synthetic_illustration_not_measurement"
        self.assertEqual(self.prepared()["evidence_kind"], "illustrative")

    def test_row_unknown_provenance_cannot_be_promoted(self):
        self.evaluation["evidence_kind"] = "measured"
        self.evaluation["rows"][0]["provenance"]["evidence_kind"] = "unknown"
        prepared = self.prepared()
        self.assertEqual(prepared["evidence_kind"], "illustrative")
        self.assertIn("unknown evidence provenance", " ".join(prepared["evaluation_bridge"]["hold_reasons"]))

    def test_inconsistent_totals_or_denominator_rejected(self):
        self.evaluation["per_model"]["teacher"]["usage_total"]["input_tokens"] = 999
        with self.assertRaises(ValueError):
            self.prepared()
        self.refresh_summaries()
        self.evaluation["per_model"]["base"]["denominator"] = 1
        with self.assertRaises(ValueError):
            self.prepared()

    def test_source_and_config_bytes_are_bound_and_output_immutable(self):
        source = self.output / "evaluation.json"
        config = self.output / "config.json"
        source.write_text(json.dumps(self.evaluation), encoding="utf-8")
        config.write_text(json.dumps(self.config), encoding="utf-8")
        destination = self.output / "cost-input.json"
        prepared = prepare_from_files(source, config, destination)
        self.assertEqual(prepared["evaluation_bridge"]["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(prepared["evaluation_bridge"]["config_sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            prepare_from_files(source, config, destination)
        self.assertEqual(before, destination.read_bytes())

    def test_actual_evaluator_to_cost_cli_without_network(self):
        evaluation = self.output / "evaluation.json"
        prepared = self.output / "cost-input.json"
        commands = [
            [sys.executable, str(ROOT / "scripts" / "evaluate.py"), "--mode", "e2e",
             "--input", str(ROOT / "data" / "samples" / "evaluation-e2e.json"), "--output", str(evaluation)],
            [sys.executable, str(ROOT / "scripts" / "report.py"), "--prepare-evaluation",
             "--input", str(evaluation), "--config", str(ROOT / "configs" / "examples" / "cost-evaluation.json"),
             "--output", str(prepared)],
            [sys.executable, str(ROOT / "scripts" / "report.py"), "--input", str(prepared),
             "--output", str(self.output / "report")],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.output / "report" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["evidence_kind"], "illustrative")
        self.assertEqual(report["decision_draft"]["decision"], "hold")
        self.assertFalse(report["evaluation_bridge"]["production_evidence"])
        self.assertIn("NOT PRODUCTION", (self.output / "report" / "volume.svg").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
