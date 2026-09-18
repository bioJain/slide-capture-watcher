"""
capture_core.py
===============

슬라이드 캡처 감시기의 GUI-agnostic core 모듈.

CLI(``slide_capture_watcher.py``)와 GUI가 공통으로 import해서 쓰는 부분을 모아 두었습니다.
이 모듈은 콘솔 출력이나 ``sys.exit`` 을 직접 하지 않습니다. 진행 상황은 이벤트 객체
(:class:`LogEvent`, :class:`CaptureEvent`, :class:`CandidateEvent`,
:class:`CandidateResolvedEvent`, :class:`MetricsEvent`, :class:`StoppedEvent`)를 콜백으로 전달하고, 치명적인 오류는 :class:`WatcherError` 계열
예외로 올립니다.

스레드 모델
-----------
:class:`SlideWatcher.run` 은 블로킹 루프이므로 GUI에서는 ``threading.Thread`` 로 돌리고,
콜백 안에서는 위젯을 직접 만지지 말고 ``queue.Queue`` 에 이벤트를 넣은 뒤 메인 스레드가
``root.after()`` 폴링으로 큐를 비우면서 화면을 갱신하십시오. 콜백은 캡처 스레드에서
호출됩니다. 중지는 :meth:`SlideWatcher.stop`, 수동 캡처는 :meth:`SlideWatcher.request_capture`
로 다른 스레드에서 안전하게 요청할 수 있습니다.

DPI 주의
--------
이 모듈을 import하는 순간 프로세스를 DPI-aware로 선언합니다(:func:`_set_dpi_awareness`).
``GetWindowRect`` 와 ``PrintWindow`` 의 좌표계를 맞추기 위한 것이므로, 다른 win32 API를
호출하는 코드보다 **먼저** 이 모듈을 import해야 합니다.

SSIM
----
``scikit-image`` 의존성을 없애기 위해 :func:`compute_ssim` 을 OpenCV만으로 구현했습니다.
``skimage.metrics.structural_similarity`` 의 기본 설정(7x7 균일 윈도우, 표본 공분산,
K1=0.01, K2=0.03, 8비트 동적 범위)과 같은 수식이라 기존 임계치를 그대로 쓸 수 있습니다.
"""

from __future__ import annotations

import ctypes
import csv
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image

try:
    import win32gui
    import win32ui
except ImportError:
    print("pywin32가 필요합니다. 'pip install pywin32' 로 설치해주세요.", file=sys.stderr)
    raise

PW_RENDERFULLCONTENT = 2

FracRect = Tuple[float, float, float, float]
FULL_FRAME: FracRect = (0.0, 0.0, 1.0, 1.0)


def _set_dpi_awareness() -> None:
    """
    화면 배율(디스플레이 확대/축소, 예: 125%/150%)이 100%가 아닌 모니터에서
    이 프로세스가 'DPI-aware'로 선언되지 않으면, GetWindowRect가 돌려주는 좌표가
    Windows의 DPI 가상화 레이어를 거친 축소된 값으로 나옵니다. 반면 PrintWindow는
    실제 물리 픽셀 기준으로 렌더링하기 때문에, 이 둘의 크기가 어긋나서 캡처한
    비트맵이 창의 왼쪽 위 일부만 채워지고 나머지는 잘려 나가는 증상이 생깁니다.

    모듈 import 시점에 프로세스를 DPI-aware로 만들어 GetWindowRect와 PrintWindow가
    같은 좌표계(물리 픽셀)를 쓰도록 맞춥니다.
    """
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_set_dpi_awareness()


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------

class WatcherError(Exception):
    """core에서 올리는 예외의 공통 베이스."""


class WindowNotFoundError(WatcherError):
    """제목에 맞는 보이는 창을 찾지 못했을 때."""


class CaptureError(WatcherError):
    """첫 캡처에 실패했을 때(창 최소화 등)."""


# ---------------------------------------------------------------------------
# 창 찾기
# ---------------------------------------------------------------------------

def enumerate_visible_windows() -> List[Tuple[int, str]]:
    """제목이 있는 '보이는' 최상위 창의 (hwnd, title) 목록."""
    windows: List[Tuple[int, str]] = []

    def _enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and title.strip():
            windows.append((hwnd, title))

    win32gui.EnumWindows(_enum_handler, None)
    return windows


