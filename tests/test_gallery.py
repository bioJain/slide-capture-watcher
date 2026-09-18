"""ThumbnailGallery 위젯 테스트. tkinter와 디스플레이가 있을 때만 실행된다."""

import numpy as np
import pytest
from PIL import Image

tkinter = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk 디스플레이 없음: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tkinter.TclError:
        pass


def write_image(path, value=100, size=(80, 50)):
    Image.fromarray(np.full((size[1], size[0], 3), value, dtype=np.uint8)).save(path)
    return path


@pytest.fixture
def folder(tmp_path):
    for name, value in [("slide_10.png", 10), ("slide_2.png", 20), ("slide_1.png", 30)]:
        write_image(tmp_path / name, value)
    (tmp_path / "slide_2_diffmask.png").write_bytes(b"not an image")
    return tmp_path


@pytest.fixture
def gallery(root):
    import gallery as gallery_module

    widget = gallery_module.ThumbnailGallery(root)
    widget.grid_into(row=0, column=0, sticky="nsew")
    root.update()
    return widget


def names(paths):
    return [p.name for p in paths]


def test_load_folder_orders_and_selects_all(gallery, folder):
    count = gallery.load_folder(folder)
    assert count == 3
    assert names(gallery.paths()) == ["slide_1.png", "slide_2.png", "slide_10.png"]
    assert names(gallery.selected_paths()) == ["slide_1.png", "slide_2.png", "slide_10.png"]
    assert gallery.var_summary.get() == "이미지 3개 중 3개 선택"


def test_add_image_is_incremental_and_sorted(gallery, folder):
    gallery.load_folder(folder)
    new = write_image(folder / "slide_5.png", 50)
    assert gallery.add_image(new) is True
    assert gallery.add_image(new) is False
    assert names(gallery.paths()) == ["slide_1.png", "slide_2.png", "slide_5.png", "slide_10.png"]
    assert len(gallery._items) == 4


def test_selection_toggle_and_set_all(gallery, folder):
    gallery.load_folder(folder)
    gallery.set_all_selected(False)
    assert gallery.selected_paths() == []
    first = gallery.paths()[0]
    gallery._items[first].selected.set(True)
    gallery._selection_changed()
    assert names(gallery.selected_paths()) == ["slide_1.png"]
    assert gallery.var_summary.get() == "이미지 3개 중 1개 선택"


def test_show_detail_and_comment_callback(gallery, folder, root):
    changes = []
    gallery.on_comment_change = lambda path, text: changes.append((path.name, text))
    gallery.load_folder(folder)
    target = gallery.paths()[1]

    gallery.show_detail(target)
    root.update()
    assert gallery.current_path == target
    assert gallery.var_detail_name.get().startswith("slide_2.png")
    assert str(gallery.text_comment["state"]) == "normal"
    assert gallery._preview_photo is not None

    gallery.text_comment.insert("1.0", "중요 슬라이드")
    gallery._on_comment_key()
    assert changes == [("slide_2.png", "중요 슬라이드")]
    assert gallery.comments[target] == "중요 슬라이드"

    # 다른 항목으로 갔다가 돌아와도 코멘트가 유지된다 (세션 메모리)
    gallery.show_detail(gallery.paths()[0])
    assert gallery.text_comment.get("1.0", "end-1c") == ""
    gallery.show_detail(target)
    assert gallery.text_comment.get("1.0", "end-1c") == "중요 슬라이드"


def test_set_comment_does_not_fire_callback(gallery, folder):
    changes = []
    gallery.on_comment_change = lambda path, text: changes.append(text)
    gallery.load_folder(folder)
    target = gallery.paths()[0]
    gallery.set_comment(target, "불러온 코멘트")
    gallery.show_detail(target)
    assert gallery.text_comment.get("1.0", "end-1c") == "불러온 코멘트"
    assert changes == []


def test_remove_image_clears_detail(gallery, folder):
    gallery.load_folder(folder)
    target = gallery.paths()[0]
    gallery.show_detail(target)
    assert gallery.remove_image(target) is True
    assert gallery.remove_image(target) is False
    assert names(gallery.paths()) == ["slide_2.png", "slide_10.png"]
    assert gallery.current_path is None
    assert str(gallery.text_comment["state"]) == "disabled"


def test_broken_file_gets_placeholder_thumbnail(gallery, tmp_path):
    bad = tmp_path / "slide_1.png"
    bad.write_bytes(b"garbage")
    assert gallery.add_image(bad) is True
    gallery.show_detail(bad)
    assert "읽을 수 없음" in gallery.var_detail_name.get()


def test_relayout_on_resize_changes_columns(gallery, folder, root):
    gallery.load_folder(folder)

    class Event:
        width = 1000

    gallery._on_canvas_resize(Event())
    wide_columns = gallery._columns
    Event.width = 200
    gallery._on_canvas_resize(Event())
    assert gallery._columns == 1 < wide_columns
    rows = {item.frame.grid_info()["row"] for item in gallery._items.values()}
    assert rows == {0, 1, 2}


def test_relayout_resets_weights_of_unused_columns(gallery, folder):
    gallery.load_folder(folder)

    class Event:
        width = 1000

    gallery._on_canvas_resize(Event())
    wide = gallery._columns
    assert wide > 1
    assert gallery.grid.columnconfigure(wide - 1, "weight") == 1

    Event.width = 200
    gallery._on_canvas_resize(Event())
    assert gallery._columns == 1
    for column in range(1, wide):
        assert gallery.grid.columnconfigure(column, "weight") == 0
    assert gallery.grid.columnconfigure(0, "weight") == 1


def test_wheel_applies_over_canvas_children_only(gallery, folder):
    gallery.load_folder(folder)
    item = next(iter(gallery._items.values()))
    assert gallery._widget_in_gallery_canvas(gallery.canvas)
    assert gallery._widget_in_gallery_canvas(gallery.grid)
    assert gallery._widget_in_gallery_canvas(item.check)
    assert not gallery._widget_in_gallery_canvas(gallery.text_comment)
    assert not gallery._widget_in_gallery_canvas(gallery.btn_select_all)
    assert not gallery._widget_in_gallery_canvas(None)


def test_wheel_scrolls_only_when_pointer_is_over_canvas(gallery, folder, monkeypatch):
    gallery.load_folder(folder)
    scrolled = []
    monkeypatch.setattr(gallery.canvas, "yview_scroll", lambda units, what: scrolled.append(units))

    class Event:
        delta = -120
        x_root = 0
        y_root = 0

    monkeypatch.setattr(gallery.canvas, "winfo_containing", lambda x, y: gallery.text_comment)
    gallery._on_mousewheel(Event())
    assert scrolled == []

    monkeypatch.setattr(gallery.canvas, "winfo_containing", lambda x, y: gallery.grid)
    gallery._on_mousewheel(Event())
    assert scrolled == [1]
