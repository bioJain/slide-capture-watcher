# PNG → PDF 로컬 도구

기존 Windows 슬라이드 캡처 스크립트와 독립적으로 사용할 수 있는 작은 로컬 도구입니다.
지정한 폴더 바로 아래의 `.png` 파일만 모아 **파일명에 포함된 숫자를 숫자값으로 비교**해
오름차순 정렬한 뒤, PNG 한 장당 PDF 한 페이지로 저장합니다.

예를 들어 아래 파일들은 표시된 순서로 들어갑니다.

```text
slide_20260917_144418.png
slide_20260917_144435.png
slide_20260917_144459.png
slide_20260917_144524.png
```

숫자가 연속될 필요는 없습니다. 대문자 `.PNG`도 인식하며, 투명한 이미지는 흰 배경 위에
합성합니다. 하위 폴더는 검색하지 않습니다.

## 설치

저장소 루트에서 별도 가상 환경을 만들면 기존 watcher 의존성과 분리해 테스트할 수 있습니다.

```bash
python -m venv .venv-png-to-pdf
# Windows
.venv-png-to-pdf\Scripts\activate
# macOS/Linux
source .venv-png-to-pdf/bin/activate

python -m pip install -r tools/png_to_pdf/requirements.txt
```

## 사용법

```bash
python tools/png_to_pdf/png_to_pdf.py "C:\path\to\captures"
```

출력 경로를 생략하면 입력 폴더 안에 `<폴더명>.pdf`가 생성됩니다. 직접 지정하려면:

```bash
python tools/png_to_pdf/png_to_pdf.py "C:\path\to\captures" \
  --output "C:\path\to\slides.pdf"
```

도움말은 `python tools/png_to_pdf/png_to_pdf.py --help`로 확인할 수 있습니다.
