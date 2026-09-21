import contextlib
import csv
import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CHAPTER = ROOT / "docs" / "chapters" / "01-introduction.md"
FIGURES = CHAPTER.parent / "chapter01-assets"
SPEC = importlib.util.spec_from_file_location(
    "chapter01_figures", ROOT / "scripts" / "generate_chapter01_figures.py")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class ChapterOneFiguresTests(unittest.TestCase):
    def test_published_figures_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(GENERATOR, "OUT", output), contextlib.redirect_stdout(io.StringIO()):
                GENERATOR.main()
            self.assertEqual({path.name for path in output.iterdir()},
                             {"flow.svg", "monthly-cost.svg", "payback.svg", "illustrative-costs.csv"})
            for path in output.iterdir():
                with self.subTest(path=path.name):
                    self.assertEqual(path.read_text(encoding="utf-8"),
                                     (FIGURES / path.name).read_text(encoding="utf-8"))
                    if path.suffix == ".svg":
                        document = ET.parse(path).getroot()
                        self.assertEqual(document.get("role"), "img")
                        self.assertIsNotNone(document.find("{http://www.w3.org/2000/svg}desc"))
            with (output / "illustrative-costs.csv").open(encoding="utf-8", newline="") as stream:
                crossing = next(row for row in csv.DictReader(stream) if row["monthly_requests"] == "500")
            self.assertEqual(crossing["teacher_monthly_jpy"], "10000")
            self.assertEqual(crossing["trained_student_monthly_jpy"], "10000")

    def test_chapter_images_resolve_and_references_are_links(self):
        chapter = CHAPTER.read_text(encoding="utf-8")
        images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", chapter)
        self.assertEqual(len(images), 3)
        for image in images:
            self.assertTrue((CHAPTER.parent / Path(image)).is_file(), image)
        references = chapter.split("## リファレンス", 1)[1]
        self.assertEqual(len(re.findall(r"\]\(https://", references)), 3)
        self.assertNotRegex(references, r"(?m)^\s*https://")


if __name__ == "__main__":
    unittest.main()
