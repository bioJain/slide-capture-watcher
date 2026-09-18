import numpy as np
import pytest
from PIL import Image


def make_config(core, **overrides):
    values = {
        "roi": (0.0, 0.0, 1.0, 1.0),
        "exclude": [],
        "thumb_width": 20,
        "ssim_threshold": 0.90,
        "min_region_ratio": 0.15,
        "pixel_diff_threshold": 25,
        "grid_rows": 2,
        "grid_cols": 2,
        "min_cell_ratio": 0.35,
        "stability_ssim": 0.985,
        "debounce_ms": 600,
        "max_stabilize_retries": 5,
    }
    values.update(overrides)
    return core.Config(**values)


# ---------------------------------------------------------------------------
# 파싱 / 전처리
# ---------------------------------------------------------------------------

def test_parse_roi_and_grid_validation(core):
    assert core.parse_frac_rect("0.1,0.2,0.8,0.7") == (0.1, 0.2, 0.8, 0.7)
    assert core.parse_grid("off") == (0, 0)
    assert core.parse_grid("3x4") == (3, 4)

    with pytest.raises(ValueError):
        core.parse_frac_rect("0.5,0,0.6,1")
    with pytest.raises(ValueError):
        core.parse_frac_rect("a,b,c,d")
    with pytest.raises(ValueError):
        core.parse_grid("-1x3")


def test_config_defaults_match_cli_defaults(core):
    cfg = core.Config()
    assert cfg.thumb_width == 400
    assert (cfg.grid_rows, cfg.grid_cols) == (3, 3)
    assert cfg.grid_enabled
    assert core.Config(grid_rows=0, grid_cols=0).grid_enabled is False
    # 기본값 인스턴스끼리 exclude 리스트를 공유하지 않아야 한다.
    a, b = core.Config(), core.Config()
    a.exclude.append((0.0, 0.0, 0.5, 0.5))
    assert b.exclude == []


def test_watch_options_validates_mode(core, tmp_path):
    opts = core.WatchOptions(title="x", outdir=str(tmp_path))
    assert opts.outdir == tmp_path
    with pytest.raises(ValueError):
        core.WatchOptions(title="x", outdir=tmp_path, mode="screen")


def test_prepare_gray_crops_before_resizing_and_masks_exclusion(core):
    image = Image.fromarray(np.full((20, 40, 3), 255, dtype=np.uint8))
    config = make_config(core, roi=(0.25, 0.0, 0.5, 1.0), exclude=[(0.5, 0.0, 0.5, 1.0)])

    gray = core.prepare_gray(image, config)

    assert gray.shape == (20, 20)
    assert np.all(gray[:, 10:] == 128)
    assert np.all(gray[:, :10] == 255)


# ---------------------------------------------------------------------------
# SSIM
# ---------------------------------------------------------------------------

def test_compute_ssim_identical_frames_is_one(core):
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 256, (60, 90), dtype=np.uint8)
    assert core.compute_ssim(frame, frame) == pytest.approx(1.0)


def test_compute_ssim_unrelated_frames_is_low(core):
    rng = np.random.default_rng(2)
    a = rng.integers(0, 256, (60, 90), dtype=np.uint8)
    b = rng.integers(0, 256, (60, 90), dtype=np.uint8)
    assert core.compute_ssim(a, b) < 0.5


def test_compute_ssim_resizes_mismatched_shapes(core):
    a = np.full((40, 40), 200, dtype=np.uint8)
    b = np.full((20, 30), 200, dtype=np.uint8)
    assert core.compute_ssim(a, b) == pytest.approx(1.0)


def test_compute_ssim_tiny_images(core):
    a = np.zeros((2, 2), dtype=np.uint8)
    assert core.compute_ssim(a, a) == 1.0
    assert core.compute_ssim(a, a + 10) == 0.0


