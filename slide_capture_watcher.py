"""
slide_capture_watcher.py (v2)
==============================

특정 창(윈도우)을 지속적으로 모니터링하다가, "슬라이드가 완전히 넘어갔다"고 판단되는
순간에만 스크린샷을 자동 저장하는 Windows 전용 스크립트.

설계 개요
---------
1. 저해상도 폴링: --roi로 슬라이드 영역을 먼저 크롭한 뒤 작은 썸네일(기본 폭 400px)로
   축소합니다. 창 chrome이나 레터박스가 비교 결과를 희석하지 않으며 연산도 가볍습니다.

2. 변화 판단 지표를 두 경로의 OR로 사용합니다.
   - SSIM(구조적 유사도): 마우스 커서/레이저 포인터처럼 국소적인 변화에는 거의 반응하지
     않고, 슬라이드가 바뀌면 값이 크게 떨어집니다.
   - 변화 영역의 bounding box 비율: SSIM이 조금 떨어지더라도 변화가 화면 전체가 아니라
     작은 영역(포인터, 커서)에 국한되어 있으면 무시합니다.
   - 그리드 판정: 셀 하나의 변경 픽셀 비율이 임계값을 넘는 국지적 변화도 감지합니다.
   --exclude로 ROI 내부의 화자 PIP 같은 지속적인 노이즈원을 마스킹할 수 있습니다.

3. 디바운스 + 안정화 체크: 후보 변화가 감지되면 바로 캡처하지 않고 짧게 대기한 뒤
   다시 비교해서, 전환 애니메이션(페이드 등)이 끝나고 화면이 안정됐는지 확인한 뒤에만
   최종 캡처를 저장합니다. 슬라이드 안에 삽입된 동영상처럼 계속 변화가 지속되는 경우는
   일정 횟수 재시도 후 "안정화 실패"로 간주하고 건너뜁니다(캡처하지 않음).

4. 창 캡처는 화면 좌표를 그대로 긁어오는 방식이 아니라 win32의 PrintWindow API
   (PW_RENDERFULLCONTENT 플래그)를 사용합니다. 덕분에 다른 창이 부분적으로 위에
   겹쳐 있어도 대상 창의 실제 내용을 캡처할 수 있습니다. 단, 일부 GPU 오버레이 기반
   영상 렌더링(하드웨어 비디오 오버레이)은 PrintWindow로 잡히지 않고 검은 화면으로
   나올 수 있습니다. 이 경우 --mode region 옵션으로 전환하면 실제 화면 좌표를 그대로
   캡처하는 방식(mss 사용)으로 동작합니다. 이 모드는 대상 창이 다른 창에 가려지지
   않고 화면에 그대로 보이고 있어야 정상 동작합니다.

설치
----
    pip install -r requirements.txt

사용법
------
    # 1) 먼저 캡처하고 싶은 창의 제목을 찾습니다 (부분 문자열 매칭)
    python slide_capture_watcher.py --list-windows

    # 2) 찾은 제목 일부를 --title 로 지정해서 감시 시작
    python slide_capture_watcher.py --title "Zoom Meeting" --outdir captures

    # 3) PrintWindow로 검은 화면만 나올 경우 region 모드로 전환
    python slide_capture_watcher.py --title "Zoom Meeting" --mode region

주요 옵션은 하단 argparse 정의 참고. 특히 아래 값은 실제 환경에서 튜닝이 필요합니다.
    --ssim-threshold     (기본 0.90)  : 이보다 낮아지면 "많이 달라졌다"고 판단
    --min-region-ratio   (기본 0.15)  : 변화 영역이 프레임의 이 비율 이상 넓어야 인정
    --stability-ssim     (기본 0.985) : 디바운스 후 이 값 이상 유사해야 "안정됐다"고 판단
    --grid               (기본 3x3)   : 셀 단위 국지 변화 판정(0x0이면 끔)
    --min-cell-ratio     (기본 0.35)  : 셀 내부 변경 픽셀의 최소 비율

콘솔에 매 이벤트마다 ssim/bbox_ratio 수치를 함께 출력하니, 처음 몇 번은 --outdir 결과와
콘솔 로그를 같이 보면서 임계치를 조정하시는 걸 권장합니다.
"""

import argparse
import ctypes
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

try:
    import win32con
    import win32gui
    import win32ui
except ImportError:
    print("pywin32가 필요합니다. 'pip install pywin32' 로 설치해주세요.", file=sys.stderr)
    raise

PW_RENDERFULLCONTENT = 2

FracRect = Tuple[float, float, float, float]