def find_windows_by_title(substring: str) -> List[Tuple[int, str]]:
    """제목에 substring(대소문자 무시)이 포함된 '보이는' 최상위 창 목록을 반환."""
    needle = substring.lower()
    return [(hwnd, title) for hwnd, title in enumerate_visible_windows() if needle in title.lower()]


def list_all_windows() -> List[str]:
    """보이는 창 제목만 모은 목록(CLI ``--list-windows`` 용)."""
    return [title for _, title in enumerate_visible_windows()]


# ---------------------------------------------------------------------------
# 캡처 백엔드
# ---------------------------------------------------------------------------

def capture_window_printwindow(hwnd) -> Optional[Image.Image]:
    """PrintWindow(PW_RENDERFULLCONTENT)로 창 내용을 캡처. occlusion에 비교적 강함."""
    if not win32gui.IsWindow(hwnd):
        return None

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    save_bitmap = win32ui.CreateBitmap()
    try:
        save_bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(save_bitmap)
        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)

        bmp_info = save_bitmap.GetInfo()
        bmp_bits = save_bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits,
            "raw",
            "BGRX",
            0,
            1,
        )
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

    if not result:
        return None
    return img


def capture_window_region(hwnd) -> Optional[Image.Image]:
    """실제 화면 좌표를 그대로 캡처(mss). 창이 가려지지 않고 보여야 정상 동작."""
    import mss

    if not win32gui.IsWindow(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": w, "height": h})
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


CAPTURE_MODES = ("window", "region")


def select_capture_fn(mode: str) -> Callable[[int], Optional[Image.Image]]:
    """캡처 모드 문자열을 백엔드 함수로 변환한다. 호출 시점의 모듈 전역을 참조한다."""
    if mode == "window":
        return capture_window_printwindow
    if mode == "region":
        return capture_window_region
    raise ValueError(f"알 수 없는 캡처 모드: {mode!r} (window 또는 region)")


def is_blank(img: Image.Image, std_threshold: float = 2.0) -> bool:
    """PrintWindow가 검은 화면(또는 단색)만 반환했는지 대략적으로 판별."""
    arr = np.asarray(img.convert("L"))
    return float(arr.std()) < std_threshold


# ---------------------------------------------------------------------------
# ROI / 제외 영역 / 썸네일 전처리
# ---------------------------------------------------------------------------

def parse_frac_rect(value: str) -> FracRect:
    """``L,T,W,H`` 형식의 프레임 비율 영역을 파싱한다. 잘못되면 ``ValueError``."""
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError as exc:
        raise ValueError(
            f"'{value}': 숫자 4개를 쉼표로 구분해서 입력하세요 (예: 0.1,0.1,0.8,0.8)"
        ) from exc
    if len(parts) != 4:
        raise ValueError(f"'{value}': L,T,W,H 형식(값 4개)이어야 합니다")
    left, top, width, height = parts
    if not (
        0.0 <= left < 1.0
        and 0.0 <= top < 1.0
        and 0.0 < width <= 1.0
        and 0.0 < height <= 1.0
    ):
        raise ValueError(f"'{value}': 각 값은 0~1 사이여야 하고 W,H는 0보다 커야 합니다")
    if left + width > 1.0 + 1e-6 or top + height > 1.0 + 1e-6:
        raise ValueError(f"'{value}': L+W 또는 T+H가 1을 넘습니다(영역이 프레임을 벗어남)")
    return left, top, width, height


def parse_grid(value: str) -> Tuple[int, int]:
    """``RxC`` 그리드 크기를 파싱한다. 0/off/none은 판정을 비활성화한다."""
    normalized = value.lower().strip()
    if normalized in ("0x0", "0", "none", "off"):
        return 0, 0
    try:
        rows_text, cols_text = normalized.split("x")
        rows, cols = int(rows_text), int(cols_text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"'{value}': RxC 형식(예: 3x3) 또는 0x0(끄기)이어야 합니다") from exc
    if rows <= 0 or cols <= 0:
        raise ValueError(f"'{value}': 행과 열은 양수여야 합니다(끄려면 0x0 사용)")
    return rows, cols


def crop_frac(img: Image.Image, frac_rect: FracRect) -> Image.Image:
    """썸네일 생성 전에 원본 이미지에서 비율 영역을 크롭한다."""
    left, top, width, height = frac_rect
    if tuple(frac_rect) == FULL_FRAME:
        return img
    image_width, image_height = img.size
    box = (
        max(0, int(left * image_width)),
        max(0, int(top * image_height)),
        min(image_width, int((left + width) * image_width)),
        min(image_height, int((top + height) * image_height)),
    )
    return img.crop(box)


def make_thumbnail_gray(img: Image.Image, width: int = 400) -> np.ndarray:
    image_width, image_height = img.size
    if image_width == 0 or image_height == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    new_height = max(1, int(image_height * (width / image_width)))
    small = img.resize((width, new_height), Image.BILINEAR)
    return cv2.cvtColor(np.asarray(small.convert("RGB")), cv2.COLOR_RGB2GRAY)


def apply_exclusions_gray(gray: np.ndarray, excludes: List[FracRect]) -> np.ndarray:
    """ROI 내부 제외 영역을 중간 회색으로 마스킹한다. 좌표는 크롭된 ROI 기준 비율."""
    if not excludes:
        return gray
    output = gray.copy()
    image_height, image_width = output.shape
    for left, top, width, height in excludes:
        x0, y0 = int(left * image_width), int(top * image_height)
        x1, y1 = int((left + width) * image_width), int((top + height) * image_height)
        output[y0:y1, x0:x1] = 128
    return output


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """변화 감지 파라미터. GUI 설정 패널이 편집하는 값들."""

    roi: FracRect = FULL_FRAME
    exclude: List[FracRect] = field(default_factory=list)
    thumb_width: int = 400
    ssim_threshold: float = 0.90
    min_region_ratio: float = 0.15
    pixel_diff_threshold: int = 25
    grid_rows: int = 3
    grid_cols: int = 3
    min_cell_ratio: float = 0.35
    stability_ssim: float = 0.985
    debounce_ms: int = 600
    max_stabilize_retries: int = 5

    @property
    def grid_enabled(self) -> bool:
        return self.grid_rows > 0 and self.grid_cols > 0


@dataclass
class WatchOptions:
    """한 번의 감시 세션에 대한 옵션(무엇을, 어디에, 어떻게 캡처할지)."""

    title: str
    outdir: Path
    mode: str = "window"
    interval: float = 1.0
    max_missing: int = 5
    debug_diff: bool = False
    hwnd: Optional[int] = None  # 지정하면 이 창을 우선 사용(GUI 드롭다운 선택). 재탐색은 title로.

    def __post_init__(self) -> None:
        self.outdir = Path(self.outdir)
        if self.mode not in CAPTURE_MODES:
            raise ValueError(f"알 수 없는 캡처 모드: {self.mode!r} (window 또는 region)")


def prepare_gray(img: Image.Image, cfg: Config) -> np.ndarray:
    """원본을 ROI 크롭, 축소, 제외 영역 마스킹 순서로 전처리한다."""
    gray = make_thumbnail_gray(crop_frac(img, cfg.roi), cfg.thumb_width)
    return apply_exclusions_gray(gray, cfg.exclude)


# ---------------------------------------------------------------------------
# SSIM (OpenCV 구현)
# ---------------------------------------------------------------------------

SSIM_WINDOW = 7
SSIM_K1 = 0.01
SSIM_K2 = 0.03
SSIM_DATA_RANGE = 255.0


def compute_ssim(gray_a: np.ndarray, gray_b: np.ndarray) -> float:
    """
    두 8비트 그레이스케일 이미지의 평균 SSIM.

    ``skimage.metrics.structural_similarity`` 기본 동작(7x7 균일 윈도우, 표본 공분산 보정,
    가장자리 (win-1)/2 픽셀 제외)을 그대로 따른다. 크기가 다르면 ``gray_b`` 를 ``gray_a``
    크기로 리사이즈한다. 윈도우보다 작은 이미지는 가능한 홀수 윈도우로 줄여 계산하고,
    3px 미만이면 완전 일치 여부만 본다.
    """
    if gray_a.shape != gray_b.shape:
        gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))

    height, width = gray_a.shape[:2]
    win = min(SSIM_WINDOW, height, width)
    if win % 2 == 0:
        win -= 1
    if win < 3:
        return 1.0 if np.array_equal(gray_a, gray_b) else 0.0

    a = gray_a.astype(np.float64)
    b = gray_b.astype(np.float64)

    def _mean(x: np.ndarray) -> np.ndarray:
        return cv2.blur(x, (win, win), borderType=cv2.BORDER_REFLECT)

    ux, uy = _mean(a), _mean(b)
    uxx, uyy, uxy = _mean(a * a), _mean(b * b), _mean(a * b)
    n = win * win
    cov_norm = n / (n - 1)
    vx = cov_norm * (uxx - ux * ux)
    vy = cov_norm * (uyy - uy * uy)
    vxy = cov_norm * (uxy - ux * uy)

    c1 = (SSIM_K1 * SSIM_DATA_RANGE) ** 2
    c2 = (SSIM_K2 * SSIM_DATA_RANGE) ** 2
    ssim_map = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))

    pad = (win - 1) // 2
    if pad:
        ssim_map = ssim_map[pad:-pad, pad:-pad]
    return float(ssim_map.mean())


