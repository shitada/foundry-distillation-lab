from pathlib import Path
import unittest

from foundry_distillation_lab.io import read_json, sha256

ROOT = Path(__file__).resolve().parents[1]


class ProvenanceTests(unittest.TestCase):
    def test_copied_assets_match_recorded_source(self):
        record = read_json(ROOT / "docs" / "reference" / "provenance.json")
        self.assertTrue(record["local_worktree_included_uncommitted_files"])
        exact = [entry for entry in record["sources"] if entry["adaptation"].startswith("unchanged")]
        self.assertEqual(len(exact), 4)
        for entry in exact:
            path = ROOT.joinpath(*entry["target"].split("/"))
            with self.subTest(path=entry["target"]):
                self.assertEqual(sha256(path), entry["local_source_sha256"])
                self.assertEqual(sha256(path), entry["target_sha256"])

    def test_original_mit_notice_preserved(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2025 Azure AI Foundry", license_text)
        self.assertIn("Permission is hereby granted", license_text)
        self.assertIn("shall be included", license_text)


if __name__ == "__main__":
    unittest.main()
