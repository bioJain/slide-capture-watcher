"""
image_files.py
==============

캡처 폴더의 이미지 파일을 찾고 정렬하는 공용 함수. 갤러리(JHA-9)와 내보내기(JHA-12)가 함께 쓴다.

``tools/png_to_pdf/png_to_pdf.py`` 에도 같은 정렬 규칙이 들어 있다. 그 스크립트는 파일 하나만
따로 받아 써도 되도록 자급자족해야 하므로 여기서 import하지 않고 규칙을 복제한다. 두 구현이
같은 순서를 내는지는 테스트(``tests/test_image_files.py``)로 묶어 둔다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple

_NUMBER_RE = re.compile(r"(\d+)")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def numeric_sort_key(path: Path) -> Tuple[Tuple[Tuple[int, object], ...], str]:
    """파일명 속 숫자 run은 숫자값으로, 나머지는 대소문자 무시 문자열로 비교하는 정렬 키."""
    parts: List[Tuple[int, object]] = []
    for part in _NUMBER_RE.split(Path(path).stem.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts), Path(path).name.casefold()


def is_capture_image(path: Path) -> bool:
    """갤러리에 보여줄 이미지인지. diff mask 파일은 제외한다."""
    path = Path(path)
    if path.suffix.casefold() not in IMAGE_SUFFIXES:
        return False
    return not path.stem.endswith("_diffmask")


def find_images(directory: Path) -> List[Path]:
    """폴더 바로 아래의 캡처 이미지를 파일명 숫자 순서로 돌려준다. 폴더가 없으면 빈 목록."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and is_capture_image(path)),
        key=numeric_sort_key,
    )


def sort_images(paths: Iterable[Path]) -> List[Path]:
    return sorted((Path(p) for p in paths), key=numeric_sort_key)
