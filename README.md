# DesktopCal2SOOP

DesktopCal의 일정을 SOOP 방송국 캘린더에 단방향으로 동기화하는 프로그램입니다.

이 프로젝트는 OpenAI Codex로 만들어졌습니다.

## 실행

1. `DesktopCal2SOOP.exe`를 쓰기 가능한 폴더에 둡니다. DesktopCal를 사용하고 있어야 합니다.
2. `DesktopCal2SOOP.exe`를 실행합니다. EXE 옆에 `config.ini`가 없으면 기본 설정으로 생성됩니다.
3. `config.ini`를 열고 `[soop]`의 `username`과 `password`에 SOOP 아이디와 비밀번호를 직접 입력합니다. 트레이 아이콘을 왼쪽 클릭하거나 우클릭 메뉴에서 **동기화**를 선택합니다. 우클릭 **종료**로 앱을 닫습니다.
4. SOOP이 추가 인증을 요구하거나 웹 요청 형식이 바뀌면 앱이 실패 이유를 알립니다.

EXE의 트레이 메뉴에서 **부팅 시 자동 실행**을 체크하면 현재 Windows 계정에 로그인할 때 앱이 실행됩니다. 체크를 해제하면 등록이 삭제됩니다. EXE를 옮겼다면 새 위치에서 실행하고 다시 체크하세요. Windows의 **설정 → 앱 → 시작 프로그램**에서 앱을 껐다면 그곳에서도 다시 켜야 합니다.

비밀번호는 INI에 평문으로 저장하므로 값을 채운 파일은 공유하지 마세요.\
`[sync]`의 `default_time`은 시간을 입력하지 않았을 때 기본값으로 사용할 시간입니다. 기본값은 `12:00`입니다.\
`[sync]`의 `interval_minutes`은 자동 동기화 간격이고 단위는 분입니다. 기본값은 10입니다.\
`[session]`은 앱이 갱신하며 세션은 현재 Windows 계정의 DPAPI로 보호됩니다. 건드리지 마세요.

`sync.db`는 게시한 일정의 연결 정보를 보관합니다.

### 일정 작성 규칙

- 날짜 칸은 줄마다 일정 하나로 읽습니다.
- `!`로 시작하는 줄은 제외합니다.
- `HH:mm`로 시작하는 줄은 해당 시간으로 캘린더에 등록합니다. 시간 표기가 없으면 `default_time` 값을 사용합니다.
- 한국 시간 기준으로 오늘부터 89일 뒤까지, 총 90일 범위의 일정만 새로 등록합니다. 오늘 이전의 SOOP 일정은 그대로 둡니다. 신규 등록이 필요한 교체 일정은 등록 범위에 들어올 때까지 보류합니다.
- 연결된 오늘 이후 일정이 SOOP에서 사라지면 연결을 정리합니다. CalendarTask에 원본이 있고 날짜가 등록 범위 안에 있으면 다시 등록합니다.
- 게시 제목은 SOOP 화면의 15자 한도에 맞춥니다.
- 일정 내용에 `합방`이나 `휴방`이 포함되면 해당 캘린더 타입으로 등록하고, 두 단어가 모두 있으면 휴방을 적용합니다.

## 빌드

uv를 설치한 뒤 프로젝트 폴더에서 실행합니다. DesktopCal의 32비트 DLL을 읽기 위해 32비트 파이썬을 사용합니다.

```powershell
uv python install --no-bin
uv sync --locked
uv run --locked python -m unittest -v
uv run --locked python -m PyInstaller --clean --noconfirm --onefile --windowed --name DesktopCal2SOOP --icon DesktopCal2SOOP.ico --add-data 'DesktopCal2SOOP.ico:.' app.py
```

빌드가 성공하면 `dist\DesktopCal2SOOP.exe` 파일이 생성됩니다.\
기본 `config.ini` 파일 내용은 `config.example.ini`에 있습니다.
