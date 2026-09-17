# Slide Capture Watcher

Windows에서 특정 창을 계속 감시하다가 슬라이드 전환이 끝난 시점에만 전체 창의
스크린샷을 저장하는 도구입니다. 기본 `window` 모드는 Win32 `PrintWindow`를 사용하므로
다른 창이 일부 겹쳐도 캡처할 수 있습니다. GPU 오버레이가 검게 캡처되는 경우에는 실제
화면 좌표를 읽는 `region` 모드를 사용할 수 있습니다.

## 설치

```powershell
python -m pip install -r requirements.txt
```

## 시작하기

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
