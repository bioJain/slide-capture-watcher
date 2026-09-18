"""
gallery.py
==========

캡처 이미지 썸네일 갤러리 위젯 (JHA-9).

- Canvas + Frame + Scrollbar 그리드. 창 폭에 맞춰 열 수를 다시 계산한다.
- 항목마다 체크박스가 있어 내보내기 대상을 다중 선택할 수 있다.
- 메모리에는 축소 썸네일만 유지한다. 큰 미리보기는 클릭할 때 디스크에서 다시 읽어 상세 패널에
  하나만 띄운다(장시간 세션에서 원본을 붙들지 않기 위함).
- ``add_image(path)`` 로 캡처 스레드가 알려준 새 파일을 증분 추가한다(전체 다시 그리지 않음).
- 코멘트 입력창은 상세 패널에 있으며 ``on_comment_change(path, text)`` 콜백으로 밖에 알린다.
  파일로 영속화하는 것은 JHA-10에서 처리한다. 이 위젯은 세션 동안 메모리에만 들고 있는다.

Tk 위젯은 메인 스레드에서만 다뤄야 한다. 캡처 스레드는 큐로 이벤트를 보내고 메인 스레드가
``add_image`` 를 호출하는 구조를 전제로 한다(``slide_capture_gui.WatcherApp`` 참고).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from image_files import find_images, numeric_sort_key

THUMB_SIZE = (140, 100)
PREVIEW_MAX = (380, 240)
MIN_COLUMNS = 1
CELL_PAD = 6


def thumbnail_for(path: Path, size: Tuple[int, int] = THUMB_SIZE) -> Image.Image:
    """디스크에서 읽어 축소한 RGB 썸네일. 깨진 파일이면 회색 placeholder."""
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return image
    except OSError:
        return Image.new("RGB", size, "#c8c8c8")


def preview_for(path: Path, max_size: Tuple[int, int] = PREVIEW_MAX) -> Optional[Image.Image]:
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            return image
    except OSError:
        return None


def _short_name(name: str, limit: int = 24) -> str:
    """체크박스 라벨용. 긴 파일명은 앞부분만 보이고 전체 이름은 상세 패널에서 확인."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def columns_for_width(width: int, cell_width: int) -> int:
    """캔버스 폭에 들어가는 열 수(최소 1)."""
    if cell_width <= 0:
        return MIN_COLUMNS
    return max(MIN_COLUMNS, width // cell_width)


@dataclass
class GalleryItem:
    path: Path
    frame: object  # ttk.Frame
    check: object  # ttk.Checkbutton
    selected: object  # tk.BooleanVar
    photo: object  # ImageTk.PhotoImage (참조 유지용)


class ThumbnailGallery:
    """
    썸네일 그리드 + 상세 패널.

    ``parent`` 안에 ``ttk.Frame`` 을 만들어 자신을 배치한다. 접근 API:
    ``load_folder``, ``add_image``, ``remove_image``, ``paths()``, ``selected_paths()``,
    ``set_all_selected``, ``current_path``, ``comments``.
    """

    def __init__(
        self,
        parent,
        on_comment_change: Optional[Callable[[Path, str], None]] = None,
        on_selection_change: Optional[Callable[[], None]] = None,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.on_comment_change = on_comment_change
        self.on_selection_change = on_selection_change
        self._items: Dict[Path, GalleryItem] = {}
        self._order: List[Path] = []
        self._columns = 3
        self._current: Optional[Path] = None
        self._preview_photo = None
        self.comments: Dict[Path, str] = {}
        self._suppress_comment_event = False

        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(1, weight=1)  # 그리드가 남는 높이를 가져간다. 상세 패널은 고정 높이.
        self.frame.columnconfigure(0, weight=1)

        # -- 툴바
        toolbar = ttk.Frame(self.frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.btn_select_all = ttk.Button(toolbar, text="전체 선택", command=lambda: self.set_all_selected(True))
        self.btn_select_all.pack(side="left")
        self.btn_select_none = ttk.Button(toolbar, text="전체 해제", command=lambda: self.set_all_selected(False))
        self.btn_select_none.pack(side="left", padx=(4, 0))
        self.var_summary = tk.StringVar(value="이미지 0개")
        ttk.Label(toolbar, textvariable=self.var_summary).pack(side="right")

        # -- 그리드 (Canvas + Frame + Scrollbar)
        grid_holder = ttk.Frame(self.frame)
        grid_holder.grid(row=1, column=0, sticky="nsew")
        grid_holder.rowconfigure(0, weight=1)
        grid_holder.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(grid_holder, highlightthickness=0, height=300)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(grid_holder, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.grid = ttk.Frame(self.canvas)
        self._grid_window = self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))

        # -- 상세 패널
        detail = ttk.LabelFrame(self.frame, text="선택한 캡처", padding=6)
        detail.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        detail.columnconfigure(0, weight=1)
        self.var_detail_name = tk.StringVar(value="썸네일을 클릭하면 여기에 크게 표시됩니다.")
        ttk.Label(detail, textvariable=self.var_detail_name).grid(row=0, column=0, sticky="w")
        preview_box = ttk.Frame(detail, height=PREVIEW_MAX[1], width=PREVIEW_MAX[0])
        preview_box.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        preview_box.grid_propagate(False)  # 이미지 크기가 달라도 패널 높이가 흔들리지 않게
        self.preview_label = ttk.Label(preview_box, anchor="center")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(detail, text="코멘트").grid(row=2, column=0, sticky="w")
        self.text_comment = tk.Text(detail, height=3, wrap="word", state="disabled")
        self.text_comment.grid(row=3, column=0, sticky="ew")
        self.text_comment.bind("<KeyRelease>", self._on_comment_key)
        self.text_comment.bind("<FocusOut>", self._on_comment_key)

    # -- 배치 ----------------------------------------------------------------------

    def grid_into(self, **grid_kwargs) -> None:
        self.frame.grid(**grid_kwargs)

    def pack_into(self, **pack_kwargs) -> None:
        self.frame.pack(**pack_kwargs)

    # -- 데이터 ----------------------------------------------------------------------

    def paths(self) -> List[Path]:
        return list(self._order)

    def selected_paths(self) -> List[Path]:
        return [p for p in self._order if self._items[p].selected.get()]

    @property
    def current_path(self) -> Optional[Path]:
        return self._current

    def load_folder(self, directory: Path) -> int:
        """폴더의 이미지를 전부 다시 읽어 그린다. 기존 항목은 비운다. 반환값은 항목 수."""
        self.clear()
        for path in find_images(Path(directory)):
            self._insert(path, select=True)
        self._relayout()
        self._update_summary()
        return len(self._order)

    def clear(self) -> None:
        for item in self._items.values():
            item.frame.destroy()
        self._items.clear()
        self._order.clear()
        self._current = None
        self._show_detail(None)
        self._update_summary()

    def add_image(self, path: Path, select: bool = True) -> bool:
        """새 캡처 1장을 증분 추가한다. 이미 있으면 False."""
        path = Path(path)
        if path in self._items:
            return False
        self._insert(path, select=select)
        self._relayout()
        self._update_summary()
        return True

    def remove_image(self, path: Path) -> bool:
        path = Path(path)
        item = self._items.pop(path, None)
        if item is None:
            return False
        item.frame.destroy()
        self._order.remove(path)
        self.comments.pop(path, None)
        if self._current == path:
            self._current = None
            self._show_detail(None)
        self._relayout()
        self._update_summary()
        return True

    def set_all_selected(self, value: bool) -> None:
        for item in self._items.values():
            item.selected.set(value)
        self._selection_changed()

    def set_comment(self, path: Path, text: str) -> None:
        """외부(예: JHA-10 불러오기)에서 코멘트를 채운다. 콜백은 호출하지 않는다."""
        self.comments[Path(path)] = text
        if self._current == Path(path):
            self._fill_comment_widget(text)

    # -- 내부 -----------------------------------------------------------------------

    def _insert(self, path: Path, select: bool) -> None:
        from PIL import ImageTk

        photo = ImageTk.PhotoImage(thumbnail_for(path))
        cell = self.ttk.Frame(self.grid, padding=2)
        selected = self.tk.BooleanVar(value=select)
        # 이미지 클릭 = 상세 보기, 아래 체크박스 = 내보내기 선택. 두 동작을 섞지 않는다.
        image_label = self.ttk.Label(cell, image=photo, cursor="hand2")
        image_label.pack()
        image_label.bind("<Button-1>", lambda _e, p=path: self.show_detail(p))
        check = self.ttk.Checkbutton(
            cell,
            text=_short_name(path.name),
            variable=selected,
            width=22,
            command=self._selection_changed,
        )
        check.pack()
        self._items[path] = GalleryItem(path, cell, check, selected, photo)
        self._order.append(path)
        self._order.sort(key=numeric_sort_key)

    def show_detail(self, path: Optional[Path]) -> None:
        self._current = Path(path) if path is not None else None
        self._show_detail(self._current)

    def _show_detail(self, path: Optional[Path]) -> None:
        from PIL import ImageTk

        if path is None:
            self.var_detail_name.set("썸네일을 클릭하면 여기에 크게 표시됩니다.")
            self.preview_label.configure(image="")
            self._preview_photo = None
            self._fill_comment_widget("", enabled=False)
            return
        preview = preview_for(path)
        if preview is None:
            self.var_detail_name.set(f"{path.name} (읽을 수 없음)")
            self.preview_label.configure(image="")
            self._preview_photo = None
        else:
            self._preview_photo = ImageTk.PhotoImage(preview)
            self.preview_label.configure(image=self._preview_photo)
            self.var_detail_name.set(f"{path.name}  ({preview.width}x{preview.height} 미리보기)")
        self._fill_comment_widget(self.comments.get(path, ""), enabled=True)

    def _fill_comment_widget(self, text: str, enabled: bool = True) -> None:
        self._suppress_comment_event = True
        try:
            self.text_comment.configure(state="normal")
            self.text_comment.delete("1.0", "end")
            self.text_comment.insert("1.0", text)
            if not enabled:
                self.text_comment.configure(state="disabled")
        finally:
            self._suppress_comment_event = False

    def _on_comment_key(self, _event=None) -> None:
        if self._suppress_comment_event or self._current is None:
            return
        text = self.text_comment.get("1.0", "end-1c")
        if self.comments.get(self._current, "") == text:
            return
        self.comments[self._current] = text
        if self.on_comment_change is not None:
            self.on_comment_change(self._current, text)

    def _selection_changed(self) -> None:
        self._update_summary()
        if self.on_selection_change is not None:
            self.on_selection_change()

    def _update_summary(self) -> None:
        self.var_summary.set(f"이미지 {len(self._order)}개 중 {len(self.selected_paths())}개 선택")

    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self._grid_window, width=event.width)
        cell_width = THUMB_SIZE[0] + 2 * CELL_PAD + 24
        columns = columns_for_width(event.width, cell_width)
        if columns != self._columns:
            self._columns = columns
            self._relayout()

    def _relayout(self) -> None:
        for column in range(max(self._columns, 1)):
            self.grid.columnconfigure(column, weight=1)
        for index, path in enumerate(self._order):
            item = self._items[path]
            item.frame.grid(row=index // self._columns, column=index % self._columns, padx=CELL_PAD, pady=CELL_PAD, sticky="n")

    def _bind_wheel(self, enable: bool) -> None:
        if enable:
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
            self.canvas.bind_all("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind_all("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