def safe_ssim(gray_a: np.ndarray, gray_b: np.ndarray) -> float:
    """하위 호환용 별칭."""
    return compute_ssim(gray_a, gray_b)


# ---------------------------------------------------------------------------
# 변화 감지
# ---------------------------------------------------------------------------

@dataclass
class DiffResult:
    ssim_value: float
    bbox_ratio: float
    grid_ratio: float
    grid_cell: Optional[Tuple[int, int]]
    is_candidate: bool
    reason: str
    mask: np.ndarray


def grid_max_ratio(mask: np.ndarray, rows: int, cols: int) -> Tuple[float, Optional[Tuple[int, int]]]:
    """그리드 셀별 변경 픽셀 비율의 최댓값과 셀 좌표를 반환한다."""
    if rows <= 0 or cols <= 0:
        return 0.0, None
    image_height, image_width = mask.shape
    best_ratio = 0.0
    best_cell = None
    for row in range(rows):
        y0, y1 = image_height * row // rows, image_height * (row + 1) // rows
        for col in range(cols):
            x0, x1 = image_width * col // cols, image_width * (col + 1) // cols
            cell = mask[y0:y1, x0:x1]
            if not cell.size:
                continue
            ratio = float(np.count_nonzero(cell)) / cell.size
            if ratio > best_ratio:
                best_ratio, best_cell = ratio, (row, col)
    return best_ratio, best_cell