def _set_dpi_awareness() -> None:
    """
    화면 배율(디스플레이 확대/축소, 예: 125%/150%)이 100%가 아닌 모니터에서
    이 프로세스가 'DPI-aware'로 선언되지 않으면, GetWindowRect가 돌려주는 좌표가
    Windows의 DPI 가상화 레이어를 거친 축소된 값으로 나옵니다. 반면 PrintWindow는
    실제 물리 픽셀 기준으로 렌더링하기 때문에, 이 둘의 크기가 어긋나서 캡처한
    비트맵이 창의 왼쪽 위 일부만 채워지고 나머지는 잘려 나가는 증상이 생깁니다.
    (창 전체가 안 잡히고 잘려서 캡처되는 문제의 가장 흔한 원인)

    스크립트 시작 시점에 프로세스를 DPI-aware로 만들어 GetWindowRect와 PrintWindow가
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


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ---------------------------------------------------------------------------
# 창 찾기
# ---------------------------------------------------------------------------

def find_windows_by_title(substring: str):
    """제목에 substring(대소문자 무시)이 포함된 '보이는' 최상위 창 목록을 반환."""
    substring = substring.lower()
    matches = []

    def _enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if substring in title.lower():
            matches.append((hwnd, title))

    win32gui.EnumWindows(_enum_handler, None)
    return matches


def list_all_windows():
    windows = []

    def _enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title.strip():
            windows.append(title)

    win32gui.EnumWindows(_enum_handler, None)
    return windows


# ---------------------------------------------------------------------------
# 캡처 백엔드
# ---------------------------------------------------------------------------

def capture_window_printwindow(hwnd) -> "Image.Image | None":
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


def capture_window_region(hwnd):
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


def is_blank(img: "Image.Image", std_threshold: float = 2.0) -> bool:
    """PrintWindow가 검은 화면(또는 단색)만 반환했는지 대략적으로 판별."""
    arr = np.asarray(img.convert("L"))
    return float(arr.std()) < std_threshold


# ---------------------------------------------------------------------------
# ROI / 제외 영역 / 썸네일 전처리
# ---------------------------------------------------------------------------

def parse_frac_rect(value: str) -> FracRect:
    """``L,T,W,H`` 형식의 프레임 비율 영역을 파싱한다."""
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"'{value}': 숫자 4개를 쉼표로 구분해서 입력하세요 (예: 0.1,0.1,0.8,0.8)"
        ) from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"'{value}': L,T,W,H 형식(값 4개)이어야 합니다")
    left, top, width, height = parts
    if not (
        0.0 <= left < 1.0
        and 0.0 <= top < 1.0
        and 0.0 < width <= 1.0
        and 0.0 < height <= 1.0
    ):
        raise argparse.ArgumentTypeError(
            f"'{value}': 각 값은 0~1 사이여야 하고 W,H는 0보다 커야 합니다"
        )
    if left + width > 1.0 + 1e-6 or top + height > 1.0 + 1e-6:
        raise argparse.ArgumentTypeError(
            f"'{value}': L+W 또는 T+H가 1을 넘습니다(영역이 프레임을 벗어남)"
        )
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
        raise argparse.ArgumentTypeError(
            f"'{value}': RxC 형식(예: 3x3) 또는 0x0(끄기)이어야 합니다"
        ) from exc
    if rows <= 0 or cols <= 0:
        raise argparse.ArgumentTypeError(
            f"'{value}': 행과 열은 양수여야 합니다(끄려면 0x0 사용)"
        )
    return rows, cols


def crop_frac(img: "Image.Image", frac_rect: FracRect) -> "Image.Image":
    """썸네일 생성 전에 원본 이미지에서 비율 영역을 크롭한다."""
    left, top, width, height = frac_rect
    if frac_rect == (0.0, 0.0, 1.0, 1.0):
        return img
    image_width, image_height = img.size
    box = (
        max(0, int(left * image_width)),
        max(0, int(top * image_height)),
        min(image_width, int((left + width) * image_width)),
        min(image_height, int((top + height) * image_height)),
    )
    return img.crop(box)


def make_thumbnail_gray(img: "Image.Image", width: int = 400) -> np.ndarray:
    image_width, image_height = img.size
    if image_width == 0 or image_height == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    new_height = max(1, int(image_height * (width / image_width)))
    small = img.resize((width, new_height), Image.BILINEAR)
    return cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)


def apply_exclusions_gray(gray: np.ndarray, excludes: List[FracRect]) -> np.ndarray:
    """ROI 내부 제외 영역을 중간 회색으로 마스킹한다."""
    if not excludes:
        return gray
    output = gray.copy()
    image_height, image_width = output.shape
    for left, top, width, height in excludes:
        x0, y0 = int(left * image_width), int(top * image_height)
        x1, y1 = int((left + width) * image_width), int((top + height) * image_height)
        output[y0:y1, x0:x1] = 128
    return output


@dataclass
class Config:
    roi: FracRect
    exclude: List[FracRect]
    thumb_width: int
    ssim_threshold: float
    min_region_ratio: float
    pixel_diff_threshold: int
    grid_rows: int
    grid_cols: int
    min_cell_ratio: float
    stability_ssim: float
    debounce_ms: int
    max_stabilize_retries: int


def build_config(args) -> Config:
    grid_rows, grid_cols = args.grid
    return Config(
        roi=args.roi,
        exclude=args.exclude,
        thumb_width=args.thumb_width,
        ssim_threshold=args.ssim_threshold,
        min_region_ratio=args.min_region_ratio,
        pixel_diff_threshold=args.pixel_diff_threshold,
        grid_rows=grid_rows,
        grid_cols=grid_cols,
        min_cell_ratio=args.min_cell_ratio,
        stability_ssim=args.stability_ssim,
        debounce_ms=args.debounce_ms,
        max_stabilize_retries=args.max_stabilize_retries,
    )


def prepare_gray(img: "Image.Image", cfg: Config) -> np.ndarray:
    """원본을 ROI 크롭, 축소, 제외 영역 마스킹 순서로 전처리한다."""
    gray = make_thumbnail_gray(crop_frac(img, cfg.roi), cfg.thumb_width)
    return apply_exclusions_gray(gray, cfg.exclude)


def safe_ssim(gray_a: np.ndarray, gray_b: np.ndarray) -> float:
    if gray_a.shape != gray_b.shape:
        gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))
    return float(ssim(gray_a, gray_b))


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

    score = float(ssim(gray_a, gray_b))

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
    grid_hit = grid_ratio >= cfg.min_cell_ratio and cfg.grid_rows > 0 and cfg.grid_cols > 0
    reason = "global" if global_hit else "grid" if grid_hit else "none"
    return DiffResult(score, bbox_ratio, grid_ratio, grid_cell, global_hit or grid_hit, reason, mask)


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------

def run(args):
    cfg = build_config(args)
    matches = find_windows_by_title(args.title)
    if not matches:
        log(f"제목에 '{args.title}' 이(가) 포함된 보이는 창을 찾지 못했습니다.")
        log("--list-windows 옵션으로 현재 열려 있는 창 목록을 확인해보세요.")
        sys.exit(1)
    if len(matches) > 1:
        log(f"'{args.title}' 로 매칭되는 창이 {len(matches)}개입니다. 첫 번째를 사용합니다:")
        for hwnd, title in matches:
            log(f"  - {title}")
    hwnd, matched_title = matches[0]
    log(f"대상 창: \"{matched_title}\" (mode={args.mode})")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    capture_fn = capture_window_printwindow if args.mode == "window" else capture_window_region

    def grab():
        img = capture_fn(hwnd)
        if img is not None and args.mode == "window" and is_blank(img):
            log("경고: PrintWindow 결과가 빈 화면(검은색)입니다. 이 창은 하드웨어 비디오 오버레이를 "
                "쓰고 있을 수 있습니다. --mode region 사용을 고려하세요.")
        return img

    baseline_img = grab()
    if baseline_img is None:
        log("첫 캡처에 실패했습니다. 창이 최소화되어 있지 않은지 확인해주세요.")
        sys.exit(1)
    baseline_gray = prepare_gray(baseline_img, cfg)

    save_count = 0

    def save(img: "Image.Image", tag: str = "", mask: Optional[np.ndarray] = None):
        nonlocal save_count
        save_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        path = outdir / f"slide_{ts}{suffix}.png"
        img.save(path)
        log(f"저장됨 ({save_count}): {path.name}")
        if mask is not None:
            mask_path = outdir / f"slide_{ts}{suffix}_diffmask.png"
            Image.fromarray(mask).save(mask_path)
            log(f"  diff mask 저장됨: {mask_path.name}")

    save(baseline_img, tag="initial")

    log(
        f"감시 시작 (interval={args.interval}s, roi={cfg.roi}, exclude={cfg.exclude}, "
        f"ssim_threshold={cfg.ssim_threshold}, min_region_ratio={cfg.min_region_ratio}, "
        f"grid={cfg.grid_rows}x{cfg.grid_cols}, min_cell_ratio={cfg.min_cell_ratio}, "
        f"pixel_diff_threshold={cfg.pixel_diff_threshold})"
    )

    consecutive_missing = 0

    try:
        while True:
            time.sleep(args.interval)

            if not win32gui.IsWindow(hwnd):
                consecutive_missing += 1
                log(f"대상 창이 사라진 것 같습니다 ({consecutive_missing}회 연속). 재탐색 시도...")
                refound = find_windows_by_title(args.title)
                if refound:
                    hwnd, matched_title = refound[0]
                    log(f"창을 다시 찾았습니다: \"{matched_title}\"")
                    consecutive_missing = 0
                elif consecutive_missing >= args.max_missing:
                    log("창을 계속 찾을 수 없어 종료합니다.")
                    break
                else:
                    continue

            current_img = grab()
            if current_img is None:
                continue
            current_gray = prepare_gray(current_img, cfg)

            diff = compare_frames(baseline_gray, current_gray, cfg)

            if not diff.is_candidate:
                # 별 변화 없음 (또는 마우스/포인터 수준의 국소 변화) -> 기준 프레임 업데이트하지 않음
                continue

            log(
                f"후보 변화 감지 (reason={diff.reason}, ssim={diff.ssim_value:.3f}, "
                f"bbox_ratio={diff.bbox_ratio:.3f}, grid_ratio={diff.grid_ratio:.3f}"
                + (f", cell={diff.grid_cell}" if diff.grid_cell is not None else "")
                + ") -> 안정화 대기 중..."
            )
            candidate_mask = diff.mask

            # 디바운스 + 안정화 체크
            stabilized = False
            check_img = current_img
            check_gray = current_gray
            for attempt in range(1, args.max_stabilize_retries + 1):
                time.sleep(args.debounce_ms / 1000.0)
                next_img = grab()
                if next_img is None:
                    continue
                next_gray = prepare_gray(next_img, cfg)
                raw_score = safe_ssim(check_gray, next_gray)

                if raw_score >= args.stability_ssim:
                    stabilized = True
                    check_img = next_img
                    check_gray = next_gray
                    break

                log(f"  아직 불안정함 (시도 {attempt}/{args.max_stabilize_retries}, "
                    f"raw_ssim={raw_score:.3f}) - 전환 애니메이션 또는 동영상 재생 중일 수 있음")
                check_img = next_img
                check_gray = next_gray

            if stabilized:
                save(check_img, mask=candidate_mask if args.debug_diff else None)
                baseline_img = check_img
                baseline_gray = check_gray
            else:
                log("  안정화 실패 -> 이번 전환은 캡처하지 않고 건너뜁니다 "
                    "(동영상 재생 등으로 계속 변하는 중일 가능성).")
                baseline_img = check_img
                baseline_gray = check_gray

    except KeyboardInterrupt:
        log("사용자에 의해 중단되었습니다.")

    log(f"종료. 총 {save_count}개 저장됨 -> {outdir.resolve()}")


def run_calibrate(args):
    """저장 없이 연속 프레임의 감지 지표를 출력하고 선택적으로 CSV에 기록한다."""
    cfg = build_config(args)
    matches = find_windows_by_title(args.title)
    if not matches:
        log(f"제목에 '{args.title}' 이(가) 포함된 보이는 창을 찾지 못했습니다.")
        sys.exit(1)
    hwnd, matched_title = matches[0]
    log(f"[calibrate] 대상 창: \"{matched_title}\" (mode={args.mode})")
    capture_fn = capture_window_printwindow if args.mode == "window" else capture_window_region

    csv_file = None
    try:
        if args.calibrate_log:
            csv_file = open(args.calibrate_log, "w", encoding="utf-8", newline="")
            csv_file.write("timestamp,ssim,bbox_ratio,grid_ratio,grid_cell,candidate,reason\n")

        previous_img = capture_fn(hwnd)
        if previous_img is None:
            log("첫 캡처에 실패했습니다. 창이 최소화되어 있지 않은지 확인해주세요.")
            return
        previous_gray = prepare_gray(previous_img, cfg)

        while True:
            time.sleep(args.interval)
            if not win32gui.IsWindow(hwnd):
                log("[calibrate] 대상 창이 사라졌습니다. 종료합니다.")
                break
            current_img = capture_fn(hwnd)
            if current_img is None:
                continue
            current_gray = prepare_gray(current_img, cfg)
            diff = compare_frames(previous_gray, current_gray, cfg)
            timestamp = datetime.now().strftime("%H:%M:%S")
            cell = "" if diff.grid_cell is None else f"{diff.grid_cell[0]}:{diff.grid_cell[1]}"
            line = (
                f"{timestamp},{diff.ssim_value:.4f},{diff.bbox_ratio:.4f},"
                f"{diff.grid_ratio:.4f},{cell},{diff.is_candidate},{diff.reason}"
            )
            print(f"[{timestamp}] ssim={diff.ssim_value:.4f} bbox_ratio={diff.bbox_ratio:.4f} "
                  f"grid_ratio={diff.grid_ratio:.4f} cell={diff.grid_cell} "
                  f"candidate={diff.is_candidate} reason={diff.reason}")
            if csv_file:
                csv_file.write(line + "\n")
                csv_file.flush()
            previous_gray = current_gray
    except KeyboardInterrupt:
        log("[calibrate] 사용자에 의해 중단되었습니다.")
    finally:
        if csv_file:
            csv_file.close()


def build_arg_parser():
    p = argparse.ArgumentParser(description="특정 창을 감시하다가 슬라이드가 바뀔 때만 자동 캡처")
    p.add_argument("--title", type=str, default=None, help="캡처할 창 제목에 포함된 부분 문자열")
    p.add_argument("--list-windows", action="store_true", help="현재 열려 있는 창 제목 목록을 출력하고 종료")
    p.add_argument("--outdir", type=str, default="captures", help="스크린샷 저장 폴더")
    p.add_argument("--mode", choices=["window", "region"], default="window",
                    help="window=PrintWindow로 창 렌더링 캡처(가려져도 어느정도 동작), "
                         "region=화면 좌표 그대로 캡처(가려지면 안됨)")
    p.add_argument("--interval", type=float, default=1.0, help="폴링 주기(초)")
    p.add_argument("--thumb-width", type=int, default=400, help="비교용 썸네일 폭(px)")
    p.add_argument("--roi", type=parse_frac_rect, default=(0.0, 0.0, 1.0, 1.0),
                    metavar="L,T,W,H", help="변화 감지에 사용할 창 내부 비율 영역")
    p.add_argument("--exclude", type=parse_frac_rect, action="append", default=[],
                    metavar="L,T,W,H", help="ROI 안에서 무시할 비율 영역(반복 지정 가능)")
    p.add_argument("--ssim-threshold", type=float, default=0.90,
                    help="이 값보다 SSIM이 낮아지면 '많이 달라졌다'고 판단")
    p.add_argument("--min-region-ratio", type=float, default=0.15,
                    help="변화 영역 bounding box가 프레임의 이 비율 이상이어야 후보로 인정 "
                         "(마우스/레이저 포인터 같은 국소 변화 무시용)")
    p.add_argument("--grid", type=parse_grid, default=(3, 3), metavar="RxC",
                    help="국지 변화 감지 그리드(기본 3x3, 0x0이면 끔)")
    p.add_argument("--min-cell-ratio", type=float, default=0.35,
                    help="그리드 셀에서 후보로 인정할 변경 픽셀 최소 비율")
    p.add_argument("--pixel-diff-threshold", type=int, default=25,
                    help="변경 픽셀로 계산할 절대 명암 차이(0~255)")
    p.add_argument("--debounce-ms", type=int, default=600,
                    help="후보 변화 감지 후 안정화를 확인하기까지 대기 시간(ms)")
    p.add_argument("--stability-ssim", type=float, default=0.985,
                    help="디바운스 후 이 값 이상 유사해야 '안정됐다'고 판단해 최종 캡처")
    p.add_argument("--max-stabilize-retries", type=int, default=5,
                    help="안정화를 확인하기 위해 재시도할 최대 횟수")
    p.add_argument("--max-missing", type=int, default=5,
                    help="창을 잃어버렸을 때 재탐색을 포기하기까지 연속 실패 허용 횟수")
    p.add_argument("--calibrate", action="store_true",
                    help="저장 없이 프레임 간 감지 지표를 계속 출력")
    p.add_argument("--calibrate-log", type=str, default=None, metavar="PATH",
                    help="calibrate 지표를 기록할 CSV 경로")
    p.add_argument("--debug-diff", action="store_true",
                    help="캡처와 함께 감지 당시의 diff mask도 저장")
    return p


def main():
    args = build_arg_parser().parse_args()

    if args.list_windows:
        for title in list_all_windows():
            print(title)
        return

    if not args.title:
        print("--title 을 지정하거나 --list-windows 로 창 목록을 먼저 확인하세요.", file=sys.stderr)
        sys.exit(1)

    if args.calibrate:
        run_calibrate(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
