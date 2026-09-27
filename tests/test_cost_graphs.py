"""Automatic chart assumptions use the supplied costs, never invented unit rates."""

import csv
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from foundry_distillation_lab.reporting.costs import write_report
from foundry_distillation_lab.reporting.graphs import (
    adaptive_graph_plan, graph_charts, write_graph_summary,
)


ROOT = Path(__file__).resolve().parents[1]


def cost_input():
    variants = {}
    for name, rate, fixed, initial in (("teacher", 0.1, 0, 5), ("base", 0.05, 20, 10),
                                      ("fine_tuned", 0.01, 40, 100)):
        variants[name] = {
            "inference_mode": "per_request", "prices": {"per_request": rate},
            "variable_fees_per_request": {"agent": 0, "tools_logs_storage": 0},
            "monthly_fixed": {"model_hosting": fixed, "agent": 0, "tools_logs_storage": 0},
            "initial": {"generation": 0, "training": 0, "evaluation": 0, "other": initial},
            "quality": {"review_complete": False, "reviewed_requests": None,
                        "successful_requests": None, "quality_gate": "unreviewed"},
            "source": {"cost_basis": "public_retail_estimate_not_invoice"},
        }
    return {"schema_version": 1, "evidence_kind": "actual", "currency": "USD",
            "monthly_requests": 1000, "horizon_months": 12, "variants": variants,
            "estimate": {"pricing_basis": "Public rates; not invoice"}}


class CostGraphTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".test-runs").mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / ".test-runs")
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name) / "report"

    def render(self, data=None):
        data = deepcopy(data or cost_input())
        plan = adaptive_graph_plan(data)
        middle = plan["scenarios"][1]
        data.update(monthly_requests=middle["monthly_requests"], horizon_months=middle["horizon_months"])
        report = write_report(data, self.output, graph_plan=plan)
        write_graph_summary(report, self.output)
        return report

    def test_real_operating_crossovers_and_bounded_automatic_axes(self):
        source = cost_input()
        original = deepcopy(source)
        plan = adaptive_graph_plan(source)
        self.assertEqual(source, original)
        axis = plan["volume_axis"]
        self.assertAlmostEqual(axis["crossovers"]["base"], 400)
        self.assertAlmostEqual(axis["crossovers"]["fine_tuned"], 40 / 0.09)
        for name, cross in axis["crossovers"].items():
            self.assertIn(cross, axis["points"])
            teacher, student = source["variants"]["teacher"], source["variants"][name]
            self.assertAlmostEqual(teacher["prices"]["per_request"] * cross,
                                   student["prices"]["per_request"] * cross
                                   + student["monthly_fixed"]["model_hosting"])
        volumes = [scenario["monthly_requests"] for scenario in plan["scenarios"]]
        self.assertLess(volumes[0], max(axis["crossovers"].values()))
        self.assertGreater(volumes[-1], max(axis["crossovers"].values()))
        self.assertEqual(len(set(volumes)), 3)
        self.assertLessEqual(len(axis["points"]), 83)
        self.assertLessEqual(axis["maximum"], max(volumes) * 3)
        self.assertTrue(plan["assumptions"]["not_actual_user_usage"])

    def test_initial_inclusive_panels_formulas_and_exact_visible_intersections(self):
        report = self.render()
        plan = report["graph_plan"]
        charts = graph_charts(report)
        for kind in ("volume", "payback", "cumulative"):
            ET.fromstring(charts[kind][1])
        rows = charts["payback"][0]
        svg = charts["payback"][1]
        for scenario in plan["scenarios"]:
            self.assertIn(f'月間 {scenario["monthly_requests"]:,.0f} 件', svg)
            selected = [row for row in rows if row["scenario"] == scenario["id"]]
            self.assertEqual(selected[0]["month"], 0)
            self.assertLessEqual(len(selected), 83)
            for name, variant in scenario["variants"].items():
                self.assertEqual(selected[0][name], variant["initial_total"])
                for row in selected:
                    self.assertAlmostEqual(row[name], variant["initial_total"]
                                           + variant["monthly_cost"] * row["month"])
            for name, comparison in scenario["comparisons"].items():
                cross = comparison["cumulative_intersection_months"]
                if cross is not None and cross <= scenario["horizon_months"]:
                    row = next(row for row in selected if row["month"] == cross)
                    self.assertAlmostEqual(row["teacher"], row[name])
        self.assertEqual(json.loads((self.output / "graph-plan.json").read_text(encoding="utf-8")), plan)
        self.assertEqual(json.loads((self.output / "report.json").read_text(encoding="utf-8"))["graph_plan"], plan)
        with (self.output / "payback.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), len(rows))
        markdown = (self.output / "graphs.md").read_text(encoding="utf-8")
        self.assertIn("品質基準は未確認", markdown)
        self.assertNotIn("quality_gate=", markdown)
        self.assertNotIn(plan["selection_reason"], markdown)
        self.assertNotIn("Human decision required", markdown)
        self.assertIn("![", markdown)
        self.assertIn("中央シナリオ", markdown)
        for name, variant in report["variants"].items():
            self.assertEqual(charts["cumulative"][0][0][name], 0)
            for row in charts["cumulative"][0]:
                self.assertAlmostEqual(row[name], variant["monthly_cost"] * row["month"])
        self.assertIn("初期費用を除く", charts["cumulative"][1])
        self.assertNotIn("data-crossing=", charts["cumulative"][1])

    def test_operating_cumulative_remains_known_when_initial_cost_is_unknown(self):
        data = cost_input()
        data["variants"]["fine_tuned"]["initial"]["other"] = None
        report = self.render(data)
        charts = graph_charts(report)
        monthly = report["variants"]["fine_tuned"]["monthly_cost"]
        for row in charts["cumulative"][0]:
            self.assertEqual(row["fine_tuned"], monthly * row["month"])
        self.assertTrue(all(row["fine_tuned"] is None for row in charts["payback"][0]))

    def test_zero_savings_and_coincident_lines_are_not_manufactured_payback(self):
        data = cost_input()
        for name in ("base", "fine_tuned"):
            data["variants"][name] = deepcopy(data["variants"]["teacher"])
        report = self.render(data)
        self.assertEqual([s["monthly_requests"] for s in report["graph_plan"]["scenarios"]],
                         [100, 1000, 10000])
        self.assertIn("VOLUME ASSUMPTIONS", report["graph_plan"]["selection_reason"])
        for scenario in report["graph_plan"]["scenarios"]:
            self.assertIn("同一線", " ".join(scenario["notes"]))
            for comparison in scenario["comparisons"].values():
                self.assertEqual(comparison["payback_status"], "no_positive_monthly_savings")
                self.assertIsNone(comparison["payback_months"])
        svg = (self.output / "volume.svg").read_text(encoding="utf-8")
        for name in ("teacher", "base", "fine_tuned"):
            self.assertIn(f'data-variant="{name}"', svg)

    def test_missing_price_keeps_unknown_costs_and_omits_only_affected_line(self):
        data = cost_input()
        data["variants"]["fine_tuned"]["prices"]["per_request"] = None
        report = self.render(data)
        for scenario in report["graph_plan"]["scenarios"]:
            self.assertIsNone(scenario["variants"]["fine_tuned"]["monthly_cost"])
            self.assertEqual(scenario["comparisons"]["fine_tuned"]["payback_status"], "unknown_costs")
        svg = (self.output / "payback.svg").read_text(encoding="utf-8")
        self.assertNotIn('data-variant="fine_tuned"', svg)
        self.assertIn("学習済み (fine_tuned) — 不明", svg)

    def test_always_cheaper_has_no_positive_volume_intersection(self):
        data = cost_input()
        for name in ("base", "fine_tuned"):
            data["variants"][name]["monthly_fixed"]["model_hosting"] = 0
        plan = adaptive_graph_plan(data)
        self.assertEqual(plan["volume_axis"]["crossovers"], {"base": None, "fine_tuned": None})
        self.assertTrue(all(s["comparisons"]["fine_tuned"]["payback_months"] > 0
                            for s in plan["scenarios"]))

    def test_reverse_operating_crossing_is_not_a_scale_up_savings_claim(self):
        data = cost_input()
        data["variants"]["teacher"]["monthly_fixed"]["model_hosting"] = 100
        data["variants"]["base"]["prices"]["per_request"] = 0.2
        plan = adaptive_graph_plan(data)
        self.assertAlmostEqual(plan["volume_axis"]["crossovers"]["base"], 800)
        self.assertIsNone(plan["scenarios"][-1]["comparisons"]["base"]["payback_months"])

    def test_huge_payback_is_capped_but_not_reported_as_no_payback(self):
        data = cost_input()
        data["variants"]["fine_tuned"]["initial"]["other"] = 1e100
        report = self.render(data)
        scenario = report["graph_plan"]["scenarios"][-1]
        self.assertEqual(scenario["horizon_months"], 1200)
        self.assertEqual(scenario["comparisons"]["fine_tuned"]["payback_status"], "outside_horizon")
        self.assertGreater(scenario["comparisons"]["fine_tuned"]["payback_months"], 1200)
        self.assertIn("表示範囲外", (self.output / "payback.svg").read_text(encoding="utf-8"))

    def test_scenario_horizons_grow_independently(self):
        data = cost_input()
        data["variants"]["fine_tuned"]["initial"]["other"] = 100000
        plan = adaptive_graph_plan(data)
        horizons = [scenario["horizon_months"] for scenario in plan["scenarios"]]
        self.assertGreater(len(set(horizons)), 1)
        for scenario in plan["scenarios"]:
            for comparison in scenario["comparisons"].values():
                payback = comparison["payback_months"]
                if payback is not None and 0 < payback <= 800:
                    self.assertGreaterEqual(scenario["horizon_months"], payback * 1.5)

    def test_short_payback_uses_close_horizon_and_marks_exact_intersections(self):
        report = self.render()
        plan = report["graph_plan"]
        high = plan["scenarios"][-1]
        self.assertAlmostEqual(high["comparisons"]["fine_tuned"]["payback_months"], 95 / 140)
        self.assertEqual(high["horizon_months"], 2)
        self.assertEqual(plan["scenarios"][0]["horizon_months"], 12)
        svg = (self.output / "volume.svg").read_text(encoding="utf-8")
        root = ET.fromstring(svg)
        for name in ("base", "fine_tuned"):
            marker = root.find(f'.//*[@data-crossing="{name}"]')
            guide = root.find(f'.//*[@data-crossing-guide="{name}"]')
            self.assertIsNotNone(marker)
            self.assertIsNotNone(guide)
            expected_x = 110 + plan["volume_axis"]["crossovers"][name] / plan["volume_axis"]["maximum"] * 650
            self.assertAlmostEqual(float(marker.attrib["cx"]), expected_x, places=4)
        cumulative = (self.output / "payback.svg").read_text(encoding="utf-8")
        self.assertIn('data-crossing="fine_tuned"', cumulative)

    def test_ordinary_money_ticks_are_nice_and_comma_formatted(self):
        data = cost_input()
        for variant in data["variants"].values():
            variant["prices"]["per_request"] *= 100
            for key in variant["monthly_fixed"]:
                variant["monthly_fixed"][key] *= 100
        report = self.render(data)
        axis = report["graph_plan"]["volume_axis"]["cost_axis"]
        self.assertEqual(axis["maximum"], 50000)
        self.assertEqual(axis["ticks"], [0, 10000, 20000, 30000, 40000, 50000])
        svg = (self.output / "volume.svg").read_text(encoding="utf-8")
        self.assertIn(">50,000</text>", svg)
        self.assertNotIn("e+0", svg)

    def test_volume_notes_distinguish_known_no_crossing_unknown_and_incomparable(self):
        data = cost_input()
        data["variants"]["base"]["monthly_fixed"]["model_hosting"] = 0
        data["variants"]["fine_tuned"]["prices"]["per_request"] = None
        plan = adaptive_graph_plan(data)
        comparisons = plan["volume_axis"]["comparisons"]
        self.assertEqual(comparisons["base"]["status"], "student_cheaper_at_all_positive_volumes")
        self.assertNotIn("不明", comparisons["base"]["note"])
        self.assertEqual(comparisons["fine_tuned"]["status"], "unknown_costs")
        self.assertIn("不明", comparisons["fine_tuned"]["note"])
        data["evaluation_bridge"] = {"comparable": False, "hold_reasons": []}
        comparison = adaptive_graph_plan(data)["volume_axis"]["comparisons"]["base"]
        self.assertEqual(comparison["status"], "not_comparable")

    def test_tiny_money_labels_are_not_rounded_to_zero(self):
        data = cost_input()
        for variant in data["variants"].values():
            variant["prices"]["per_request"] *= 1e-12
            for key in variant["monthly_fixed"]:
                variant["monthly_fixed"][key] *= 1e-12
            for key in variant["initial"]:
                variant["initial"][key] *= 1e-12
        report = self.render(data)
        svg = graph_charts(report)["volume"][1]
        self.assertIn("e-", svg)
        self.assertGreater(report["variants"]["fine_tuned"]["monthly_cost"], 0)

    def test_huge_crossover_uses_bounded_points_and_readable_ticks(self):
        data = cost_input()
        for variant in data["variants"].values():
            variant["prices"]["per_request"] *= 1e-100
        report = self.render(data)
        self.assertGreater(report["graph_plan"]["volume_axis"]["maximum"], 1e100)
        self.assertLessEqual(len(report["graph_plan"]["volume_axis"]["points"]), 83)
        self.assertIn("e+", (self.output / "volume.svg").read_text(encoding="utf-8"))

    def test_incomparable_evidence_suppresses_inferred_intersections(self):
        data = cost_input()
        data["evaluation_bridge"] = {"comparable": False, "hold_reasons": ["Fixture cohorts differ"]}
        plan = adaptive_graph_plan(data)
        self.assertEqual(plan["volume_axis"]["crossovers"], {"base": None, "fine_tuned": None})
        self.assertEqual(plan["scenarios"][0]["comparisons"]["base"]["payback_status"], "not_comparable")

    def test_numeric_overflow_or_underflow_is_an_error_not_free_costs(self):
        data = cost_input()
        for variant in data["variants"].values():
            variant["prices"]["per_request"] = 1e308
        with self.assertRaisesRegex(ValueError, "finite"):
            adaptive_graph_plan(data)
        data = cost_input()
        raw = data["variants"]["teacher"]
        raw.update(inference_mode="tokens",
                   usage_per_request={"input_tokens": 1e-200, "cached_input_tokens": 0, "output_tokens": 0},
                   prices={"input_per_million": 1e-200, "cached_input_per_million": 0, "output_per_million": 0})
        with self.assertRaisesRegex(ValueError, "underflows"):
            adaptive_graph_plan(data)


if __name__ == "__main__":
    unittest.main()
