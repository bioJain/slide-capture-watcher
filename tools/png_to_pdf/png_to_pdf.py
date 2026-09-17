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
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


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


def find_images(input_dir: Path) -> list[Path]:
    """Find supported direct-child images in numeric filename order."""
    return sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in _IMAGE_SUFFIXES
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


def create_pdf(image_paths: Iterable[Path], output_path: Path) -> int:
    """Write one PDF page per selected image and return the page count."""
    paths = list(image_paths)
    if not paths:
        raise ValueError("PDF로 변환할 이미지 파일이 없습니다.")

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
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        help="PNG 파일이 들어 있는 폴더 (--gui 사용 시 생략 가능)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="출력 PDF 경로 (기본값: <입력 폴더>/<입력 폴더명>.pdf)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="폴더의 이미지 썸네일을 보고 선택하여 PDF로 저장하는 창 열기",
    )
    return parser


class ImagePdfSelector:
    """Small Tk GUI for selecting local images and exporting them as a PDF."""

    def __init__(self, root: object, initial_dir: Path | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.ImageTk = ImageTk
        self.directory: Path | None = None
        self.images: list[Path] = []
        self.selected: list[object] = []
        self._thumbnail_refs: list[object] = []

        root.title("이미지 선택 → PDF")
        root.geometry("920x680")
        root.minsize(620, 420)

        toolbar = ttk.Frame(root, padding=10)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="폴더 열기", command=self.choose_folder).pack(side="left")
        ttk.Button(toolbar, text="전체 선택", command=lambda: self.set_all(True)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="전체 해제", command=lambda: self.set_all(False)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="선택 이미지 PDF 저장", command=self.export_pdf).pack(
            side="right"
        )

        self.status = tk.StringVar(value="폴더 열기를 눌러 이미지 폴더를 선택하세요.")
        ttk.Label(root, textvariable=self.status, padding=(10, 0, 10, 8)).pack(fill="x")

        container = ttk.Frame(root)
        container.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.grid = ttk.Frame(self.canvas)
        self.grid.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.canvas.bind("<Configure>", self._resize_grid)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        if initial_dir and initial_dir.is_dir():
            self.load_folder(initial_dir)

    def _resize_grid(self, event: object) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event: object) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def choose_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(
            title="이미지 폴더 선택",
            initialdir=str(self.directory) if self.directory else None,
        )
        if chosen:
            self.load_folder(Path(chosen))

    def load_folder(self, directory: Path) -> None:
        from tkinter import messagebox

        try:
            images = find_images(directory)
        except OSError as exc:
            messagebox.showerror("폴더 열기 실패", str(exc))
            return

        self.directory = directory
        self.images = images
        self.selected.clear()
        self._thumbnail_refs.clear()
        for child in self.grid.winfo_children():
            child.destroy()

        if not images:
            self.status.set(f"지원하는 이미지가 없습니다: {directory}")
            return

        for index, path in enumerate(images):
            variable = self.tk.BooleanVar(value=True)
            self.selected.append(variable)
            try:
                with Image.open(path) as source:
                    thumbnail = source.convert("RGB")
                    thumbnail.thumbnail((180, 130), Image.Resampling.LANCZOS)
                photo = self.ImageTk.PhotoImage(thumbnail)
            except OSError:
                photo = self.ImageTk.PhotoImage(Image.new("RGB", (180, 130), "#dddddd"))
                variable.set(False)
            self._thumbnail_refs.append(photo)
            checkbox = self.ttk.Checkbutton(
                self.grid,
                text=path.name,
                image=photo,
                compound="top",
                variable=variable,
                width=24,
                command=self.update_status,
            )
            checkbox.grid(row=index // 4, column=index % 4, padx=8, pady=8, sticky="n")

        for column in range(4):
            self.grid.columnconfigure(column, weight=1)
        self.update_status()

    def set_all(self, value: bool) -> None:
        for variable in self.selected:
            variable.set(value)
        self.update_status()

    def selected_images(self) -> list[Path]:
        return [
            path
            for path, variable in zip(self.images, self.selected)
            if variable.get()
        ]

    def update_status(self) -> None:
        selected_count = len(self.selected_images())
        self.status.set(
            f"{self.directory} — 전체 {len(self.images)}개 중 {selected_count}개 선택"
        )

    def export_pdf(self) -> None:
        from tkinter import filedialog, messagebox

        chosen_images = self.selected_images()
        if not chosen_images:
            messagebox.showwarning("선택 필요", "PDF에 넣을 이미지를 하나 이상 선택하세요.")
            return

        assert self.directory is not None
        output = filedialog.asksaveasfilename(
            title="PDF 저장",
            initialdir=str(self.directory),
            initialfile=f"{self.directory.name}.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF 파일", "*.pdf")],
        )
        if not output:
            return
        try:
            page_count = create_pdf(chosen_images, Path(output))
        except (OSError, ValueError) as exc:
            messagebox.showerror("PDF 생성 실패", str(exc))
            return
        messagebox.showinfo("완료", f"{page_count}개 이미지를 PDF로 저장했습니다.\n{output}")


def run_gui(initial_dir: Path | None = None) -> int:
    """Open the image-selection GUI and block until it is closed."""
    import tkinter as tk

    root = tk.Tk()
    ImagePdfSelector(root, initial_dir)
    root.mainloop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gui:
        initial_dir = args.input_dir.expanduser() if args.input_dir else None
        return run_gui(initial_dir)
    if args.input_dir is None:
        print("오류: 입력 폴더를 지정하거나 --gui를 사용하세요.", file=sys.stderr)
        return 2
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
