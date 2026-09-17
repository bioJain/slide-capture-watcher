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

## 가장 빠른 실행 방법

터미널을 열고 **이 저장소의 루트 폴더**로 이동한 다음 아래 명령을 실행합니다.
`captures` 부분에는 실제 PNG 파일들이 들어 있는 폴더 경로를 넣으면 됩니다.

### Windows PowerShell

```powershell
cd C:\path\to\slide-capture-watcher
py -m venv .venv-png-to-pdf
.\.venv-png-to-pdf\Scripts\Activate.ps1
py -m pip install -r tools\png_to_pdf\requirements.txt
py tools\png_to_pdf\png_to_pdf.py "C:\path\to\captures"
```

예를 들어 PNG들이 `C:\Users\me\Desktop\slides`에 있다면 마지막 명령은 다음과 같습니다.

```powershell
py tools\png_to_pdf\png_to_pdf.py "C:\Users\me\Desktop\slides"
```

실행이 끝나면 기본적으로 아래 PDF가 만들어집니다.

```text
C:\Users\me\Desktop\slides\slides.pdf
```

PowerShell에서 가상 환경 활성화가 실행 정책 때문에 차단되는 경우에는 활성화 단계를
생략하고 가상 환경의 Python을 직접 실행할 수도 있습니다.

```powershell
.\.venv-png-to-pdf\Scripts\python.exe -m pip install -r tools\png_to_pdf\requirements.txt
.\.venv-png-to-pdf\Scripts\python.exe tools\png_to_pdf\png_to_pdf.py "C:\Users\me\Desktop\slides"
```

### macOS/Linux

```bash
cd /path/to/slide-capture-watcher
python3 -m venv .venv-png-to-pdf
source .venv-png-to-pdf/bin/activate
python -m pip install -r tools/png_to_pdf/requirements.txt
python tools/png_to_pdf/png_to_pdf.py "/path/to/captures"
```

## 출력 파일 위치 지정

출력 경로를 생략하면 입력 폴더 안에 `<입력 폴더명>.pdf`가 생성됩니다. 원하는 이름이나
위치로 저장하려면 `--output`(또는 `-o`)을 사용합니다.

### Windows PowerShell

```powershell
py tools\png_to_pdf\png_to_pdf.py "C:\Users\me\Desktop\slides" --output "C:\Users\me\Desktop\lecture.pdf"
```

### macOS/Linux

```bash
python tools/png_to_pdf/png_to_pdf.py "/path/to/captures" --output "/path/to/lecture.pdf"
```

경로에 공백이 있을 수 있으므로 입력 및 출력 경로를 따옴표로 감싸는 것을 권장합니다.
도움말은 `py tools\png_to_pdf\png_to_pdf.py --help`(Windows) 또는
`python tools/png_to_pdf/png_to_pdf.py --help`(macOS/Linux)로 확인할 수 있습니다.
