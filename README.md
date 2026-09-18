# Slide Capture Watcher

Windows에서 특정 창을 계속 감시하다가 슬라이드 전환이 끝난 시점에만 전체 창의
스크린샷을 저장하는 도구입니다. 기본 `window` 모드는 Win32 `PrintWindow`를 사용하므로
다른 창이 일부 겹쳐도 캡처할 수 있습니다. GPU 오버레이가 검게 캡처되는 경우에는 실제
화면 좌표를 읽는 `region` 모드를 사용할 수 있습니다.

## 설치

```powershell
python -m pip install -r requirements.txt
```

SSIM 계산은 OpenCV만으로 구현되어 있어 `scikit-image`는 더 이상 필요하지 않습니다.

## 구성

| 파일 | 역할 |
| --- | --- |
| `capture_core.py` | 창 탐색, 캡처, 변화 감지, 감시 루프. GUI와 CLI가 공통으로 import하는 core 모듈 |
| `slide_capture_gui.py` | Tkinter GUI. 창 선택, 저장 폴더, 감지 설정 패널, Start/Stop/지금 캡처, 캘리브레이션, 로그, 썸네일 갤러리 |
| `gallery.py` | 썸네일 갤러리 위젯. 체크박스 다중 선택, 클릭 시 큰 미리보기와 코멘트 입력 |
| `image_files.py` | 캡처 폴더 이미지 검색과 파일명 숫자 정렬 (갤러리와 내보내기가 공용) |
| `slide_capture_watcher.py` | 명령행 wrapper. 인자를 `Config`/`WatchOptions`로 바꾸고 core 이벤트를 콘솔에 출력 |
| `tests/` | core, CLI, GUI 단위 테스트 (`python -m pytest tests`; GUI 위젯 테스트는 디스플레이가 있을 때만 실행) |
| `tools/png_to_pdf/` | 독립 PNG→PDF 변환 도구 |

core는 콘솔 출력이나 `sys.exit`를 하지 않습니다. `SlideWatcher(options, config, on_event)`에
콜백을 넘기면 `LogEvent`, `CaptureEvent`, `CandidateEvent`, `MetricsEvent`, `StoppedEvent`를
받게 되고, `watcher.stop()`과 `watcher.request_capture()`로 다른 스레드에서 감시를 멈추거나
수동 캡처를 요청할 수 있습니다. `capture_core`를 import하는 시점에 프로세스가 DPI-aware로
선언되므로 다른 win32 호출보다 먼저 import해야 합니다.

## 시작하기

GUI로 사용하려면:

```powershell
python slide_capture_gui.py
```

창을 고르고 저장 폴더를 지정한 뒤 Start를 누르면 됩니다. 감지 설정 패널에서 ROI, 제외 영역,
그리드, 임계치를 바꿀 수 있고, "캘리브레이션"을 켜고 Start하면 저장 없이 지표만 로그에
출력됩니다. "지금 캡처"는 변화 여부와 관계없이 현재 프레임을 저장합니다. 오른쪽 갤러리에는
저장 폴더의 기존 이미지와 새로 저장된 캡처가 파일명 순서로 쌓이고, 썸네일을 클릭하면 큰
미리보기와 코멘트 입력창이 열립니다. 체크박스는 나중에 내보낼 이미지를 고르는 용도입니다.

CLI로 사용하려면:

```powershell
# 보이는 창 제목 확인
python slide_capture_watcher.py --list-windows

# 제목 일부가 일치하는 첫 번째 창 감시
python slide_capture_watcher.py --title "Zoom Meeting" --outdir captures

# PrintWindow가 검은 화면을 반환할 때
python slide_capture_watcher.py --title "Zoom Meeting" --mode region
```

저장 파일은 원본 창 전체를 담습니다. 다음의 ROI 및 제외 영역은 **변화 감지 계산에만**
적용됩니다.

## v2 변화 감지

