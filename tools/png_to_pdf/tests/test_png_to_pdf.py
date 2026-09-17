import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image

MODULE_PATH = Path(__file__).parents[1] / "png_to_pdf.py"
SPEC = importlib.util.spec_from_file_location("png_to_pdf", MODULE_PATH)
assert SPEC and SPEC.loader
png_to_pdf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(png_to_pdf)


class PngToPdfTests(unittest.TestCase):
    def test_find_pngs_uses_numeric_order_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for name in ("slide_10.png", "slide_2.PNG", "slide_1.png", "notes.txt"):
                (directory / name).touch()

            names = [path.name for path in png_to_pdf.find_pngs(directory)]

            self.assertEqual(names, ["slide_1.png", "slide_2.PNG", "slide_10.png"])

    def test_find_images_supports_common_formats_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for name in ("page_12.jpg", "page_2.webp", "page_1.PNG", "notes.txt"):
                (directory / name).touch()

            names = [path.name for path in png_to_pdf.find_images(directory)]

            self.assertEqual(names, ["page_1.PNG", "page_2.webp", "page_12.jpg"])

    def test_create_pdf_writes_one_page_per_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            paths = []
            for name, mode, color in (
                ("slide_1.png", "RGB", "red"),
                ("slide_3.png", "RGBA", (0, 0, 255, 128)),
            ):
                path = directory / name
                Image.new(mode, (20, 10), color).save(path)
                paths.append(path)

            output = directory / "slides.pdf"
            count = png_to_pdf.create_pdf(paths, output)

            self.assertEqual(count, 2)
            with Image.open(output) as pdf:
                self.assertEqual(pdf.n_frames, 2)

    def test_main_rejects_directory_without_pngs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(png_to_pdf.main([temp_dir]), 1)

    def test_main_requires_a_directory_outside_gui_mode(self):
        self.assertEqual(png_to_pdf.main([]), 2)


if __name__ == "__main__":
    unittest.main()
