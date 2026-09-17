#!/usr/bin/env python3
"""Combine PNG files in a directory into a timestamp/number-ordered PDF."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

_NUMBER_RE = re.compile(r"(\d+)")


def numeric_sort_key(path: Path) -> tuple[tuple[tuple[int, object], ...], str]:
    """Return a key that compares digit runs numerically and text case-insensitively."""
    parts: list[tuple[int, object]] = []
    for part in _NUMBER_RE.split(path.stem.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts), path.name.casefold()


def find_pngs(input_dir: Path) -> list[Path]:
    """Find direct child PNG files in deterministic numeric filename order."""
    return sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.casefold() == ".png"
        ),
        key=numeric_sort_key,
    )


def _as_rgb(image: Image.Image) -> Image.Image:
    """Flatten transparency onto white and return an RGB image suitable for PDF."""
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def create_pdf(png_paths: Iterable[Path], output_path: Path) -> int:
    """Write one PDF page per PNG and return the number of pages written."""
    paths = list(png_paths)
    if not paths:
        raise ValueError("PDF로 변환할 PNG 파일이 없습니다.")

    pages: list[Image.Image] = []
    try:
        for path in paths:
            with Image.open(path) as image:
                image.load()
                pages.append(_as_rgb(image))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pages[0].save(
            output_path,
            "PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=100.0,
        )
    finally:
        for page in pages:
            page.close()

    return len(paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="폴더의 PNG 파일을 파일명 속 숫자 순서대로 정렬해 하나의 PDF로 만듭니다."
    )
    parser.add_argument("input_dir", type=Path, help="PNG 파일이 들어 있는 폴더")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="출력 PDF 경로 (기본값: <입력 폴더>/<입력 폴더명>.pdf)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir: Path = args.input_dir.expanduser()

    if not input_dir.is_dir():
        print(f"오류: 폴더를 찾을 수 없습니다: {input_dir}", file=sys.stderr)
        return 2

    output_path = (
        args.output.expanduser()
        if args.output
        else input_dir / f"{input_dir.resolve().name}.pdf"
    )
    if output_path.suffix.casefold() != ".pdf":
        print("오류: 출력 파일은 .pdf 확장자여야 합니다.", file=sys.stderr)
        return 2

    png_paths = find_pngs(input_dir)
    if not png_paths:
        print(f"오류: PNG 파일이 없습니다: {input_dir}", file=sys.stderr)
        return 1

    try:
        page_count = create_pdf(png_paths, output_path)
    except (OSError, ValueError) as exc:
        print(f"오류: PDF 생성에 실패했습니다: {exc}", file=sys.stderr)
        return 1

    print(f"완료: {page_count}개 이미지를 {output_path}에 저장했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