def compare_frames(gray_a: np.ndarray, gray_b: np.ndarray, cfg: Config) -> DiffResult:
    if gray_a.shape != gray_b.shape:
        # 창 크기가 바뀐 경우: 리사이즈해서 비교(대략적 처리)
        gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))

    score = compute_ssim(gray_a, gray_b)

    diff = cv2.absdiff(gray_a, gray_b)
    _, mask = cv2.threshold(diff, cfg.pixel_diff_threshold, 255, cv2.THRESH_BINARY)
    coords = cv2.findNonZero(mask)
    if coords is None:
        bbox_ratio = 0.0
    else:
        x, y, w, h = cv2.boundingRect(coords)
        frame_area = gray_a.shape[0] * gray_a.shape[1]
        bbox_ratio = (w * h) / frame_area if frame_area else 0.0

    grid_ratio, grid_cell = grid_max_ratio(mask, cfg.grid_rows, cfg.grid_cols)
    global_hit = score < cfg.ssim_threshold and bbox_ratio >= cfg.min_region_ratio
    grid_hit = cfg.grid_enabled and grid_ratio >= cfg.min_cell_ratio
    reason = "global" if global_hit else "grid" if grid_hit else "none"
    return DiffResult(score, bbox_ratio, grid_ratio, grid_cell, global_hit or grid_hit, reason, mask)


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------

@dataclass
class LogEvent:
    message: str
    level: str = "info"  # info | warning | error


@dataclass
class CaptureEvent:
    path: Path
    count: int
    tag: str = ""
    mask_path: Optional[Path] = None


