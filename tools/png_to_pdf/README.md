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

## 독립 프로그램으로 사용하는 것을 권장하는 이유

이 기능을 캡처 감시기에만 묶어 둘 필요는 없습니다. **이미지 선택 → PDF 생성**은 캡처
방식과 무관한 기능이므로 독립 도구로 두고, 나중에 slide capture watcher에서도 같은 PDF
생성 함수를 호출하는 구조가 더 단순합니다. 그러면 실시간으로 캡처한 이미지뿐 아니라
다운로드하거나 다른 폴더에 보관한 이미지에도 같은 기능을 쓸 수 있습니다.

현재 파일은 두 가지 사용 방식을 함께 제공합니다.

* 빠른 일괄 변환: 폴더의 PNG를 전부 파일명 숫자 순서대로 변환하는 기존 CLI
* 눈으로 골라 변환: 폴더의 PNG/JPG/JPEG/BMP/WebP/TIFF 썸네일을 체크박스로 선택하는 GUI

PDF 생성 로직은 두 방식이 같은 `create_pdf` 함수를 사용하므로 별도 프로그램을 만든다고
핵심 로직이 중복되지는 않습니다.

## 썸네일을 보고 선택해서 PDF로 저장하기

`png_to_pdf.py`가 있는 폴더에서 Windows PowerShell을 열고 다음 명령을 실행합니다.

```powershell
python --version
python -m pip install "Pillow>=10,<13"
python png_to_pdf.py --gui
```

`python`을 인식하지 못한다면 Python 3.10 이상을 설치할 때 **Add python.exe to PATH**를
선택하고 PowerShell을 새로 연 뒤 다시 실행합니다. 이 GUI만 쓸 때는 저장소 전체가 아니라
`png_to_pdf.py` 파일 하나만 따로 받아도 됩니다.

창이 열리면 **폴더 열기**로 원하는 이미지 폴더를 선택하고, PDF에 넣을 이미지만 체크한 뒤
**선택 이미지 PDF 저장**을 누릅니다. 파일은 화면에 표시된 숫자 기준 파일명 순서대로 PDF에
들어가며, 전체 선택과 전체 해제도 지원합니다.

처음부터 특정 폴더를 열어 놓으려면 폴더 경로를 함께 전달합니다.

```powershell
python png_to_pdf.py "C:\Users\me\Desktop\slides" --gui
```

GUI는 Python에 기본 포함된 Tkinter와 기존 의존성인 Pillow만 사용하므로 별도의 GUI
프레임워크를 설치할 필요가 없습니다. CLI와 GUI 모두 저장소의 나머지 캡처 프로그램과
독립적으로 실행할 수 있습니다.

## 가장 빠른 실행 방법

터미널을 열고 **이 저장소의 루트 폴더**로 이동한 다음 아래 명령을 실행합니다.
`captures` 부분에는 실제 PNG 파일들이 들어 있는 폴더 경로를 넣으면 됩니다.

### Windows PowerShell

```powershell
cd C:\path\to\slide-capture-watcher
python -m venv .venv-png-to-pdf
.\.venv-png-to-pdf\Scripts\Activate.ps1
python -m pip install -r tools\png_to_pdf\requirements.txt
python tools\png_to_pdf\png_to_pdf.py "C:\path\to\captures"
```

예를 들어 PNG들이 `C:\Users\me\Desktop\slides`에 있다면 마지막 명령은 다음과 같습니다.

```powershell
python tools\png_to_pdf\png_to_pdf.py "C:\Users\me\Desktop\slides"
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
python tools\png_to_pdf\png_to_pdf.py "C:\Users\me\Desktop\slides" --output "C:\Users\me\Desktop\lecture.pdf"
```

### macOS/Linux

```bash
python tools/png_to_pdf/png_to_pdf.py "/path/to/captures" --output "/path/to/lecture.pdf"
```

경로에 공백이 있을 수 있으므로 입력 및 출력 경로를 따옴표로 감싸는 것을 권장합니다.
도움말은 `python tools\png_to_pdf\png_to_pdf.py --help`(Windows) 또는
`python tools/png_to_pdf/png_to_pdf.py --help`(macOS/Linux)로 확인할 수 있습니다.
