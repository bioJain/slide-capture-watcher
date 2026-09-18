"""
실제 Tk 위젯을 만드는 스모크 테스트. tkinter와 디스플레이(예: xvfb-run)가 있을 때만 실행된다.

    xvfb-run -a python -m pytest tests/test_gui_app.py
"""

import time

import numpy as np
import pytest
from PIL import Image

tkinter = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:  # 디스플레이 없음
        pytest.skip(f"Tk 디스플레이 없음: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tkinter.TclError:
        pass


def solid(value, size=(40, 60)):
    return Image.fromarray(np.full((size[1], size[0], 3), value, dtype=np.uint8))


class FakeCapture:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, hwnd):
        self.calls += 1
        return self.frames[min(self.calls - 1, len(self.frames) - 1)]


def pump(root, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def app(core, root, monkeypatch, win32gui_stub, tmp_path):
    import slide_capture_gui

    monkeypatch.setattr(core, "enumerate_visible_windows", lambda: [(7, "Fake Window"), (8, "Slide Capture Watcher")])
    monkeypatch.setattr(win32gui_stub, "GetWindowText", lambda hwnd: "Fake Window")
    app = slide_capture_gui.WatcherApp(root)
    app.var_outdir.set(str(tmp_path / "out"))
    app.vars["interval"].set("0.05")
    app.vars["debounce_ms"].set("0")
    return app


def log_text(app):
    return app.text_log.get("1.0", "end")


def test_refresh_lists_windows_and_hides_self(app):
    assert list(app.combo_window["values"]) == ["Fake Window"]
    assert app.form_values().hwnd == 7
    assert app.form_values().window_title == "Fake Window"


def test_start_captures_and_stop_returns_to_idle(app, core, root, monkeypatch, tmp_path):
    fake = FakeCapture([solid(0), solid(0), solid(255), solid(255), solid(255)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)

    app.start()
    assert app.is_running
    assert str(app.btn_start["state"]) == "disabled"
    assert str(app.btn_stop["state"]) == "normal"
    assert str(app.btn_capture["state"]) == "normal"

    assert pump(root, lambda: "저장됨 (2)" in log_text(app)), log_text(app)
    assert app.var_count.get() == "저장 2개"

    app.stop()
    assert pump(root, lambda: not app.is_running and str(app.btn_start["state"]) == "normal")
    assert "감시 종료" in log_text(app)
    assert app.var_status.get() == "대기 중"
    assert len(list((tmp_path / "out").glob("slide_*.png"))) == 2


def test_manual_capture_button(app, core, root, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "capture_window_printwindow", FakeCapture([solid(0)]))
    app.start()
    assert pump(root, lambda: "저장됨 (1)" in log_text(app))
    app.capture_now()
    assert pump(root, lambda: "_manual" in log_text(app)), log_text(app)
    app.stop()
    assert pump(root, lambda: not app.is_running)


def test_invalid_form_shows_error_and_does_not_start(app, monkeypatch):
    from tkinter import messagebox

    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, message, **kw: shown.append((title, message)))
    app.vars["roi"].set("0.5,0,0.6,1")
    app.start()
    assert not app.is_running
    assert shown and shown[0][1].startswith("ROI:")


def test_calibrate_mode_logs_metrics_without_saving(app, core, root, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "capture_window_printwindow", FakeCapture([solid(0), solid(0), solid(255)]))
    app.var_calibrate.set(True)
    app.start()
    assert str(app.btn_capture["state"]) == "disabled"
    assert pump(root, lambda: "reason=global" in log_text(app) or "reason=grid" in log_text(app)), log_text(app)
    app.stop()
    assert pump(root, lambda: not app.is_running)
    assert not list((tmp_path / "out").glob("slide_*.png")) if (tmp_path / "out").exists() else True


def test_window_not_found_is_reported_in_log(app, core, root, monkeypatch):
    monkeypatch.setattr(app, "_windows", [(99, "Gone")])
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [])
    import sys
    monkeypatch.setattr(sys.modules["win32gui"], "IsWindow", lambda hwnd: False)
    app.var_window.set("Gone")
    app.combo_window["values"] = ["Gone"]
    app.combo_window.current(0)
    app.start()
    assert pump(root, lambda: not app.is_running and "찾지 못했습니다" in log_text(app)), log_text(app)
    assert app.var_status.get() == "종료됨"


