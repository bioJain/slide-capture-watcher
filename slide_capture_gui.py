"""
slide_capture_gui.py
====================

Slide Capture Watcher의 Tkinter + ttk GUI 골격 (JHA-7).

- 감시할 창 선택(드롭다운 + 새로고침), 저장 폴더 지정, Start / Stop / 지금 캡처 버튼
- 감지 설정 패널(ROI, 제외 영역, 그리드, 임계치, 디바운스, 폴링 간격)
- 캘리브레이션 모드(저장 없이 지표만 로그에 출력)
- 상태 로그 영역

스레드 모델
-----------
``capture_core.SlideWatcher`` 는 백그라운드 스레드에서 돌고, core가 보내는 이벤트는
``queue.Queue`` 에 쌓입니다. Tk 위젯은 메인 스레드에서만 만져야 하므로
``root.after(100, ...)`` 폴링으로 큐를 비우면서 화면을 갱신합니다. 이후 갤러리 이슈(JHA-9)도
같은 큐의 ``CaptureEvent`` 를 구독하면 됩니다.

실행::

    python slide_capture_gui.py
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# capture_core 를 먼저 import해야 프로세스가 DPI-aware로 선언됩니다 (다른 win32 호출보다 먼저).
import capture_core as core
from capture_core import (
    CandidateEvent,
    CandidateResolvedEvent,
    CaptureEvent,
    Config,
    LogEvent,
    MetricsEvent,
    SlideWatcher,
    StoppedEvent,
    WatchOptions,
    WatcherError,
)

APP_TITLE = "Slide Capture Watcher"
POLL_MS = 100


# ---------------------------------------------------------------------------
# 폼 값 → core 설정 (Tk와 무관, 단위 테스트 대상)
# ---------------------------------------------------------------------------

@dataclass
class FormValues:
    """GUI 입력 위젯의 문자열 값. Tk 없이도 검증할 수 있도록 순수 데이터로 둔다."""

    window_title: str = ""
    hwnd: Optional[int] = None
    outdir: str = ""
    mode: str = "window"
    interval: str = "1.0"
    roi: str = "0,0,1,1"
    excludes: List[str] = field(default_factory=list)
    grid: str = "3x3"
    min_cell_ratio: str = "0.35"
    ssim_threshold: str = "0.90"
    min_region_ratio: str = "0.15"
    stability_ssim: str = "0.985"
    debounce_ms: str = "600"
    pixel_diff_threshold: str = "25"
    debug_diff: bool = False

    @classmethod
    def from_config(cls, cfg: Config) -> "FormValues":
        """core 기본값으로 폼 초기값을 만든다."""
        return cls(
            roi=",".join(_fmt(v) for v in cfg.roi),
            excludes=[",".join(_fmt(v) for v in rect) for rect in cfg.exclude],
            grid=f"{cfg.grid_rows}x{cfg.grid_cols}",
            min_cell_ratio=_fmt(cfg.min_cell_ratio),
            ssim_threshold=_fmt(cfg.ssim_threshold),
            min_region_ratio=_fmt(cfg.min_region_ratio),
            stability_ssim=_fmt(cfg.stability_ssim),
            debounce_ms=str(cfg.debounce_ms),
            pixel_diff_threshold=str(cfg.pixel_diff_threshold),
        )


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


class FormError(ValueError):
    """어느 입력이 잘못됐는지 필드 이름과 함께 알려준다."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name


def _parse_float(field_name: str, text: str, lo: float, hi: float) -> float:
    try:
        value = float(text.strip())
    except ValueError as exc:
        raise FormError(field_name, f"숫자를 입력하세요 ('{text}')") from exc
    if not (lo <= value <= hi):
        raise FormError(field_name, f"{lo}~{hi} 범위여야 합니다 ('{text}')")
    return value


def _parse_int(field_name: str, text: str, lo: int, hi: int) -> int:
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise FormError(field_name, f"정수를 입력하세요 ('{text}')") from exc
    if not (lo <= value <= hi):
        raise FormError(field_name, f"{lo}~{hi} 범위여야 합니다 ('{text}')")
    return value


