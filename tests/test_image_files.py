import importlib.util
from pathlib import Path

import pytest

import image_files


def test_numeric_order_and_case_insensitive(tmp_path):
    names = ["slide_10.png", "slide_2.PNG", "slide_1.png", "Slide_3.png", "notes.txt", "slide_5_diffmask.png"]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    found = [p.name for p in image_files.find_images(tmp_path)]
    assert found == ["slide_1.png", "slide_2.PNG", "Slide_3.png", "slide_10.png"]


def test_find_images_missing_dir_is_empty(tmp_path):
    assert image_files.find_images(tmp_path / "nope") == []


def test_is_capture_image_excludes_diffmask():
    assert image_files.is_capture_image(Path("slide_20260918_151530.png"))
    assert not image_files.is_capture_image(Path("slide_20260918_151530_diffmask.png"))
    assert not image_files.is_capture_image(Path("notes.json"))


def test_order_matches_png_to_pdf_tool():
    """tools/png_to_pdf는 자급자족 스크립트라 규칙을 복제했다. 두 구현의 순서가 같아야 한다."""
    tool_path = Path(__file__).parents[1] / "tools" / "png_to_pdf" / "png_to_pdf.py"
    spec = importlib.util.spec_from_file_location("png_to_pdf_tool", tool_path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    paths = [Path(n) for n in ["b_2.png", "a_10.png", "a_9.png", "A_9.png", "a_9b.png", "img.png", "a_09.png"]]
    assert sorted(paths, key=image_files.numeric_sort_key) == sorted(paths, key=tool.numeric_sort_key)


@pytest.mark.parametrize("width, cell, expected", [(0, 200, 1), (199, 200, 1), (200, 200, 1), (650, 200, 3), (900, 0, 1)])
def test_columns_for_width(width, cell, expected):
    import gallery

    assert gallery.columns_for_width(width, cell) == expected