def test_compute_ssim_matches_skimage_default():
    """scikit-image가 설치된 환경에서만: 기존 구현과 수치가 같아야 기존 임계치를 유지할 수 있다."""
    skimage_metrics = pytest.importorskip("skimage.metrics")
    import capture_core as core

    rng = np.random.default_rng(3)
    base = rng.integers(0, 256, (45, 80), dtype=np.uint8)
    noisy = np.clip(base.astype(int) + rng.integers(-30, 30, base.shape), 0, 255).astype(np.uint8)
    gradient = np.tile(np.linspace(0, 255, 80).astype(np.uint8), (45, 1))
    local = gradient.copy()
    local[10:30, 20:50] = 0

    for x, y in [(base, noisy), (gradient, local), (base, base)]:
        expected = skimage_metrics.structural_similarity(x, y)
        assert core.compute_ssim(x, y) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# 변화 감지
# ---------------------------------------------------------------------------

def test_grid_path_detects_local_change_when_global_path_does_not(core):
    baseline = np.zeros((20, 20), dtype=np.uint8)
    changed = baseline.copy()
    changed[:10, :10] = 255
    config = make_config(core, ssim_threshold=0.0, min_cell_ratio=0.9)

    result = core.compare_frames(baseline, changed, config)

    assert result.is_candidate is True
    assert result.reason == "grid"
    assert result.grid_ratio == 1.0
    assert result.grid_cell == (0, 0)


def test_grid_can_be_disabled(core):
    baseline = np.zeros((20, 20), dtype=np.uint8)
    changed = baseline.copy()
    changed[:10, :10] = 255
    config = make_config(core, ssim_threshold=0.0, grid_rows=0, grid_cols=0)

    result = core.compare_frames(baseline, changed, config)

    assert result.is_candidate is False
    assert result.reason == "none"
    assert result.grid_cell is None


def test_global_path_ignores_small_cursor_like_change(core):
    rng = np.random.default_rng(4)
    baseline = rng.integers(0, 256, (60, 80), dtype=np.uint8)
    changed = baseline.copy()
    changed[5:9, 5:9] = 255  # 커서 크기의 국소 변화
    config = make_config(core, grid_rows=0, grid_cols=0)

    result = core.compare_frames(baseline, changed, config)

    assert result.is_candidate is False
    assert result.bbox_ratio < config.min_region_ratio


# ---------------------------------------------------------------------------
# 감시 루프 (win32 stub + 가짜 캡처 백엔드)
# ---------------------------------------------------------------------------

def solid(value: int, size=(40, 60)) -> Image.Image:
    return Image.fromarray(np.full((size[1], size[0], 3), value, dtype=np.uint8))


