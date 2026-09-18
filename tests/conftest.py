"""
Windows 전용 win32 모듈을 stub으로 대체해 Linux/CI에서도 core를 import할 수 있게 한다.
개별 테스트는 ``win32gui`` stub에 필요한 함수를 monkeypatch로 추가한다.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _name in ("win32con", "win32gui", "win32ui"):
    sys.modules.setdefault(_name, types.ModuleType(_name))


@pytest.fixture(scope="session")
def core():
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    pytest.importorskip("PIL.Image")
    import capture_core

    return capture_core


@pytest.fixture
def win32gui_stub(monkeypatch):
    """테스트마다 깨끗한 win32gui stub 함수를 설정할 수 있게 한다."""
    module = sys.modules["win32gui"]
    monkeypatch.setattr(module, "IsWindow", lambda hwnd: True, raising=False)
    monkeypatch.setattr(module, "IsWindowVisible", lambda hwnd: True, raising=False)
    monkeypatch.setattr(module, "GetWindowText", lambda hwnd: f"Window {hwnd}", raising=False)
    return module
