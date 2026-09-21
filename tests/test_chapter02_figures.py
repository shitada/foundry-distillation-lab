import contextlib
import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CHAPTER = ROOT / "docs" / "chapters" / "02-measurement.md"
FIGURES = CHAPTER.parent / "chapter02-assets"
NS = {"s": "http://www.w3.org/2000/svg"}
SPEC = importlib.util.spec_from_file_location(
    "chapter02_figures", ROOT / "scripts" / "generate_chapter02_evaluation_figures.py")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class ChapterTwoFiguresTests(unittest.TestCase):
    def test_evaluation_figures_are_reproducible_and_show_one_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(GENERATOR, "OUT", output), contextlib.redirect_stdout(io.StringIO()):
                GENERATOR.main()
            self.assertEqual({p.name for p in output.iterdir()},
                             {"evaluation-next-action.svg", "evaluation-workflow.svg"})
            for path in output.iterdir():
                with self.subTest(figure=path.name):
                    self.assertEqual(path.read_text(encoding="utf-8"),
                                     (FIGURES / path.name).read_text(encoding="utf-8"))
                    root = ET.parse(path).getroot()
                    labels = [node.text or "" for node in root.findall(".//s:text", NS)]
                    self.assertEqual(labels.count("評価対象モデル"), 1)
                    self.assertFalse(any("学習前" in value or "学習済み" in value for value in labels))
                    self.assertIsNotNone(root.find("s:desc", NS))

    def test_adoption_flow_is_six_stages_on_one_vertical_axis(self):
        root = ET.parse(FIGURES / "adoption-flow.svg").getroot()
        boxes = [node for node in root.findall(".//s:rect", NS) if node.get("width") == "600"]
        self.assertEqual(len(boxes), 6)
        self.assertEqual(len({node.get("x") for node in boxes}), 1)
        self.assertEqual([int(node.get("y")) for node in boxes],
                         sorted(int(node.get("y")) for node in boxes))
        labels = " ".join(node.text or "" for node in root.findall(".//s:text", NS))
        self.assertIn("今後の追加導入費", labels)
        self.assertIn("教師ありファインチューニング（SFT）", labels)

    def test_chapter_images_resolve(self):
        text = CHAPTER.read_text(encoding="utf-8")
        images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        self.assertEqual(len(images), 3)
        for image in images:
            path = CHAPTER.parent / Path(image)
            self.assertTrue(path.is_file(), image)
            self.assertEqual(ET.parse(path).getroot().get("role"), "img")


if __name__ == "__main__":
    unittest.main()
