import importlib.util
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


@pytest.fixture(scope="module")
def watcher():
    for name in ("win32con", "win32gui", "win32ui"):
        sys.modules.setdefault(name, types.ModuleType(name))
    path = Path(__file__).parents[1] / "slide_capture_watcher.py"
    spec = importlib.util.spec_from_file_location("slide_capture_watcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_config(watcher, **overrides):
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
    return watcher.Config(**values)


def test_parse_roi_and_grid_validation(watcher):
    assert watcher.parse_frac_rect("0.1,0.2,0.8,0.7") == (0.1, 0.2, 0.8, 0.7)
    assert watcher.parse_grid("off") == (0, 0)
    assert watcher.parse_grid("3x4") == (3, 4)

    with pytest.raises(Exception):
        watcher.parse_frac_rect("0.5,0,0.6,1")
    with pytest.raises(Exception):
        watcher.parse_grid("-1x3")


def test_prepare_gray_crops_before_resizing_and_masks_exclusion(watcher):
    image = Image.fromarray(np.full((20, 40, 3), 255, dtype=np.uint8))
    config = make_config(
        watcher,
        roi=(0.25, 0.0, 0.5, 1.0),
        exclude=[(0.5, 0.0, 0.5, 1.0)],
    )

    gray = watcher.prepare_gray(image, config)

    assert gray.shape == (20, 20)
    assert np.all(gray[:, 10:] == 128)
    assert np.all(gray[:, :10] == 255)


def test_grid_path_detects_local_change_when_global_path_does_not(watcher):
    baseline = np.zeros((20, 20), dtype=np.uint8)
    changed = baseline.copy()
    changed[:10, :10] = 255
    config = make_config(watcher, ssim_threshold=0.0, min_cell_ratio=0.9)

    result = watcher.compare_frames(baseline, changed, config)

    assert result.is_candidate is True
    assert result.reason == "grid"
    assert result.grid_ratio == 1.0
    assert result.grid_cell == (0, 0)


def test_grid_can_be_disabled(watcher):
    baseline = np.zeros((20, 20), dtype=np.uint8)
    changed = baseline.copy()
    changed[:10, :10] = 255
    config = make_config(watcher, ssim_threshold=0.0, grid_rows=0, grid_cols=0)

    result = watcher.compare_frames(baseline, changed, config)

    assert result.is_candidate is False
    assert result.reason == "none"
    assert result.grid_cell is None