def test_add_and_remove_exclude(app, monkeypatch):
    from tkinter import messagebox

    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, message, **kw: shown.append(message))
    app.vars["exclude_entry"].set("0.7,0,0.3,0.3")
    app.add_exclude()
    app.vars["exclude_entry"].set("bad")
    app.add_exclude()
    assert list(app.list_exclude.get(0, "end")) == ["0.7,0,0.3,0.3"]
    assert shown
    app.list_exclude.selection_set(0)
    app.remove_exclude()
    assert list(app.list_exclude.get(0, "end")) == []


def test_duplicate_titles_map_to_distinct_hwnds(app, core, monkeypatch):
    monkeypatch.setattr(core, "enumerate_visible_windows", lambda: [(7, "Same"), (9, "Same")])
    app.refresh_windows()
    assert list(app.combo_window["values"]) == ["Same", "Same [2]"]
    app.combo_window.current(1)
    values = app.form_values()
    assert values.hwnd == 9
    assert values.window_title == "Same"


def test_status_returns_to_watching_after_failed_stabilization(app, core, root, monkeypatch):
    # 후보 감지 후 계속 바뀌는 프레임 -> 안정화 실패 -> 상태가 "감시 중"으로 돌아와야 한다.
    frames = [solid(0), solid(255), solid(0), solid(255), solid(0), solid(255), solid(0)]
    monkeypatch.setattr(core, "capture_window_printwindow", FakeCapture(frames))
    app.vars["interval"].set("1.0")  # 실패 후 다음 폴링까지 여유를 둬서 상태를 관찰
    app.start()
    assert pump(root, lambda: "안정화 실패" in log_text(app)), log_text(app)
    assert pump(root, lambda: app.var_status.get() == "감시 중", timeout=2.0), app.var_status.get()
    app.stop()
    assert pump(root, lambda: not app.is_running)


def test_captures_appear_in_gallery(app, core, root, monkeypatch, tmp_path):
    fake = FakeCapture([solid(0), solid(0), solid(255), solid(255), solid(255)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    app.start()
    assert pump(root, lambda: len(app.gallery.paths()) >= 2), log_text(app)
    assert [p.suffix for p in app.gallery.paths()] == [".png", ".png"]
    assert len(app.gallery.selected_paths()) == 2
    app.stop()
    assert pump(root, lambda: not app.is_running)


def test_existing_images_are_loaded_on_start(app, core, root, monkeypatch, tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    solid(90).save(outdir / "slide_20260101_000001.png")
    monkeypatch.setattr(core, "capture_window_printwindow", FakeCapture([solid(0)]))
    app.start()
    assert pump(root, lambda: "저장됨 (1)" in log_text(app))
    assert "기존 이미지 1개" in log_text(app)
    assert len(app.gallery.paths()) == 2
    app.stop()
    assert pump(root, lambda: not app.is_running)


def test_unreadable_output_folder_is_reported_and_watcher_not_started(app, core, monkeypatch, tmp_path):
    import gallery as gallery_module
    from tkinter import messagebox

    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, message, **kw: shown.append((title, message)))

    def denied(directory):
        raise PermissionError(13, "Access is denied", str(directory))

    monkeypatch.setattr(gallery_module, "find_images", denied)
    monkeypatch.setattr(core, "capture_window_printwindow", FakeCapture([solid(0)]))

    app.start()

    assert not app.is_running
    assert app.watcher is None
    assert shown and shown[0][0] == "저장 폴더"
    assert "폴더를 읽을 수 없습니다" in log_text(app)
    assert str(app.btn_start["state"]) == "normal"
