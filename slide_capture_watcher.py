"""
slide_capture_watcher.py
=========================

특정 창(윈도우)을 지속적으로 모니터링하다가, "슬라이드가 완전히 넘어갔다"고 판단되는
순간에만 스크린샷을 자동 저장하는 Windows 전용 스크립트.

설계 개요
---------
1. 저해상도 폴링: 매 프레임을 원본 해상도로 비교하지 않고, 작은 썸네일(기본 폭 320px)로
   축소한 뒤 비교합니다. 이렇게 하면 diff 연산 비용이 수백 배 줄어들어 1초 간격 폴링도
   CPU 부담이 거의 없습니다.

2. 변화 판단 지표를 두 가지 조합으로 사용합니다.
   - SSIM(구조적 유사도): 마우스 커서/레이저 포인터처럼 국소적인 변화에는 거의 반응하지
     않고, 슬라이드가 바뀌면 값이 크게 떨어집니다.
   - 변화 영역의 bounding box 비율: SSIM이 조금 떨어지더라도 변화가 화면 전체가 아니라
     작은 영역(포인터, 커서)에 국한되어 있으면 무시합니다.
   두 조건(SSIM 하락 AND 변화 영역이 충분히 넓음)이 동시에 만족될 때만 "후보 변화"로
   인정합니다.

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

주요 옵션은 하단 argparse 정의 참고. 특히 아래 세 개는 실제 환경에서 튜닝이 필요합니다.
    --ssim-threshold     (기본 0.90)  : 이보다 낮아지면 "많이 달라졌다"고 판단
    --min-region-ratio   (기본 0.15)  : 변화 영역이 프레임의 이 비율 이상 넓어야 인정
    --stability-ssim     (기본 0.985) : 디바운스 후 이 값 이상 유사해야 "안정됐다"고 판단

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
# 변화 감지
# ---------------------------------------------------------------------------

@dataclass
class DiffResult:
    ssim_value: float
    bbox_ratio: float
    is_candidate: bool


def make_thumbnail_gray(img: "Image.Image", width: int = 320) -> np.ndarray:
    w, h = img.size
    if w == 0 or h == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    new_h = max(1, int(h * (width / w)))
    small = img.resize((width, new_h), Image.BILINEAR)
    return cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2GRAY)


def compare_frames(gray_a: np.ndarray, gray_b: np.ndarray, ssim_threshold: float, min_region_ratio: float) -> DiffResult:
    if gray_a.shape != gray_b.shape:
        # 창 크기가 바뀐 경우: 리사이즈해서 비교(대략적 처리)
        gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))

    score = ssim(gray_a, gray_b)

    diff = cv2.absdiff(gray_a, gray_b)
    _, mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    coords = cv2.findNonZero(mask)
    if coords is None:
        bbox_ratio = 0.0
    else:
        x, y, w, h = cv2.boundingRect(coords)
        frame_area = gray_a.shape[0] * gray_a.shape[1]
        bbox_ratio = (w * h) / frame_area if frame_area else 0.0

    is_candidate = (score < ssim_threshold) and (bbox_ratio >= min_region_ratio)
    return DiffResult(ssim_value=score, bbox_ratio=bbox_ratio, is_candidate=is_candidate)


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------

def run(args):
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
    baseline_gray = make_thumbnail_gray(baseline_img, args.thumb_width)

    save_count = 0

    def save(img: "Image.Image", tag: str = ""):
        nonlocal save_count
        save_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        path = outdir / f"slide_{ts}{suffix}.png"
        img.save(path)
        log(f"저장됨 ({save_count}): {path.name}")

    save(baseline_img, tag="initial")

    log(f"감시 시작 (interval={args.interval}s, ssim_threshold={args.ssim_threshold}, "
        f"min_region_ratio={args.min_region_ratio})")

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
            current_gray = make_thumbnail_gray(current_img, args.thumb_width)

            diff = compare_frames(baseline_gray, current_gray, args.ssim_threshold, args.min_region_ratio)

            if not diff.is_candidate:
                # 별 변화 없음 (또는 마우스/포인터 수준의 국소 변화) -> 기준 프레임 업데이트하지 않음
                continue

            log(f"후보 변화 감지 (ssim={diff.ssim_value:.3f}, bbox_ratio={diff.bbox_ratio:.3f}) "
                f"-> 안정화 대기 중...")

            # 디바운스 + 안정화 체크
            stabilized = False
            check_img = current_img
            check_gray = current_gray
            for attempt in range(1, args.max_stabilize_retries + 1):
                time.sleep(args.debounce_ms / 1000.0)
                next_img = grab()
                if next_img is None:
                    continue
                next_gray = make_thumbnail_gray(next_img, args.thumb_width)
                stability = compare_frames(check_gray, next_gray, args.ssim_threshold, args.min_region_ratio)
                # 안정화 판단은 "얼마나 유사해졌는가"만 보므로 raw ssim 값을 직접 재계산
                raw_score = ssim(check_gray, next_gray) if check_gray.shape == next_gray.shape else 0.0

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
                save(check_img)
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


def build_arg_parser():
    p = argparse.ArgumentParser(description="특정 창을 감시하다가 슬라이드가 바뀔 때만 자동 캡처")
    p.add_argument("--title", type=str, default=None, help="캡처할 창 제목에 포함된 부분 문자열")
    p.add_argument("--list-windows", action="store_true", help="현재 열려 있는 창 제목 목록을 출력하고 종료")
    p.add_argument("--outdir", type=str, default="captures", help="스크린샷 저장 폴더")
    p.add_argument("--mode", choices=["window", "region"], default="window",
                    help="window=PrintWindow로 창 렌더링 캡처(가려져도 어느정도 동작), "
                         "region=화면 좌표 그대로 캡처(가려지면 안됨)")
    p.add_argument("--interval", type=float, default=1.0, help="폴링 주기(초)")
    p.add_argument("--thumb-width", type=int, default=320, help="비교용 썸네일 폭(px)")
    p.add_argument("--ssim-threshold", type=float, default=0.90,
                    help="이 값보다 SSIM이 낮아지면 '많이 달라졌다'고 판단")
    p.add_argument("--min-region-ratio", type=float, default=0.15,
                    help="변화 영역 bounding box가 프레임의 이 비율 이상이어야 후보로 인정 "
                         "(마우스/레이저 포인터 같은 국소 변화 무시용)")
    p.add_argument("--debounce-ms", type=int, default=600,
                    help="후보 변화 감지 후 안정화를 확인하기까지 대기 시간(ms)")
    p.add_argument("--stability-ssim", type=float, default=0.985,
                    help="디바운스 후 이 값 이상 유사해야 '안정됐다'고 판단해 최종 캡처")
    p.add_argument("--max-stabilize-retries", type=int, default=5,
                    help="안정화를 확인하기 위해 재시도할 최대 횟수")
    p.add_argument("--max-missing", type=int, default=5,
                    help="창을 잃어버렸을 때 재탐색을 포기하기까지 연속 실패 허용 횟수")
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

    run(args)


if __name__ == "__main__":
    main()
