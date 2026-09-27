"""Graph-first command integration using recorded study and public-price fixtures."""

from contextlib import redirect_stdout
import csv
import importlib.util
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.reporting.estimate import estimate
import test_cost_estimate as fixtures


ROOT = Path(__file__).resolve().parents[1]


class GraphEstimateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CostEstimateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def estimate(self, config, name):
        return estimate(config, self.root, self.root / name, pricing=fixtures.Prices(),
                        discover=lambda _: self.fixture.inventory, as_of=fixtures.AS_OF)

    def test_graph_generation_needs_no_plan_file_or_user_volume(self):
        spec = importlib.util.spec_from_file_location("graph_report_cli", ROOT / "scripts" / "report.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = StringIO()
        with patch("foundry_distillation_lab.reporting.estimate.AzurePricing", return_value=fixtures.Prices()), \
                patch("foundry_distillation_lab.reporting.estimate.discover_evidence",
                      return_value=self.fixture.inventory), redirect_stdout(output):
            result = cli.main(["--estimate", "--runs-dir", str(self.root),
                               "--output", str(self.root / "graphs")])
        self.assertEqual(result, 0, output.getvalue())
        folder = self.root / "graphs"
        plan = read_json(folder / "plan.json")
        self.assertEqual(set(plan), {"additional_initial_cost", "additional_monthly_cost",
                                     "hosting_hours_per_day"})
        self.assertEqual(plan["hosting_hours_per_day"], 24)
        for name in ("graphs.md", "graph-plan.json", "report.json", "estimate.json",
                     "volume.svg", "payback.svg", "cumulative.svg", "costs.csv"):
            self.assertTrue((folder / name).is_file(), name)
        for name in ("volume.svg", "payback.svg"):
            root = ET.parse(folder / name).getroot()
            self.assertEqual(root.get("role"), "img")
            self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}desc"))
        report = read_json(folder / "report.json")
        self.assertEqual(report["estimate"]["actual_invoice_cost"], None)
        self.assertEqual(report["decision_draft"]["decision"], "hold")
        self.assertIn("volume.svg", (folder / "graphs.md").read_text(encoding="utf-8-sig"))
        self.assertIn("payback.svg", (folder / "graphs.md").read_text(encoding="utf-8-sig"))
        with (folder / "cumulative.csv").open(encoding="utf-8-sig", newline="") as stream:
            first = next(csv.DictReader(stream))
        self.assertEqual(float(first["month"]), 0)
        for model in ("teacher", "base", "fine_tuned"):
            self.assertEqual(float(first[model]), 0, "Operating-only graph must begin at zero")
        with (folder / "payback.csv").open(encoding="utf-8-sig", newline="") as stream:
            first = next(csv.DictReader(stream))
        self.assertEqual(float(first["month"]), 0)
        self.assertGreater(float(first["fine_tuned"]), 0, "Payback graph must include actual initial costs")

    def test_optional_costs_and_hours_change_future_curves_not_historical_cost(self):
        first = self.estimate(None, "default")
        write_json(self.root / "optional.json", {
            "additional_initial_cost": 100, "additional_monthly_cost": 20, "hosting_hours_per_day": 6})
        changed = self.estimate(self.root / "optional.json", "changed")
        left, right = first["report"]["variants"], changed["report"]["variants"]
        self.assertEqual(left["fine_tuned"]["monthly_fixed_components"]["model_hosting"], 365)
        self.assertEqual(right["fine_tuned"]["monthly_fixed_components"]["model_hosting"], 91.25)
        self.assertEqual(right["fine_tuned"]["monthly_fixed"], 111.25)
        self.assertAlmostEqual(right["fine_tuned"]["initial_total"] - left["fine_tuned"]["initial_total"], 100)
        for name in ("teacher", "base", "fine_tuned"):
            self.assertEqual(left[name]["initial_components"]["evaluation"],
                             right[name]["initial_components"]["evaluation"])
            self.assertEqual(left[name]["inference_per_request"], right[name]["inference_per_request"])
        for name in ("teacher", "base"):
            self.assertEqual(left[name]["initial_total"], right[name]["initial_total"])
            self.assertEqual(left[name]["monthly_fixed"], right[name]["monthly_fixed"])
        for result in (first, changed):
            metadata = read_json(Path(result["output_dir"]) / "graph-plan.json")
            self.assertTrue(metadata)

    def test_explicit_missing_plan_does_not_silently_use_defaults(self):
        with self.assertRaises(FileNotFoundError):
            self.estimate(self.root / "absent.json", "must-not-exist")
        self.assertFalse((self.root / "must-not-exist").exists())


if __name__ == "__main__":
    unittest.main()
