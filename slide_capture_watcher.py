"""
slide_capture_watcher.py (v2 CLI)
=================================

특정 창(윈도우)을 지속적으로 모니터링하다가, "슬라이드가 완전히 넘어갔다"고 판단되는
순간에만 스크린샷을 자동 저장하는 Windows 전용 CLI.

이 파일은 얇은 wrapper입니다. 창 탐색, 캡처, 변화 감지, 감시 루프는 모두
``capture_core.py`` 에 있고 GUI에서도 같은 모듈을 사용합니다. 여기서는 명령행 인자를
:class:`capture_core.Config` / :class:`capture_core.WatchOptions` 로 변환하고, core가
보내는 이벤트를 콘솔에 출력하는 일만 합니다.

설계 개요 (자세한 내용은 capture_core.py 참고)
---------
1. 저해상도 폴링: --roi로 슬라이드 영역을 먼저 크롭한 뒤 작은 썸네일(기본 폭 400px)로
   축소해 비교합니다.
2. 변화 판단은 두 경로의 OR: 전역 경로(SSIM < ssim_threshold AND 변경 bbox 비율 >=
   min_region_ratio)와 그리드 경로(한 셀의 변경 픽셀 비율 >= min_cell_ratio).
   --exclude로 ROI 안의 화자 PIP 같은 노이즈원을 마스킹할 수 있습니다.
3. 디바운스 + 안정화 체크: 후보 감지 후 debounce_ms 대기하고 다시 비교해, 화면이
   안정됐을 때만 저장합니다. 계속 변하면 max_stabilize_retries 후 건너뜁니다.
4. 창 캡처는 PrintWindow(PW_RENDERFULLCONTENT)를 기본으로 하고, 검은 화면만 나오면
   --mode region(mss 화면 좌표 캡처)으로 전환합니다.

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

    # 4) 저장 없이 지표만 보면서 임계치 튜닝
    python slide_capture_watcher.py --title "Zoom Meeting" --calibrate --calibrate-log noise.csv

주요 옵션은 하단 argparse 정의 참고. 특히 아래 값은 실제 환경에서 튜닝이 필요합니다.
    --ssim-threshold     (기본 0.90)  : 이보다 낮아지면 "많이 달라졌다"고 판단
    --min-region-ratio   (기본 0.15)  : 변화 영역이 프레임의 이 비율 이상 넓어야 인정
    --stability-ssim     (기본 0.985) : 디바운스 후 이 값 이상 유사해야 "안정됐다"고 판단
    --grid               (기본 3x3)   : 셀 단위 국지 변화 판정(0x0이면 끔)
    --min-cell-ratio     (기본 0.35)  : 셀 내부 변경 픽셀의 최소 비율
"""

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# capture_core 를 import하는 순간 프로세스가 DPI-aware로 선언됩니다.
# 다른 win32 호출보다 먼저 import되어야 하므로 이 줄을 위로 유지하세요.
import capture_core as core
from capture_core import (
    CandidateEvent,
    CaptureEvent,
    Config,
    LogEvent,
    MetricsEvent,
    SlideWatcher,
    StoppedEvent,
    WatchOptions,
    WatcherError,
)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def print_event(event: core.Event) -> None:
    """core 이벤트를 콘솔 로그로 변환한다."""
    if isinstance(event, LogEvent):
        log(event.message)
    elif isinstance(event, CaptureEvent):
        log(f"저장됨 ({event.count}): {event.path.name}")
        if event.mask_path is not None:
            log(f"  diff mask 저장됨: {event.mask_path.name}")
    elif isinstance(event, MetricsEvent):
        diff = event.diff
        print(
            f"[{event.timestamp}] ssim={diff.ssim_value:.4f} bbox_ratio={diff.bbox_ratio:.4f} "
            f"grid_ratio={diff.grid_ratio:.4f} cell={diff.grid_cell} "
            f"candidate={diff.is_candidate} reason={diff.reason}"
        )
    elif isinstance(event, StoppedEvent):
        if event.reason == "window_lost":
            log("창을 잃어버려 감시를 종료합니다.")
    elif isinstance(event, CandidateEvent):
        pass  # 상세 내용은 LogEvent로 함께 전달됨


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _arg_frac_rect(value: str):
    try:
        return core.parse_frac_rect(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _arg_grid(value: str):
    try:
        return core.parse_grid(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_config(args) -> Config:
    grid_rows, grid_cols = args.grid
    return Config(
        roi=args.roi,
        exclude=list(args.exclude),
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


def build_watch_options(args) -> WatchOptions:
    return WatchOptions(
        title=args.title,
        outdir=Path(args.outdir),
        mode=args.mode,
        interval=args.interval,
        max_missing=args.max_missing,
        debug_diff=args.debug_diff,
    )


def build_arg_parser():
    p = argparse.ArgumentParser(description="특정 창을 감시하다가 슬라이드가 바뀔 때만 자동 캡처")
    p.add_argument("--title", type=str, default=None, help="캡처할 창 제목에 포함된 부분 문자열")
    p.add_argument("--list-windows", action="store_true", help="현재 열려 있는 창 제목 목록을 출력하고 종료")
    p.add_argument("--outdir", type=str, default="captures", help="스크린샷 저장 폴더")
    p.add_argument("--mode", choices=list(core.CAPTURE_MODES), default="window",
                    help="window=PrintWindow로 창 렌더링 캡처(가려져도 어느정도 동작), "
                         "region=화면 좌표 그대로 캡처(가려지면 안됨)")
    p.add_argument("--interval", type=float, default=1.0, help="폴링 주기(초)")
    p.add_argument("--thumb-width", type=int, default=400, help="비교용 썸네일 폭(px)")
    p.add_argument("--roi", type=_arg_frac_rect, default=core.FULL_FRAME,
                    metavar="L,T,W,H", help="변화 감지에 사용할 창 내부 비율 영역")
    p.add_argument("--exclude", type=_arg_frac_rect, action="append", default=[],
                    metavar="L,T,W,H", help="ROI 안에서 무시할 비율 영역(반복 지정 가능)")
    p.add_argument("--ssim-threshold", type=float, default=0.90,
                    help="이 값보다 SSIM이 낮아지면 '많이 달라졌다'고 판단")
    p.add_argument("--min-region-ratio", type=float, default=0.15,
                    help="변화 영역 bounding box가 프레임의 이 비율 이상이어야 후보로 인정 "
                         "(마우스/레이저 포인터 같은 국소 변화 무시용)")
    p.add_argument("--grid", type=_arg_grid, default=(3, 3), metavar="RxC",
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


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

STOP_JOIN_TIMEOUT = 10.0  # Ctrl+C 후 워커가 저장/CSV 정리를 마칠 때까지 기다리는 최대 시간(초)


def _run_in_thread(target, stop_fn, *args) -> Optional[BaseException]:
    """
    감시 루프를 백그라운드 스레드에서 돌리고 메인 스레드는 Ctrl+C만 기다린다.
    GUI가 쓰게 될 것과 같은 stop-event 패턴을 CLI에서도 그대로 사용한다.

    Ctrl+C가 오면 ``stop_fn()`` 으로 워커에 중지를 알린 뒤 워커가 끝날 때까지 join한다.
    진행 중인 이미지 저장이나 CSV 닫기가 끝나기 전에 프로세스가 종료되지 않게 하기 위함이다.
    """
    error: dict = {}

    def _worker():
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 - 메인 스레드로 전달
            error["exc"] = exc

    thread = threading.Thread(target=_worker, name="slide-watcher", daemon=True)
    thread.start()
    interrupted = False
    try:
        while thread.is_alive():
            thread.join(timeout=0.2)
    except KeyboardInterrupt:
        interrupted = True
        log("사용자에 의해 중단되었습니다. 정리 중...")
        stop_fn()
        try:
            thread.join(timeout=STOP_JOIN_TIMEOUT)
        except KeyboardInterrupt:
            log("한 번 더 중단 요청을 받아 정리를 기다리지 않고 종료합니다.")
        if thread.is_alive():
            log("워커 스레드가 제때 끝나지 않았습니다. 마지막 캡처가 불완전할 수 있습니다.")
    if interrupted:
        return KeyboardInterrupt()
    return error.get("exc")


def run(args) -> int:
    watcher = SlideWatcher(build_watch_options(args), build_config(args), on_event=print_event)
    error = _run_in_thread(watcher.run, watcher.stop)
    if isinstance(error, KeyboardInterrupt):
        pass  # 이미 stop + join 완료
    elif isinstance(error, WatcherError):
        log(str(error))
        if isinstance(error, core.WindowNotFoundError):
            log("--list-windows 옵션으로 현재 열려 있는 창 목록을 확인해보세요.")
        return 1
    elif error is not None:
        raise error
    log(f"종료. 총 {watcher.save_count}개 저장됨 -> {watcher.options.outdir.resolve()}")
    return 0


def run_calibrate(args) -> int:
    watcher = SlideWatcher(build_watch_options(args), build_config(args), on_event=print_event)
    error = _run_in_thread(watcher.run_calibrate, watcher.stop, args.calibrate_log)
    if isinstance(error, KeyboardInterrupt):
        log("[calibrate] 종료했습니다.")
    elif isinstance(error, WatcherError):
        log(str(error))
        return 1
    elif error is not None:
        raise error
    return 0


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.list_windows:
        for title in core.list_all_windows():
            print(title)
        return 0

    if not args.title:
        print("--title 을 지정하거나 --list-windows 로 창 목록을 먼저 확인하세요.", file=sys.stderr)
        return 1

    if args.calibrate:
        return run_calibrate(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