def build_settings(values: FormValues, calibrate: bool = False) -> Tuple[WatchOptions, Config]:
    """
    폼 값을 검증해 :class:`WatchOptions` 와 :class:`Config` 로 바꾼다.
    잘못된 값이 있으면 :class:`FormError` 를 올린다(첫 번째 오류만).
    """
    if not values.window_title.strip():
        raise FormError("감시 대상 창", "창을 선택하세요 (목록이 비어 있으면 새로고침)")
    outdir_text = values.outdir.strip()
    if not calibrate and not outdir_text:
        raise FormError("저장 폴더", "캡처를 저장할 폴더를 지정하세요")
    if values.mode not in core.CAPTURE_MODES:
        raise FormError("캡처 모드", f"window 또는 region ('{values.mode}')")

    try:
        roi = core.parse_frac_rect(values.roi)
    except ValueError as exc:
        raise FormError("ROI", str(exc)) from exc

    excludes = []
    for index, text in enumerate(values.excludes, start=1):
        try:
            excludes.append(core.parse_frac_rect(text))
        except ValueError as exc:
            raise FormError(f"제외 영역 #{index}", str(exc)) from exc

    try:
        grid_rows, grid_cols = core.parse_grid(values.grid)
    except ValueError as exc:
        raise FormError("그리드", str(exc)) from exc

    cfg = Config(
        roi=roi,
        exclude=excludes,
        ssim_threshold=_parse_float("SSIM 임계치", values.ssim_threshold, 0.0, 1.0),
        min_region_ratio=_parse_float("최소 bbox 비율", values.min_region_ratio, 0.0, 1.0),
        pixel_diff_threshold=_parse_int("픽셀 차이 임계치", values.pixel_diff_threshold, 0, 255),
        grid_rows=grid_rows,
        grid_cols=grid_cols,
        min_cell_ratio=_parse_float("최소 셀 비율", values.min_cell_ratio, 0.0, 1.0),
        stability_ssim=_parse_float("안정화 SSIM", values.stability_ssim, 0.0, 1.0),
        debounce_ms=_parse_int("디바운스(ms)", values.debounce_ms, 0, 60_000),
    )
    options = WatchOptions(
        title=values.window_title,
        outdir=Path(outdir_text) if outdir_text else Path("captures"),
        mode=values.mode,
        interval=_parse_float("폴링 간격(초)", values.interval, 0.05, 60.0),
        debug_diff=values.debug_diff,
        hwnd=values.hwnd,
    )
    return options, cfg


def make_window_labels(windows: List[Tuple[int, str]]) -> Dict[str, Tuple[int, str]]:
    """
    드롭다운에 표시할 고유 문자열 → (hwnd, title) 매핑을 만든다.
    같은 제목의 창이 여러 개면 두 번째부터 " [2]", " [3]" 을 붙여 구분한다(순서 유지).
    """
    labels: Dict[str, Tuple[int, str]] = {}
    seen: Dict[str, int] = {}
    for hwnd, title in windows:
        count = seen.get(title, 0) + 1
        seen[title] = count
        label = title if count == 1 else f"{title} [{count}]"
        while label in labels:  # 제목 자체가 " [2]"로 끝나는 창과 충돌하는 경우
            count += 1
            label = f"{title} [{count}]"
        labels[label] = (hwnd, title)
    return labels


# ---------------------------------------------------------------------------
# 이벤트 → 로그 문자열 (Tk와 무관)
# ---------------------------------------------------------------------------

def format_event(event: core.Event) -> Optional[str]:
    """큐에서 꺼낸 core 이벤트를 로그 한 줄로 바꾼다. 로그로 남기지 않을 이벤트는 None."""
    if isinstance(event, LogEvent):
        prefix = {"warning": "[경고] ", "error": "[오류] "}.get(event.level, "")
        return prefix + event.message
    if isinstance(event, CaptureEvent):
        text = f"저장됨 ({event.count}): {event.path.name}"
        if event.mask_path is not None:
            text += f"  (diff mask: {event.mask_path.name})"
        return text
    if isinstance(event, MetricsEvent):
        d = event.diff
        return (
            f"[{event.timestamp}] ssim={d.ssim_value:.4f} bbox_ratio={d.bbox_ratio:.4f} "
            f"grid_ratio={d.grid_ratio:.4f} cell={d.grid_cell} candidate={d.is_candidate} reason={d.reason}"
        )
    if isinstance(event, StoppedEvent):
        if event.reason == "window_lost":
            return "창을 잃어버려 감시를 종료했습니다."
        if event.reason == "error":
            return None
        return f"감시 종료. 총 {event.save_count}개 저장됨."
    if isinstance(event, (CandidateEvent, CandidateResolvedEvent)):
        return None
    return str(event)


# ---------------------------------------------------------------------------
# Tk 앱
# ---------------------------------------------------------------------------

