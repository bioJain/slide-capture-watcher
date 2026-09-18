"""Tk 없이 검증 가능한 GUI 로직: 폼 값 → core 설정 변환, 이벤트 → 로그 문자열."""

from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def gui(core):
    import slide_capture_gui

    return slide_capture_gui


def valid_values(gui, **overrides):
    values = gui.FormValues.from_config(gui.Config())
    values.window_title = "Zoom Meeting"
    values.hwnd = 42
    values.outdir = "C:/captures"
    for key, value in overrides.items():
        setattr(values, key, value)
    return values


def test_defaults_round_trip_to_core_defaults(gui, core):
    options, cfg = gui.build_settings(valid_values(gui))
    assert cfg == core.Config()
    assert options.title == "Zoom Meeting"
    assert options.hwnd == 42
    assert options.outdir == Path("C:/captures")
    assert options.mode == "window"
    assert options.interval == 1.0
    assert options.debug_diff is False


def test_roi_exclude_grid_and_mode_are_applied(gui):
    values = valid_values(
        gui,
        roi="0.1,0.1,0.8,0.8",
        excludes=["0.7,0,0.3,0.3", "0,0.9,1,0.1"],
        grid="off",
        mode="region",
        debug_diff=True,
        interval="0.5",
    )
    options, cfg = gui.build_settings(values)
    assert cfg.roi == (0.1, 0.1, 0.8, 0.8)
    assert cfg.exclude == [(0.7, 0.0, 0.3, 0.3), (0.0, 0.9, 1.0, 0.1)]
    assert cfg.grid_enabled is False
    assert options.mode == "region"
    assert options.debug_diff is True
    assert options.interval == 0.5


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"window_title": ""}, "감시 대상 창"),
        ({"outdir": "  "}, "저장 폴더"),
        ({"roi": "0.5,0,0.6,1"}, "ROI"),
        ({"excludes": ["0,0,1,1", "x"]}, "제외 영역 #2"),
        ({"grid": "3by3"}, "그리드"),
        ({"ssim_threshold": "1.5"}, "SSIM 임계치"),
        ({"debounce_ms": "abc"}, "디바운스(ms)"),
        ({"interval": "0"}, "폴링 간격(초)"),
        ({"pixel_diff_threshold": "300"}, "픽셀 차이 임계치"),
        ({"mode": "screen"}, "캡처 모드"),
    ],
)
def test_invalid_inputs_name_the_field(gui, overrides, field):
    with pytest.raises(gui.FormError) as info:
        gui.build_settings(valid_values(gui, **overrides))
    assert info.value.field_name == field
    assert str(info.value).startswith(field + ":")


def test_calibrate_does_not_require_outdir(gui):
    options, _ = gui.build_settings(valid_values(gui, outdir=""), calibrate=True)
    assert options.outdir == Path("captures")


def test_format_event_lines(gui, core, tmp_path):
    mask = np.zeros((2, 2), dtype=np.uint8)
    diff = core.DiffResult(0.5, 0.2, 0.4, (1, 2), True, "global", mask)
    assert gui.format_event(core.LogEvent("hello")) == "hello"
    assert gui.format_event(core.LogEvent("careful", "warning")) == "[경고] careful"
    assert gui.format_event(core.CaptureEvent(tmp_path / "slide_1.png", 3)) == "저장됨 (3): slide_1.png"
    assert "diff mask: m.png" in gui.format_event(core.CaptureEvent(tmp_path / "a.png", 1, "", tmp_path / "m.png"))
    assert gui.format_event(core.MetricsEvent("12:00:00", diff)).startswith("[12:00:00] ssim=0.5000")
    assert gui.format_event(core.CandidateEvent(diff)) is None
    assert gui.format_event(core.StoppedEvent("error", 0)) is None
    assert "총 2개" in gui.format_event(core.StoppedEvent("stopped", 2))
    assert "잃어버려" in gui.format_event(core.StoppedEvent("window_lost", 0))