@dataclass
class CandidateEvent:
    """후보 변화 감지(안정화 대기 시작)."""

    diff: DiffResult


@dataclass
class CandidateResolvedEvent:
    """후보 변화의 안정화 판정이 끝남. ``captured`` 가 False면 안정화 실패로 건너뛴 것."""

    captured: bool


@dataclass
class MetricsEvent:
    """캘리브레이션 모드의 프레임별 지표."""

    timestamp: str
    diff: DiffResult


@dataclass
class StoppedEvent:
    reason: str  # stopped | window_lost
    save_count: int
    outdir: Optional[Path] = None


Event = Union[LogEvent, CaptureEvent, CandidateEvent, CandidateResolvedEvent, MetricsEvent, StoppedEvent]
EventCallback = Callable[[Event], None]


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# 감시 루프
# ---------------------------------------------------------------------------

class SlideWatcher:
    """
    창을 감시하다가 슬라이드가 안정적으로 바뀐 시점에만 캡처를 저장한다.

    사용 예::

        watcher = SlideWatcher(WatchOptions("Zoom", "captures"), Config(), on_event=queue.put)
        threading.Thread(target=watcher.run, daemon=True).start()
        ...
        watcher.stop()

    인스턴스는 한 번의 실행을 전제로 한다. 스레드 start 직후 들어온 stop() 요청이 유실되지 않도록
    run() 안에서 stop_event를 초기화하지 않으므로, 재실행하려면 start 전에 reset()을 호출한다.
    """

    def __init__(
        self,
        options: WatchOptions,
        config: Optional[Config] = None,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.options = options
        self.config = config or Config()
        self._on_event = on_event or (lambda event: None)
        self.stop_event = threading.Event()
        self.capture_request = threading.Event()
        self.save_count = 0
        self.hwnd: Optional[int] = None
        self.window_title: Optional[str] = None
        self._blank_warned = False

    # -- 외부 제어 (다른 스레드에서 호출 가능) ---------------------------------

    def stop(self) -> None:
        """감시 루프를 가능한 한 빨리 종료한다(디바운스 대기 중에도 즉시)."""
        self.stop_event.set()

    def request_capture(self) -> None:
        """다음 폴링에서 변화 여부와 무관하게 현재 프레임을 저장하도록 요청한다."""
        self.capture_request.set()

    def reset(self) -> None:
        """
        같은 인스턴스로 다시 run()/run_calibrate() 하기 전에 호출한다.
        stop/캡처 요청 플래그와 저장 카운터를 초기화한다. 워커 스레드를 start하기 **전에** 호출할 것.
        """
        self.stop_event.clear()
        self.capture_request.clear()
        self.save_count = 0
        self._blank_warned = False

    # -- 내부 유틸 ---------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        self._on_event(event)

    def _log(self, message: str, level: str = "info") -> None:
        self._emit(LogEvent(message, level))

    def _wait(self, seconds: float) -> bool:
        """``seconds`` 만큼 대기. 그 사이 stop 요청이 오면 True."""
        if seconds <= 0:
            return self.stop_event.is_set()
        return self.stop_event.wait(seconds)

    def _locate_window(self) -> None:
        if self.options.hwnd is not None and win32gui.IsWindow(self.options.hwnd):
            self.hwnd = self.options.hwnd
            self.window_title = win32gui.GetWindowText(self.hwnd) or self.options.title
            return
        matches = find_windows_by_title(self.options.title)
        if not matches:
            raise WindowNotFoundError(
                f"제목에 '{self.options.title}' 이(가) 포함된 보이는 창을 찾지 못했습니다."
            )
        if len(matches) > 1:
            self._log(f"'{self.options.title}' 로 매칭되는 창이 {len(matches)}개입니다. 첫 번째를 사용합니다:")
            for _, title in matches:
                self._log(f"  - {title}")
        self.hwnd, self.window_title = matches[0]

    def _grab(self, warn_blank: bool = True) -> Optional[Image.Image]:
        capture_fn = select_capture_fn(self.options.mode)
        img = capture_fn(self.hwnd)
        if img is not None and warn_blank and self.options.mode == "window":
            blank = is_blank(img)
            if blank and not self._blank_warned:
                self._log(
                    "경고: PrintWindow 결과가 빈 화면(검은색)입니다. 이 창은 하드웨어 비디오 오버레이를 "
                    "쓰고 있을 수 있습니다. region 모드 사용을 고려하세요.",
                    level="warning",
                )
            self._blank_warned = blank
        return img

    def _save(self, img: Image.Image, tag: str = "", mask: Optional[np.ndarray] = None) -> Path:
        self.save_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        path = self.options.outdir / f"slide_{ts}{suffix}.png"
        # 같은 초에 두 번 저장되면 덮어쓰지 않도록 번호를 붙인다.
        counter = 1
        while path.exists():
            counter += 1
            path = self.options.outdir / f"slide_{ts}{suffix}_{counter}.png"
        img.save(path)
        mask_path: Optional[Path] = None
        if mask is not None:
            mask_path = path.with_name(path.stem + "_diffmask.png")
            Image.fromarray(mask).save(mask_path)
        self._emit(CaptureEvent(path, self.save_count, tag, mask_path))
        return path

    def _handle_missing_window(self, consecutive_missing: int) -> bool:
        """창이 사라졌을 때 재탐색. 계속 감시 가능하면 True."""
        self._log(f"대상 창이 사라진 것 같습니다 ({consecutive_missing}회 연속). 재탐색 시도...", "warning")
        refound = find_windows_by_title(self.options.title)
        if refound:
            self.hwnd, self.window_title = refound[0]
            self._log(f"창을 다시 찾았습니다: \"{self.window_title}\"")
            return True
        return consecutive_missing < self.options.max_missing

    # -- 메인 루프 ---------------------------------------------------------------

    def run(self) -> int:
        """
        감시 루프. :meth:`stop` 이 호출되거나 창을 계속 찾을 수 없을 때 끝난다.
        반환값은 저장한 이미지 수. 시작 단계의 실패는 예외로 올린다.
        """
        cfg = self.config
        opts = self.options
        # stop_event는 여기서 clear하지 않는다. 스레드를 start한 직후 run()이 이 줄에 닿기 전에
        # 들어온 stop() 요청이 지워지는 경쟁을 막기 위한 것. 재사용하려면 reset()을 먼저 호출.
        self._locate_window()
        self._log(f"대상 창: \"{self.window_title}\" (mode={opts.mode})")

        opts.outdir.mkdir(parents=True, exist_ok=True)

        baseline_img = self._grab()
        if baseline_img is None:
            raise CaptureError("첫 캡처에 실패했습니다. 창이 최소화되어 있지 않은지 확인해주세요.")
        baseline_gray = prepare_gray(baseline_img, cfg)
        self._save(baseline_img, tag="initial")

        self._log(
            f"감시 시작 (interval={opts.interval}s, roi={cfg.roi}, exclude={cfg.exclude}, "
            f"ssim_threshold={cfg.ssim_threshold}, min_region_ratio={cfg.min_region_ratio}, "
            f"grid={cfg.grid_rows}x{cfg.grid_cols}, min_cell_ratio={cfg.min_cell_ratio}, "
            f"pixel_diff_threshold={cfg.pixel_diff_threshold})"
        )

        reason = "stopped"
        consecutive_missing = 0

        while True:
            if self._wait(opts.interval):
                break

            if not win32gui.IsWindow(self.hwnd):
                consecutive_missing += 1
                if not self._handle_missing_window(consecutive_missing):
                    self._log("창을 계속 찾을 수 없어 종료합니다.", "error")
                    reason = "window_lost"
                    break
                continue
            consecutive_missing = 0

            current_img = self._grab()
            if current_img is None:
                continue
            current_gray = prepare_gray(current_img, cfg)

            if self.capture_request.is_set():
                self.capture_request.clear()
                self._log("수동 캡처 요청 -> 현재 프레임 저장")
                self._save(current_img, tag="manual")
                baseline_img, baseline_gray = current_img, current_gray
                continue

            diff = compare_frames(baseline_gray, current_gray, cfg)
            if not diff.is_candidate:
                # 별 변화 없음 (또는 마우스/포인터 수준의 국소 변화) -> 기준 프레임 업데이트하지 않음
                continue

            self._emit(CandidateEvent(diff))
            self._log(
                f"후보 변화 감지 (reason={diff.reason}, ssim={diff.ssim_value:.3f}, "
                f"bbox_ratio={diff.bbox_ratio:.3f}, grid_ratio={diff.grid_ratio:.3f}"
                + (f", cell={diff.grid_cell}" if diff.grid_cell is not None else "")
                + ") -> 안정화 대기 중..."
            )
            candidate_mask = diff.mask

            # 디바운스 + 안정화 체크
            stabilized = False
            interrupted = False
            check_img, check_gray = current_img, current_gray
            for attempt in range(1, cfg.max_stabilize_retries + 1):
                if self._wait(cfg.debounce_ms / 1000.0):
                    interrupted = True
                    break
                next_img = self._grab()
                if next_img is None:
                    continue
                next_gray = prepare_gray(next_img, cfg)
                raw_score = compute_ssim(check_gray, next_gray)
                check_img, check_gray = next_img, next_gray

                if raw_score >= cfg.stability_ssim:
                    stabilized = True
                    break

                self._log(
                    f"  아직 불안정함 (시도 {attempt}/{cfg.max_stabilize_retries}, "
                    f"raw_ssim={raw_score:.3f}) - 전환 애니메이션 또는 동영상 재생 중일 수 있음"
                )

            if interrupted:
                break

            if stabilized:
                self._save(check_img, mask=candidate_mask if opts.debug_diff else None)
            else:
                self._log(
                    "  안정화 실패 -> 이번 전환은 캡처하지 않고 건너뜁니다 "
                    "(동영상 재생 등으로 계속 변하는 중일 가능성).",
                    "warning",
                )
            self._emit(CandidateResolvedEvent(stabilized))
            baseline_img, baseline_gray = check_img, check_gray

        self._emit(StoppedEvent(reason, self.save_count, opts.outdir))
        return self.save_count

    # -- 캘리브레이션 ------------------------------------------------------------

    def run_calibrate(self, csv_path: Optional[Union[str, Path]] = None) -> int:
        """
        저장 없이 연속 프레임의 감지 지표를 :class:`MetricsEvent` 로 내보낸다.
        ``csv_path`` 를 주면 같은 값을 CSV로도 기록한다. 반환값은 측정한 프레임 수.
        """
        cfg = self.config
        opts = self.options
        self._locate_window()
        self._log(f"[calibrate] 대상 창: \"{self.window_title}\" (mode={opts.mode})")

        csv_file = None
        writer = None
        frames = 0
        reason = "stopped"
        try:
            if csv_path:
                csv_file = open(csv_path, "w", encoding="utf-8", newline="")
                writer = csv.writer(csv_file)
                writer.writerow(["timestamp", "ssim", "bbox_ratio", "grid_ratio", "grid_cell", "candidate", "reason"])

            previous_img = self._grab(warn_blank=False)
            if previous_img is None:
                raise CaptureError("첫 캡처에 실패했습니다. 창이 최소화되어 있지 않은지 확인해주세요.")
            previous_gray = prepare_gray(previous_img, cfg)

            while not self._wait(opts.interval):
                if not win32gui.IsWindow(self.hwnd):
                    self._log("[calibrate] 대상 창이 사라졌습니다. 종료합니다.", "error")
                    reason = "window_lost"
                    break
                current_img = self._grab(warn_blank=False)
                if current_img is None:
                    continue
                current_gray = prepare_gray(current_img, cfg)
                diff = compare_frames(previous_gray, current_gray, cfg)
                timestamp = _timestamp()
                frames += 1
                self._emit(MetricsEvent(timestamp, diff))
                if writer is not None:
                    cell = "" if diff.grid_cell is None else f"{diff.grid_cell[0]}:{diff.grid_cell[1]}"
                    writer.writerow([
                        timestamp,
                        f"{diff.ssim_value:.4f}",
                        f"{diff.bbox_ratio:.4f}",
                        f"{diff.grid_ratio:.4f}",
                        cell,
                        diff.is_candidate,
                        diff.reason,
                    ])
                    csv_file.flush()
                previous_gray = current_gray
        finally:
            if csv_file:
                csv_file.close()

        self._emit(StoppedEvent(reason, 0, None))
        return frames