기존 감지는 SSIM과 변경 영역 bounding box를 창 전체에 대해 계산했습니다. 브라우저 UI,
레터박스, 화자 PIP 등이 포함되면 실제 슬라이드 일부가 크게 바뀌어도 변화가 전체 평균에
희석될 수 있습니다. v2는 다음 기능으로 이를 보완합니다.

- `--roi L,T,W,H`: 원본에서 슬라이드 영역을 먼저 크롭한 뒤 비교합니다. 네 값은 창 너비와
  높이에 대한 0~1 비율입니다.
- `--exclude L,T,W,H`: ROI 내부에서 PIP 같은 노이즈원을 마스킹합니다. 여러 번 지정할 수
  있으며 좌표는 크롭된 ROI를 기준으로 합니다.
- `--grid RxC`, `--min-cell-ratio`: 전역 `SSIM AND bbox` 경로와 별도로 셀 단위 변경 비율을
  검사합니다. 어느 한 경로가 참이면 변화 후보가 됩니다. `--grid 0x0`으로 끌 수 있습니다.
- `--pixel-diff-threshold`: 변경 픽셀로 계산할 절대 명암 차이를 조절합니다.
- `--calibrate`, `--calibrate-log`: 이미지를 저장하지 않고 연속 프레임의 SSIM, bbox 비율,
  최대 셀 비율을 출력하거나 CSV로 기록합니다.
- `--debug-diff`: 최종 캡처와 함께 후보 감지 시점의 이진 diff mask를 저장합니다.

후보가 감지되면 `--debounce-ms`만큼 기다린 뒤 ROI와 제외 영역이 동일하게 반영된 프레임을
비교합니다. `--stability-ssim` 이상으로 안정될 때만 저장하고, 동영상이나 전환 효과로 계속
변하면 `--max-stabilize-retries` 이후 건너뜁니다.

## 권장 튜닝 절차

먼저 평상시 화면의 노이즈 플로어를 측정합니다.

```powershell
python slide_capture_watcher.py --title "Zoom Meeting" `
    --calibrate --calibrate-log noise_floor.csv
```

그다음 슬라이드 콘텐츠에 맞춘 ROI와 PIP 제외 영역을 적용합니다.

```powershell
python slide_capture_watcher.py --title "Zoom Meeting" `
    --roi 0.02,0.10,0.96,0.85 `
    --exclude 0.75,0.0,0.25,0.30 `
    --grid 3x3 --min-cell-ratio 0.30 --debug-diff
```

주요 기본값은 다음과 같습니다.

| 옵션 | 기본값 | 의미 |
| --- | ---: | --- |
| `--thumb-width` | `400` | 비교용 ROI 썸네일 폭 |
| `--ssim-threshold` | `0.90` | 전역 경로에서 이 값 미만이면 큰 변화 |
| `--min-region-ratio` | `0.15` | 전역 경로의 최소 변경 bbox 면적 비율 |
| `--grid` | `3x3` | 국지 변화 판정 셀 구성 |
| `--min-cell-ratio` | `0.35` | 한 셀의 최소 변경 픽셀 비율 |
| `--pixel-diff-threshold` | `25` | 변경 픽셀의 최소 절대 명암 차이 |
| `--stability-ssim` | `0.985` | 저장 가능한 안정 상태의 최소 SSIM |

v1과 같은 전역 판정만 사용하려면 `--grid 0x0 --thumb-width 320`을 지정합니다.

## 캡처 모드 주의사항

- `window`: 대상 창이 가려져도 비교적 잘 동작하지만 일부 하드웨어 비디오 오버레이는
  검은 화면으로 나올 수 있습니다.
- `region`: 실제 화면 영역을 캡처하므로 대상 창이 화면에 보이고 다른 창에 가려지지 않아야
  합니다.
- 창 좌표는 캡처마다 다시 조회하므로 실행 중 이동하거나 크기를 바꿔도 ROI 비율은 새 창
  크기에 적용됩니다.
