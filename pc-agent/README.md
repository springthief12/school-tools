# PC 에이전트 — 컴시간 알리미 · 충북소통메신저 → teacher-tool 실시간 연동

```
[컴시간 알리미]  ┐                         ┌→ 시간표 변경 자동 반영 (주황색, 되돌리기 가능)
                 ├→ school_agent.py → AI 요약 ─→ teacher-tool.html ─┤
[충북소통메신저] ┘   (이 PC 안에서만)          └→ 중요 업무 + 마감기한 자동 등록
```

## 1. 설치 (처음 한 번)

1. [Python 3.10 이상](https://www.python.org/downloads/windows/)을 설치합니다. 설치 화면에서 **"Add python.exe to PATH"** 를 체크하세요.
2. 이 폴더(`pc-agent`)에서 명령 프롬프트를 열고 다음을 실행합니다.
   ```
   pip install -r requirements.txt
   ```
3. **ChatGPT Plus 계정으로 AI 요약 연결** (API 키·추가 결제 없음)
   1. [Node.js LTS](https://nodejs.org/ko)를 설치합니다.
   2. Codex CLI 를 설치합니다.
      ```
      npm install -g @openai/codex
      ```
   3. 로그인합니다. 브라우저가 열리면 **Sign in with ChatGPT** 를 고르고 Plus 계정으로 로그인하세요.
      ```
      codex login
      ```
   4. 연결을 시험합니다. 예시 메시지가 GPT-6.1 Sol 로 요약되어 나오면 성공입니다.
      ```
      python school_agent.py --test-ai
      ```

   Codex 를 찾지 못하거나 로그인이 만료되면 AI 대신 **규칙 기반 추출**(‘까지’, ‘제출’, ‘회신’ 같은 낱말과 날짜를 찾는 방식)로 자동 전환됩니다.

## 2. 감지 대상 확인

컴시간 알리미와 메신저를 켠 상태에서 실행합니다.

```
python school_agent.py --list-windows        ← ★ 표시가 붙으면 감지 성공
python school_agent.py --dump-text 메신저     ← 창 안의 글자를 실제로 읽을 수 있는지 확인
```

★ 이 안 붙으면 `config.json` 의 `title_keywords` 나 `process_names`(exe 이름)를 출력된 값으로 고쳐 주세요.
`--dump-text` 결과가 비어 있으면 그 프로그램은 화면 글자를 외부에 공개하지 않는 방식이라, 창 제목·알림 팝업 제목만 감지됩니다.

## 3. 실행

`start_agent.bat` 을 더블클릭합니다. 처음 실행하면 `config.example.json` 이 `config.json` 으로 복사됩니다.
`config.json` 의 `teacher.description` 에 "3학년 2반 담임, 국어 담당 홍길동" 처럼 적어 두면, 컴시간 화면에서 **내 시간표 변경만** 골라냅니다.

teacher-tool 은 아래 둘 중 편한 곳에서 여세요.

| 여는 주소 | 비고 |
|---|---|
| GitHub Pages 주소 (예: `https://springthief12.github.io/school-tools/teacher-tool.html`) | 기존 데이터 그대로 사용. 크롬에서 "로컬 네트워크 접근" 허용 창이 뜨면 **허용** |
| `http://127.0.0.1:8765/teacher-tool.html` | 에이전트가 직접 제공. 주소가 달라서 기존 브라우저 데이터는 따로 보관됨 |

Windows 로그인 시 자동 실행하려면 `Win + R` → `shell:startup` 폴더에 `start_agent.bat` 바로가기를 넣으세요.

## 4. 성능 — 확인 주기

| 작업 | 주기 | 비용 |
|---|---|---|
| 맨 앞 창 확인 | 2초 | 거의 0 (Win32 함수 1~3회) |
| 보이는 창 목록 (새 알림 팝업 감지) | 10초 | 수 ms |
| 프로세스 목록 (트레이 프로그램) | 30초 | 수~수십 ms |
| 창 글자 읽기 — 그 창을 보고 있을 때 | 메신저 5초 · 컴시간 10초 | 창 크기에 따라 수십~수백 ms |
| 창 글자 읽기 — 뒤에 있을 때 | 메신저 60초 · 컴시간 5분 | 위와 같음 |
| 새 창(알림 팝업)이 나타나면 | 즉시 1회 | — |
| AI 요약 | **변경 시에만**, 30초 조용해진 뒤 · 같은 앱 최소 2분 간격 · 하루 120회 한도 | codex 1회 (보통 수 초~수십 초, 백그라운드) |
| 브라우저 전송 | 변경 시에만 (SSE), 20초마다 연결 유지 신호 | 거의 0 |

- 5분 동안 키보드·마우스 입력이 없으면 모든 주기가 **4배**로 늘어납니다.
- `active_hours`(기본 평일 07:30~18:30) 밖에서는 창 글자 읽기를 멈춥니다.
- 숫자는 모두 `config.json` 에서 바꿀 수 있습니다.

## 5. 개인정보 · 보안

- 에이전트는 `127.0.0.1`(이 PC)에서만 접속을 받고, `allowed_origins` 에 적힌 웹 주소만 데이터를 읽을 수 있습니다.
- AI를 켜면 **새로 바뀐 글자만** ChatGPT 계정을 통해 OpenAI로 전송됩니다(메신저는 새 줄만, 컴시간은 변경 전/후 화면).
- Codex 는 빈 폴더(`data/codex_work`)에서 읽기 전용(`--sandbox read-only`)으로 실행되고, `--ephemeral` 로 대화 기록을 남기지 않습니다.
  학교 정보보안 지침상 외부 전송이 어렵다면 `"llm": {"enabled": false}` 로 끄고 규칙 기반 추출만 사용하세요.
- 화면 기록은 `data/state.json` 에만 저장되며 git 에 올라가지 않습니다(`.gitignore`).

## 6. ChatGPT Plus 와의 관계

ChatGPT 데스크톱 앱은 사용자가 요청할 때 허락된 앱 내용을 읽는 방식이라, 백그라운드에서 계속 감시하며 웹페이지를 고치는 용도로는 쓸 수 없습니다.
그래서 감지는 이 에이전트가 맡고, 요약은 **Plus 요금제에 포함된 Codex** 로 GPT-6.1 Sol 을 호출합니다(`codex exec`).

- Plus 의 Codex 사용 한도를 함께 씁니다. 한도에 닿으면 그동안은 규칙 기반 추출로 대신합니다.
  다른 Codex 작업을 많이 하신다면 `daily_limit` 를 낮추거나 `min_interval_sec` 를 늘리세요.
- `reasoning_effort` 는 기본 `low` 입니다. 요약 품질이 아쉬우면 `medium` 으로 올릴 수 있지만 사용량과 시간이 늘어납니다.
- API 키 방식을 쓰고 싶으시면 `"provider": "openai"` 로 바꾸고 환경변수 `OPENAI_API_KEY` 를 등록하면 됩니다.

## 테스트 (Windows 가 아닌 곳)

```
python school_agent.py --mock mock.json
```
`mock.json` 형식은 `school_agent.py` 의 `MockSource` 설명을 참고하세요.