class WatcherApp:
    """메인 윈도우. ``root`` 는 ``tkinter.Tk()`` 인스턴스."""

    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.queue: "queue.Queue[core.Event]" = queue.Queue()
        self.watcher: Optional[SlideWatcher] = None
        self.thread: Optional[threading.Thread] = None
        self._windows: List[Tuple[int, str]] = []
        self._window_by_label: Dict[str, Tuple[int, str]] = {}
        self._log_lines = 0

        root.title(APP_TITLE)
        root.minsize(720, 560)

        defaults = FormValues.from_config(Config())
        self.var_window = tk.StringVar()
        self.var_outdir = tk.StringVar(value=str(Path("captures").resolve()))
        self.var_mode = tk.StringVar(value="window")
        self.var_debug_diff = tk.BooleanVar(value=False)
        self.var_calibrate = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="대기 중")
        self.var_count = tk.StringVar(value="저장 0개")
        self.vars: Dict[str, "tk.StringVar"] = {
            "interval": tk.StringVar(value=defaults.interval),
            "roi": tk.StringVar(value=defaults.roi),
            "grid": tk.StringVar(value=defaults.grid),
            "min_cell_ratio": tk.StringVar(value=defaults.min_cell_ratio),
            "ssim_threshold": tk.StringVar(value=defaults.ssim_threshold),
            "min_region_ratio": tk.StringVar(value=defaults.min_region_ratio),
            "stability_ssim": tk.StringVar(value=defaults.stability_ssim),
            "debounce_ms": tk.StringVar(value=defaults.debounce_ms),
            "pixel_diff_threshold": tk.StringVar(value=defaults.pixel_diff_threshold),
            "exclude_entry": tk.StringVar(value=""),
        }

        self._build_widgets()
        self.refresh_windows()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(POLL_MS, self._poll_queue)

    # -- 위젯 구성 -------------------------------------------------------------

    def _build_widgets(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        # 1) 감시 대상 / 저장 폴더
        target = ttk.LabelFrame(root, text="감시 대상", padding=8)
        target.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        target.columnconfigure(1, weight=1)

        ttk.Label(target, text="창").grid(row=0, column=0, sticky="w")
        self.combo_window = ttk.Combobox(target, textvariable=self.var_window, state="readonly")
        self.combo_window.grid(row=0, column=1, sticky="ew", padx=6)
        self.btn_refresh = ttk.Button(target, text="새로고침", command=self.refresh_windows)
        self.btn_refresh.grid(row=0, column=2)

        ttk.Label(target, text="저장 폴더").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.entry_outdir = ttk.Entry(target, textvariable=self.var_outdir)
        self.entry_outdir.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.btn_browse = ttk.Button(target, text="찾아보기...", command=self.choose_outdir)
        self.btn_browse.grid(row=1, column=2, pady=(6, 0))

        mode_row = ttk.Frame(target)
        mode_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(mode_row, text="캡처 모드").pack(side="left")
        self.radio_window = ttk.Radiobutton(mode_row, text="window (PrintWindow)", value="window", variable=self.var_mode)
        self.radio_window.pack(side="left", padx=(6, 0))
        self.radio_region = ttk.Radiobutton(mode_row, text="region (화면 좌표, 가려지면 안 됨)", value="region", variable=self.var_mode)
        self.radio_region.pack(side="left", padx=(6, 0))
        self.check_debug = ttk.Checkbutton(mode_row, text="diff mask 저장", variable=self.var_debug_diff)
        self.check_debug.pack(side="left", padx=(16, 0))

        # 2) 감지 설정
        settings = ttk.LabelFrame(root, text="감지 설정 (값은 창 비율 0~1)", padding=8)
        settings.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        for col in (1, 3, 5):
            settings.columnconfigure(col, weight=1)

        fields = [
            ("폴링 간격(초)", "interval"), ("ROI L,T,W,H", "roi"), ("그리드 RxC (0x0=끔)", "grid"),
            ("SSIM 임계치", "ssim_threshold"), ("최소 bbox 비율", "min_region_ratio"), ("최소 셀 비율", "min_cell_ratio"),
            ("안정화 SSIM", "stability_ssim"), ("디바운스(ms)", "debounce_ms"), ("픽셀 차이 임계치", "pixel_diff_threshold"),
        ]
        self.entries: Dict[str, "ttk.Entry"] = {}
        for index, (label, key) in enumerate(fields):
            r, c = divmod(index, 3)
            ttk.Label(settings, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0, 4), pady=2)
            entry = ttk.Entry(settings, textvariable=self.vars[key], width=14)
            entry.grid(row=r, column=c * 2 + 1, sticky="ew", padx=(0, 12), pady=2)
            self.entries[key] = entry

        exclude = ttk.Frame(settings)
        exclude.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(6, 0))
        exclude.columnconfigure(1, weight=1)
        ttk.Label(exclude, text="제외 영역 (ROI 기준 L,T,W,H)").grid(row=0, column=0, sticky="w")
        self.entry_exclude = ttk.Entry(exclude, textvariable=self.vars["exclude_entry"])
        self.entry_exclude.grid(row=0, column=1, sticky="ew", padx=6)
        self.btn_exclude_add = ttk.Button(exclude, text="추가", command=self.add_exclude)
        self.btn_exclude_add.grid(row=0, column=2)
        self.btn_exclude_del = ttk.Button(exclude, text="선택 삭제", command=self.remove_exclude)
        self.btn_exclude_del.grid(row=0, column=3, padx=(4, 0))
        self.list_exclude = tk.Listbox(exclude, height=3, exportselection=False)
        self.list_exclude.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 0))

        # 3) 제어
        controls = ttk.Frame(root, padding=(10, 4))
        controls.grid(row=2, column=0, sticky="ew")
        self.btn_start = ttk.Button(controls, text="Start", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        self.btn_capture = ttk.Button(controls, text="지금 캡처", command=self.capture_now, state="disabled")
        self.btn_capture.pack(side="left", padx=(6, 0))
        self.check_calibrate = ttk.Checkbutton(controls, text="캘리브레이션 (저장 없이 지표만)", variable=self.var_calibrate)
        self.check_calibrate.pack(side="left", padx=(16, 0))
        ttk.Label(controls, textvariable=self.var_count).pack(side="right")
        ttk.Label(controls, textvariable=self.var_status).pack(side="right", padx=(0, 12))

        # 4) 로그
        log_frame = ttk.LabelFrame(root, text="로그", padding=4)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(4, 10))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.text_log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.text_log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.text_log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text_log.configure(yscrollcommand=scroll.set)
        self.btn_clear_log = ttk.Button(log_frame, text="로그 지우기", command=self.clear_log)
        self.btn_clear_log.grid(row=1, column=0, sticky="e", pady=(4, 0))

    # -- 폼 접근 -----------------------------------------------------------------

    def form_values(self) -> FormValues:
        # 드롭다운 표시 문자열은 창마다 고유하므로(중복 제목은 " [n]" 접미사) 문자열로 hwnd를 찾는다.
        # combobox.current()는 같은 문자열의 첫 항목을 돌려주기 때문에 쓰지 않는다.
        label = self.var_window.get()
        hwnd, title = self._window_by_label.get(label, (None, label))
        return FormValues(
            window_title=title,
            hwnd=hwnd,
            outdir=self.var_outdir.get(),
            mode=self.var_mode.get(),
            interval=self.vars["interval"].get(),
            roi=self.vars["roi"].get(),
            excludes=list(self.list_exclude.get(0, "end")),
            grid=self.vars["grid"].get(),
            min_cell_ratio=self.vars["min_cell_ratio"].get(),
            ssim_threshold=self.vars["ssim_threshold"].get(),
            min_region_ratio=self.vars["min_region_ratio"].get(),
            stability_ssim=self.vars["stability_ssim"].get(),
            debounce_ms=self.vars["debounce_ms"].get(),
            pixel_diff_threshold=self.vars["pixel_diff_threshold"].get(),
            debug_diff=self.var_debug_diff.get(),
        )

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # -- 버튼 동작 ---------------------------------------------------------------

    def refresh_windows(self) -> None:
        try:
            windows = core.enumerate_visible_windows()
        except Exception as exc:  # win32 호출 실패 등
            self.log(f"[오류] 창 목록을 가져오지 못했습니다: {exc}")
            windows = []
        # 이 앱 자신의 창은 목록에서 뺀다.
        self._windows = [(hwnd, title) for hwnd, title in windows if title != APP_TITLE]
        self._window_by_label = make_window_labels(self._windows)
        labels = list(self._window_by_label)
        self.combo_window["values"] = labels
        current = self.var_window.get()
        if current in labels:
            self.combo_window.current(labels.index(current))
        elif labels:
            self.combo_window.current(0)
        else:
            self.var_window.set("")
        self.log(f"창 목록 새로고침: {len(labels)}개")

    def choose_outdir(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="캡처 저장 폴더", initialdir=self.var_outdir.get() or None)
        if chosen:
            self.var_outdir.set(chosen)

    def add_exclude(self) -> None:
        text = self.vars["exclude_entry"].get().strip()
        if not text:
            return
        try:
            core.parse_frac_rect(text)
        except ValueError as exc:
            self._show_error("제외 영역", str(exc))
            return
        self.list_exclude.insert("end", text)
        self.vars["exclude_entry"].set("")

    def remove_exclude(self) -> None:
        for index in reversed(self.list_exclude.curselection()):
            self.list_exclude.delete(index)

    def start(self) -> None:
        if self.is_running:
            return
        calibrate = self.var_calibrate.get()
        try:
            options, cfg = build_settings(self.form_values(), calibrate=calibrate)
        except FormError as exc:
            self._show_error("입력 확인", str(exc))
            return

        self.watcher = SlideWatcher(options, cfg, on_event=self.queue.put)
        watcher = self.watcher

        def _worker() -> None:
            try:
                if calibrate:
                    watcher.run_calibrate()
                else:
                    watcher.run()
            except WatcherError as exc:
                self.queue.put(LogEvent(str(exc), "error"))
                self.queue.put(StoppedEvent("error", watcher.save_count, options.outdir))
            except Exception as exc:  # noqa: BLE001 - 스레드에서 죽지 않고 GUI에 알린다
                self.queue.put(LogEvent(f"예상치 못한 오류: {exc!r}", "error"))
                self.queue.put(StoppedEvent("error", watcher.save_count, options.outdir))

        self.thread = threading.Thread(target=_worker, name="slide-watcher", daemon=True)
        self.thread.start()
        self._set_running(True, calibrate)
        self.log("캘리브레이션 시작" if calibrate else f"감시 시작 -> {options.outdir}")

    def stop(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.var_status.set("중지 중...")

    def capture_now(self) -> None:
        if self.watcher is not None and self.is_running:
            self.watcher.request_capture()

    def on_close(self) -> None:
        if self.is_running and self.watcher is not None:
            self.watcher.stop()
            self.thread.join(timeout=2.0)
        self.root.destroy()

    # -- 큐 폴링 / 로그 -----------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_queue)

    def _handle_event(self, event: core.Event) -> None:
        if isinstance(event, CaptureEvent):
            self.var_count.set(f"저장 {event.count}개")
            self.var_status.set("감시 중")
        elif isinstance(event, CandidateEvent):
            self.var_status.set("안정화 대기...")
        elif isinstance(event, CandidateResolvedEvent):
            # 안정화 실패로 건너뛴 경우에도 감시는 계속되므로 상태를 되돌린다.
            self.var_status.set("감시 중")
        elif isinstance(event, StoppedEvent):
            self._set_running(False, False)
            self.var_status.set("대기 중" if event.reason == "stopped" else "종료됨")
        line = format_event(event)
        if line:
            self.log(line)

    def _set_running(self, running: bool, calibrate: bool) -> None:
        state_run = "disabled" if running else "normal"
        self.btn_start.configure(state=state_run)
        self.btn_refresh.configure(state=state_run)
        self.combo_window.configure(state="disabled" if running else "readonly")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_capture.configure(state="normal" if running and not calibrate else "disabled")
        self.check_calibrate.configure(state=state_run)
        if running:
            self.var_status.set("캘리브레이션 중" if calibrate else "감시 중")
            if not calibrate:
                self.var_count.set("저장 0개")

    def log(self, line: str) -> None:
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        self.text_log.configure(state="normal")
        self.text_log.insert("end", f"[{stamp}] {line}\n")
        self._log_lines += 1
        if self._log_lines > 2000:  # 장시간 세션에서 무한히 커지지 않도록
            self.text_log.delete("1.0", "500.0")
            self._log_lines -= 500
        self.text_log.see("end")
        self.text_log.configure(state="disabled")

    def clear_log(self) -> None:
        self.text_log.configure(state="normal")
        self.text_log.delete("1.0", "end")
        self.text_log.configure(state="disabled")
        self._log_lines = 0

    def _show_error(self, title: str, message: str) -> None:
        from tkinter import messagebox

        self.log(f"[오류] {message}")
        messagebox.showerror(title, message, parent=self.root)


def main() -> int:
    import tkinter as tk

    root = tk.Tk()
    WatcherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