class FakeCapture:
    """호출될 때마다 프레임 목록을 순서대로 돌려주고, 다 떨어지면 마지막 프레임을 반복한다."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, hwnd):
        self.calls += 1
        index = min(self.calls - 1, len(self.frames) - 1)
        return self.frames[index]


@pytest.fixture
def fast_options(core, tmp_path):
    return core.WatchOptions(title="fake", outdir=tmp_path / "out", interval=0.0)


def _fast_config(core):
    return make_config(core, debounce_ms=0, max_stabilize_retries=2)


def test_watch_loop_saves_initial_and_stable_change_then_stops(core, monkeypatch, win32gui_stub, fast_options):
    frames = [solid(0), solid(0), solid(255), solid(255), solid(255)]
    fake = FakeCapture(frames)
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    events = []
    watcher = core.SlideWatcher(fast_options, _fast_config(core))

    def on_event(event):
        events.append(event)
        if isinstance(event, core.CaptureEvent) and event.count >= 2:
            watcher.stop()

    watcher._on_event = on_event
    saved = watcher.run()

    assert saved == 2
    captures = [e for e in events if isinstance(e, core.CaptureEvent)]
    assert captures[0].tag == "initial"
    assert captures[1].tag == ""
    assert all(e.path.exists() for e in captures)
    assert any(isinstance(e, core.CandidateEvent) for e in events)
    resolved = [e for e in events if isinstance(e, core.CandidateResolvedEvent)]
    assert resolved and resolved[0].captured is True
    stopped = events[-1]
    assert isinstance(stopped, core.StoppedEvent)
    assert stopped.reason == "stopped"
    assert stopped.save_count == 2


def test_watch_loop_skips_unstable_change(core, monkeypatch, win32gui_stub, fast_options):
    # 후보 감지 후 계속 바뀌는 프레임(동영상) -> 안정화 실패 -> 저장 안 함
    frames = [solid(0), solid(255), solid(0), solid(255), solid(0), solid(255)]
    fake = FakeCapture(frames)
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    events = []
    watcher = core.SlideWatcher(fast_options, _fast_config(core))

    def on_event(event):
        events.append(event)
        if isinstance(event, core.LogEvent) and "안정화 실패" in event.message:
            watcher.stop()

    watcher._on_event = on_event
    saved = watcher.run()

    assert saved == 1  # initial만
    assert any("안정화 실패" in e.message for e in events if isinstance(e, core.LogEvent))
    resolved = [e for e in events if isinstance(e, core.CandidateResolvedEvent)]
    assert resolved and resolved[0].captured is False


def test_manual_capture_request_saves_current_frame(core, monkeypatch, win32gui_stub, fast_options):
    fake = FakeCapture([solid(0)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    events = []
    watcher = core.SlideWatcher(fast_options, _fast_config(core))
    watcher.request_capture()

    def on_event(event):
        events.append(event)
        if isinstance(event, core.CaptureEvent) and event.tag == "manual":
            watcher.stop()

    watcher._on_event = on_event
    watcher.run()

    tags = [e.tag for e in events if isinstance(e, core.CaptureEvent)]
    assert tags == ["initial", "manual"]


def test_watch_loop_ends_when_window_is_lost(core, monkeypatch, win32gui_stub, fast_options):
    fake = FakeCapture([solid(0)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")] if fake.calls == 0 else [])
    monkeypatch.setattr(win32gui_stub, "IsWindow", lambda hwnd: fake.calls < 1)

    fast_options.max_missing = 3
    events = []
    watcher = core.SlideWatcher(fast_options, _fast_config(core), on_event=events.append)
    watcher.run()

    stopped = events[-1]
    assert isinstance(stopped, core.StoppedEvent)
    assert stopped.reason == "window_lost"
    lost_logs = [e for e in events if isinstance(e, core.LogEvent) and "재탐색" in e.message]
    assert len(lost_logs) == 3


def test_run_raises_when_no_window_matches(core, monkeypatch, win32gui_stub, fast_options):
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [])
    watcher = core.SlideWatcher(fast_options, _fast_config(core))
    with pytest.raises(core.WindowNotFoundError):
        watcher.run()


def test_run_raises_when_first_capture_fails(core, monkeypatch, win32gui_stub, fast_options):
    monkeypatch.setattr(core, "capture_window_printwindow", lambda hwnd: None)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])
    watcher = core.SlideWatcher(fast_options, _fast_config(core))
    with pytest.raises(core.CaptureError):
        watcher.run()


def test_stop_interrupts_debounce_wait(core, monkeypatch, win32gui_stub, tmp_path):
    import threading
    import time

    frames = [solid(0), solid(0), solid(255)]
    fake = FakeCapture(frames)
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    options = core.WatchOptions(title="fake", outdir=tmp_path, interval=0.0)
    config = make_config(core, debounce_ms=60_000, max_stabilize_retries=5)
    watcher = core.SlideWatcher(options, config)

    def on_event(event):
        if isinstance(event, core.CandidateEvent):
            threading.Timer(0.05, watcher.stop).start()

    watcher._on_event = on_event
    started = time.monotonic()
    watcher.run()
    assert time.monotonic() - started < 5.0


def test_calibrate_emits_metrics_and_writes_csv(core, monkeypatch, win32gui_stub, tmp_path):
    frames = [solid(0), solid(0), solid(255)]
    fake = FakeCapture(frames)
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    options = core.WatchOptions(title="fake", outdir=tmp_path, interval=0.0)
    events = []
    watcher = core.SlideWatcher(options, _fast_config(core))

    def on_event(event):
        events.append(event)
        if isinstance(event, core.MetricsEvent) and event.diff.is_candidate:
            watcher.stop()

    watcher._on_event = on_event
    csv_path = tmp_path / "metrics.csv"
    frames_measured = watcher.run_calibrate(csv_path)

    metrics = [e for e in events if isinstance(e, core.MetricsEvent)]
    assert frames_measured == len(metrics) == 2
    assert metrics[-1].diff.reason in ("global", "grid")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("timestamp,ssim,bbox_ratio")
    assert len(lines) == 3
    assert not list((tmp_path).glob("slide_*.png"))  # calibrate는 저장하지 않는다


def test_explicit_hwnd_is_preferred_over_title_search(core, monkeypatch, win32gui_stub, tmp_path):
    fake = FakeCapture([solid(0)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: pytest.fail("title search should not run"))
    monkeypatch.setattr(win32gui_stub, "GetWindowText", lambda hwnd: f"Exact {hwnd}")

    options = core.WatchOptions(title="fake", outdir=tmp_path, interval=0.0, hwnd=7)
    watcher = core.SlideWatcher(options, _fast_config(core))
    watcher._on_event = lambda e: watcher.stop() if isinstance(e, core.CaptureEvent) else None
    watcher.run()

    assert watcher.hwnd == 7
    assert watcher.window_title == "Exact 7"


def test_stale_hwnd_falls_back_to_title_search(core, monkeypatch, win32gui_stub, tmp_path):
    fake = FakeCapture([solid(0)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(3, "Found by title")])
    monkeypatch.setattr(win32gui_stub, "IsWindow", lambda hwnd: hwnd != 7)

    options = core.WatchOptions(title="fake", outdir=tmp_path, interval=0.0, hwnd=7)
    watcher = core.SlideWatcher(options, _fast_config(core))
    watcher._on_event = lambda e: watcher.stop() if isinstance(e, core.CaptureEvent) else None
    watcher.run()

    assert watcher.hwnd == 3
    assert watcher.window_title == "Found by title"


def test_stop_requested_before_run_is_not_lost(core, monkeypatch, win32gui_stub, fast_options):
    fake = FakeCapture([solid(0), solid(0), solid(255), solid(255)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    events = []
    watcher = core.SlideWatcher(fast_options, _fast_config(core), on_event=events.append)
    watcher.stop()  # 스레드 start 직후, run()이 시작되기 전에 들어온 stop 요청을 흉내낸다
    saved = watcher.run()

    assert saved == 1  # initial만 저장하고 루프에 들어가자마자 종료
    assert isinstance(events[-1], core.StoppedEvent)
    assert events[-1].reason == "stopped"


def test_stop_requested_before_calibrate_is_not_lost(core, monkeypatch, win32gui_stub, tmp_path):
    fake = FakeCapture([solid(0), solid(255)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    options = core.WatchOptions(title="fake", outdir=tmp_path, interval=0.0)
    events = []
    watcher = core.SlideWatcher(options, _fast_config(core), on_event=events.append)
    watcher.stop()
    frames = watcher.run_calibrate()

    assert frames == 0
    assert isinstance(events[-1], core.StoppedEvent)


def test_reset_allows_rerun(core, monkeypatch, win32gui_stub, fast_options):
    fake = FakeCapture([solid(0)])
    monkeypatch.setattr(core, "capture_window_printwindow", fake)
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [(1, "Fake Window")])

    watcher = core.SlideWatcher(fast_options, _fast_config(core))
    watcher.stop()
    assert watcher.run() == 1
    watcher.reset()
    assert not watcher.stop_event.is_set()
    assert watcher.save_count == 0
    watcher._on_event = lambda e: watcher.stop() if isinstance(e, core.CaptureEvent) else None
    assert watcher.run() == 1
