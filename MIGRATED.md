# MIGRATED.md

## 1. 문서 목적 및 주의사항

이 문서는 ChatGPT와의 대화에서 설계하고 구현했던 Python 프로젝트의
배경, 구조, 주요 결정 사항을 Codex에 전달하기 위한 마이그레이션 메모이다.

**중요: 이 문서를 현재 코드베이스의 정확한 명세로 간주하지 말 것.**

- 사용자가 ChatGPT의 예제 코드를 적용한 이후 직접 수정한 부분이 있다.
- 일부 코드는 처음부터 적용하지 않았거나, 다른 방식으로 구현했을 수 있다.
- 이 문서에 기록된 파일 구조, 함수명, 인터페이스, 구현 상태는
  현재 저장소와 다를 수 있다.
- 과거 ChatGPT가 제안한 코드에는 검증되지 않은 가정이나 오류가
  포함되어 있을 가능성도 있다.
- 이 문서는 설계 배경과 의도를 이해하기 위한 참고자료이지,
  구현의 진실 공급원(source of truth)이 아니다.

### Codex가 따라야 할 원칙

1. **현재 저장소의 실제 코드와 설정을 최우선으로 신뢰할 것.**
2. 작업하기 전에 관련 파일을 직접 읽고 현재 구현을 파악할 것.
3. 이 문서와 현재 코드가 충돌하면 현재 코드를 우선할 것.
4. 사용자가 수정한 코드를 과거 예제에 맞춰 임의로 되돌리지 말 것.
5. 문서에 적힌 기능이 실제로 구현되어 있다고 가정하지 말 것.
6. API 동작이나 인증 방식은 필요한 경우 공식 문서 및 현재 SDK 버전으로 검증할 것.
7. 불필요하게 전체 구조를 다시 작성하지 말고 기존 구현을 존중할 것.

---

## 2. 프로젝트 개요

프로젝트는 Python으로 작성한 ChatGPT 기반 CLI 및 간단한
로컬 AI 에이전트 실행 환경이다.

Python 프로젝트 관리에는 `uv`를 사용한다.

주요 목표:

- ChatGPT 계정의 OAuth 인증을 이용한다.
- OpenAI Responses API를 호출한다.
- 모델 목록을 조회하고 기본 모델을 선택할 수 있다.
- 단발성 `ask` 명령과 대화형 `chat` REPL을 제공한다.
- 두 명령은 같은 영구 대화 기록을 사용한다.
- Python 함수를 AI에게 도구로 제공할 수 있는 구조를 마련한다.
- CLI 파싱과 프로그램 로직을 분리한다.
- 가능한 한 기존 라이브러리를 활용하고 불필요한 자체 구현을 피한다.

이 프로젝트는 처음에는 단순한 Python 스크립트로 시작했지만,
현재는 패키지 구조를 갖춘 독립 프로그램을 지향한다.

---

## 3. 개발 환경 및 의존성

프로젝트 관리 도구:

- uv

주요 Python 라이브러리로 사용하거나 도입했던 것:

- `authlib`: OAuth 2.0 / PKCE 지원
- `PyJWT`: ID Token 검증
- `cryptography`: JWT 서명 검증에 필요한 암호화 기능
- `keyring`: 운영체제의 비밀정보 저장소 사용
- `requests`: 일부 HTTP 요청
- `openai`: OpenAI 공식 Python SDK
- `platformdirs`: 크로스플랫폼 설정 및 데이터 경로
- `pytest`: 테스트
- `ruff`: 린팅 및 포맷팅

실제 의존성 목록은 현재 `pyproject.toml` 및 `uv.lock`을
확인해야 한다.

일반적인 개발 명령:

```bash
uv sync
uv run chatgpt-client --help
uv run pytest
uv run ruff check .
uv run ruff format .
```

`ruff format .`은 파일을 수정하므로 필요한 경우에만 실행한다.

---

## 4. 예상 프로젝트 구조

과거 ChatGPT가 제안했던 구조는 다음과 같다.

```text
chatgpt-client/
├── pyproject.toml
├── uv.lock
├── README.md
├── MIGRATED.md
├── src/
│   └── chatgpt_client/
│       ├── __init__.py
│       ├── cli.py
│       ├── app.py
│       ├── auth.py
│       ├── api.py
│       ├── settings.py
│       ├── agent.py
│       ├── tools.py
│       └── history.py
└── tests/
```

이 구조는 참고용이다.

**실제 파일 존재 여부 및 구현 내용은 저장소를 확인할 것.**

각 파일의 원래 의도는 다음과 같다.

| 파일 | 책임 |
|---|---|
| `cli.py` | argparse 기반 명령 파싱 및 디스패치 |
| `app.py` | 사용자 명령에 대응하는 애플리케이션 로직 |
| `auth.py` | ChatGPT OAuth 로그인, 인증정보 저장 및 갱신 |
| `api.py` | OpenAI SDK 클라이언트 생성 및 모델 목록 조회 |
| `settings.py` | 선택한 모델 등 일반 설정 관리 |
| `agent.py` | LLM 호출, 대화 기록 및 도구 호출 루프 |
| `tools.py` | AI에게 제공할 Python 도구 등록 및 실행 |
| `history.py` | 대화 기록의 영구 저장 및 복원 |

책임 분리는 가능한 한 유지하되, 현재 코드가 다른 구조라면
무조건 이 표에 맞춰 리팩터링할 필요는 없다.

---

## 5. CLI 인터페이스

설계했던 CLI 인터페이스:

```bash
chatgpt-client login

chatgpt-client model ls
chatgpt-client model set <model-id>
chatgpt-client model show

chatgpt-client ask "message"
chatgpt-client chat

chatgpt-client history clear
```

### login

ChatGPT 계정으로 OAuth 로그인한다.

### model ls

사용 가능한 모델을 조회한다.

기본적으로 선택된 모델을 별도로 표시하는 방식이 제안되었다.

### model set

사용할 모델을 지정하고 설정 파일에 영구 저장한다.

모델이 실제로 사용 가능한지 API로 검증하는 방식이 제안되었다.

### model show

현재 선택된 모델을 표시한다.

### ask

메시지 하나를 AI에게 전달한다.

중요한 요구사항은 `ask`가 독립된 새 대화를 생성하지 않고,
저장된 기존 대화 기록에 이어서 메시지를 전송한다는 것이다.

### chat

대화형 REPL을 시작한다.

예상 인터페이스:

```text
Chat started.
You> 안녕!
AI> 안녕!

You> /exit
```

제안되었던 REPL 내부 명령:

- `/exit`: REPL 종료
- `/clear`: 현재 대화 기록 초기화

### history clear

영구 저장된 대화 기록을 초기화한다.

CLI의 실제 명령과 옵션은 현재 코드에서 확인해야 한다.

---

## 6. 인증 설계

초기 목표는 ChatGPT 구독 계정의 OAuth 인증정보를 이용해
Responses API에 접근하는 것이었다.

사용자는 OAuth 프로토콜 자체를 직접 구현하는 데 관심이 있는 것이 아니라,
가능한 한 기존 라이브러리로 인증을 처리하기를 원했다.

공식 Node.js DevKit도 검토했으나, 당시에는 프로젝트에 도입하기
번거롭다고 판단하여 Python 기반 구현을 선택했다.

제안된 Python 구현은 다음 라이브러리를 사용했다.

- Authlib
- PyJWT
- requests
- keyring

### 인증 관련 설계 의도

- 브라우저를 통한 사용자 로그인
- OAuth Authorization Code + PKCE
- OpenAI의 동적 클라이언트 등록 흐름 처리
- ID Token 검증
- Access Token과 Refresh Token 관리
- Access Token 만료 시 자동 갱신
- 시스템 비밀 저장소에 인증정보 저장

### keyring

Python의 `keyring` 라이브러리는 GNOME 전용이 아니다.

운영체제별로 지원되는 비밀 저장소 백엔드를 사용한다.

예:

- Windows Credential Manager
- macOS Keychain
- Linux Secret Service (GNOME Keyring, 호환 KWallet 등)

인증정보를 일반 설정 JSON에 평문으로 저장하지 않는 것이
기존 설계의 의도다.

### 주의

당시 제안된 OAuth 구현은 공식 OpenAI 전용 Python 인증 SDK를
사용한 것이 아니라, 범용 OAuth 라이브러리를 조합한 것이다.

따라서 다음을 반드시 실제 코드 및 현재 공식 문서로 검증할 것.

- OAuth 엔드포인트
- Client ID 등록 및 재사용 방식
- OAuth scope와 resource 값
- ID Token 검증
- Refresh Token 갱신 및 회전
- 현재 지원되는 ChatGPT 계정 기반 API 접근 범위

과거 예제의 값이 여전히 유효하다고 가정하지 말 것.

---

## 7. 모델 선택 및 설정

초기에는 `ask`가 모델 목록의 첫 번째 모델을 자동 선택했지만,
이후 명시적인 모델 선택 방식을 선호하게 되었다.

설계 의도:

1. `model ls`로 모델 목록을 확인한다.
2. `model set <model-id>`로 사용할 모델을 선택한다.
3. 선택한 모델을 영구 저장한다.
4. `ask`와 `chat`이 해당 모델을 사용한다.
5. 모델이 선택되지 않았다면 명확한 오류를 출력한다.

일반 설정은 `platformdirs`를 사용해 운영체제별 설정 디렉터리에
저장하도록 제안되었다.

설정 예:

```json
{
  "model": "selected-model-id"
}
```

모델 ID를 코드에 고정하지 않고 실제 API 결과를 사용하는 것이
의도된 방식이다.

---

## 8. 에이전트 실행 환경

사용자는 AI에게 Python 프로그램이 제공하는 간단한 기능을
실행할 수 있는 '가상 에이전트' 환경을 주고 싶어 했다.

현재 단계에서는 구체적인 도구들을 구현하는 것보다,
**추후 도구를 등록할 수 있는 구조를 만드는 것**이 목표였다.

제안된 구성:

### Tool

개별 도구를 나타내는 객체.

주요 정보:

- name
- description
- parameters (JSON Schema)
- Python handler

모델에는 function calling 명세를 제공하고,
실제 Python 코드는 애플리케이션이 실행한다.

### ToolRegistry

도구들을 보관하는 레지스트리.

개념적인 인터페이스:

```python
registry.register(tool)
registry.specifications()
registry.execute(name, arguments)
```

### Agent

에이전트 실행을 담당한다.

의도된 처리 순서:

1. 현재 대화 기록을 준비한다.
2. 사용자 메시지를 추가한다.
3. Responses API를 호출한다.
4. 모델의 응답을 처리한다.
5. 함수 호출이 있으면 등록된 Python 도구를 실행한다.
6. 도구 실행 결과를 모델에 다시 전달한다.
7. 모델이 최종 답변을 반환할 때까지 반복한다.
8. 완료된 대화 기록을 저장한다.

무한한 도구 호출을 막기 위한 반복 횟수 제한도 제안되었다.

### 안전성

AI가 임의의 Python 코드나 셸 명령을 실행하도록 허용하는 구조는
기본 목표가 아니다.

등록된 도구만 실행해야 한다.

파일 수정, 프로세스 실행, 네트워크 접근 등 부작용이 있는 도구를
추가할 경우에는 인수 검증과 권한 제한, 필요 시 사용자 승인을
고려해야 한다.

과거 제안의 기본 레지스트리는 비어 있었다.

실제로 어떤 도구가 구현되었는지는 현재 코드를 확인할 것.

---

## 9. Responses API 사용 방식

`openai` 공식 Python SDK를 사용하는 구조가 제안되었다.

개념적인 사용 방식:

```python
client = OpenAI(api_key=access_token)
```

이때 `access_token`은 OAuth로 얻은 토큰을 의미한다.

에이전트는 Responses API의 스트리밍 출력을 사용하도록 설계되었다.

대표적으로 다루는 이벤트는 다음과 같다.

- `response.output_text.delta`
- `response.completed`
- `response.failed`
- `response.incomplete`

모델이 반환하는 function call은 프로그램에서 처리하고,
결과를 `function_call_output` 형태로 다음 요청에 포함한다.

### 대화 상태

과거 설계에서는 다음 설정을 사용했다.

```python
store = False
stream = True
```

서버 측 대화 상태에 의존하지 않고,
필요한 대화 기록을 클라이언트가 유지하는 방식이었다.

추론 모델의 reasoning 항목과 function call 항목 등,
다음 요청에서 필요한 출력 데이터를 보존하도록 제안되었다.

다만 실제 요청과 응답의 스키마, 지원되는 옵션, 토큰 제한,
reasoning 항목 처리 정책은 현재 공식 문서를 확인할 것.

---

## 10. 영구 대화 기록

가장 최근에 추가한 요구사항은 다음과 같다.

**`ask`와 `chat`이 동일한 영구 대화 기록을 공유해야 한다.**

이는 중요한 설계 결정이다.

### 요구 동작

```bash
chatgpt-client ask "나는 앨리스야."
chatgpt-client ask "내 이름이 뭐였지?"
chatgpt-client chat
```

세 명령 모두 같은 대화 맥락을 사용해야 한다.

프로세스를 종료했다가 다시 시작해도 기록이 유지되어야 한다.

### HistoryStore

제안되었던 `history.py`는 다음 기능을 담당했다.

```python
store.load()
store.save(items)
store.clear()
```

영구 기록에는 사용자 메시지뿐 아니라 모델의 출력 항목,
function call, function_call_output 등 필요한 문맥을 보존한다.

저장 형식으로 JSON을 제안했으며, 버전 필드를 포함했다.

예:

```json
{
  "version": 1,
  "items": []
}
```

`platformdirs.user_data_path()`를 이용해 운영체제별 데이터 디렉터리에
기록을 저장하는 방식이 제안되었다.

Linux의 예시 경로:

```text
~/.local/share/chatgpt-client/history.json
```

이 경로는 고정된 명세가 아니라 예시다.

### 대화 기록의 확정 시점

중요한 설계 의도:

**하나의 사용자 턴이 정상적으로 끝난 뒤에만 영구 기록을 갱신한다.**

이를 위해 기존 기록을 직접 수정하지 않고 임시 작업 목록
(`pending`)을 만드는 방식이 제안되었다.

처리 순서:

1. 이전 대화 기록을 복사한다.
2. 새 사용자 메시지를 추가한다.
3. 모델 호출과 도구 호출을 진행한다.
4. 모델의 최종 응답이 정상 완료되는지 확인한다.
5. 완성된 기록을 파일에 저장한다.
6. 에이전트의 현재 기록을 새 기록으로 교체한다.

오류가 발생하면 미완성 기록을 영구 저장하지 않는 것이 목적이다.

단, 도구가 이미 외부 시스템에 수행한 작업까지 롤백되는 것은 아니다.

### REPL과 ask의 관계

`ask`는 다음과 같은 방식이다.

```python
agent = create_agent()
agent.run(message)
```

`chat`은 하나의 Agent 객체를 반복 사용한다.

```python
agent = create_agent()

while True:
    message = input("You> ")
    agent.run(message)
```

두 명령 모두 같은 HistoryStore를 사용한다.

### 기록 초기화

- CLI: `history clear`
- REPL: `/clear`

두 방식 모두 영구 기록을 초기화하도록 설계되었다.

---

## 11. 동시성 및 데이터 무결성

과거 예제는 기본적으로 단일 프로세스를 전제로 했다.

예를 들어:

1. 터미널 A에서 `chat`을 실행한다.
2. 터미널 B에서 `ask`를 실행한다.
3. 각 프로세스가 서로 다른 대화 기록을 메모리에 보유한다.
4. 이후 저장하는 쪽이 다른 프로세스의 변경을 덮어쓸 수 있다.

따라서 다중 프로세스 지원이 필요해진다면 다음 사항을 검토해야 한다.

- 파일 잠금
- 최신 기록 재확인
- 대화 기록의 버전 또는 수정 시각 비교
- 원자적 저장
- Refresh Token 갱신 시 동시성 제어

당시에는 다중 프로세스 동시 실행까지 구현하는 것이 목표가 아니었다.

---

## 12. 향후 확장 가능성

이전 대화에서 논의된 확장 방향:

- `history ls`
- `history new`
- `history switch`
- 여러 대화 세션 관리
- REPL 명령어 확장
- 새로운 Python 도구 등록
- 도구별 실행 권한 및 승인 정책
- 대화 기록이 길어질 때 컨텍스트 관리
- 토큰 사용량 추적
- 장시간 실행 시 OAuth 토큰 자동 갱신 안정화

이 항목들은 과거에 생각했던 확장 후보이며,
현재 작업해야 할 확정 TODO 목록은 아니다.

사용자의 새로운 지시가 우선한다.

---

## 13. 현재 구현 검증 체크리스트

Codex는 관련 작업을 시작할 때 다음을 확인하면 좋다.

- [ ] 현재 `pyproject.toml` 및 의존성 확인
- [ ] 현재 CLI 명령어와 인자 확인
- [ ] 인증정보 저장 및 갱신 방식 확인
- [ ] 모델 목록 조회 응답 형식 확인
- [ ] 모델 설정 저장 및 복원 확인
- [ ] `ask`와 `chat`의 대화 기록 공유 여부 확인
- [ ] 대화 기록 저장 형식 확인
- [ ] Responses API 호출 및 스트리밍 처리 확인
- [ ] function calling의 구현 상태 확인
- [ ] 현재 등록된 도구 확인
- [ ] 테스트 실행 및 실패 항목 확인

이 체크리스트는 현황 파악을 위한 것이지,
모든 항목이 이미 구현되었다는 의미가 아니다.

---

## 14. 마지막 지침

이 문서는 이전 ChatGPT 대화에서 나온 요구사항과 설계 선택을
Codex가 이해하도록 정리한 것이다.

**이 문서는 과거의 설계 의도를 설명할 뿐, 현재 코드의 정확성을
보증하지 않는다.**

사용자는 이전에 제공된 코드 일부를 직접 수정했으며,
그 변경은 이 문서에 반영되어 있지 않을 수 있다.

따라서 Codex는:

- 이 문서를 읽은 뒤 실제 코드를 반드시 조사하고,
- 현재 구현을 기준으로 작업하며,
- 과거 예제와 다르다는 이유만으로 코드를 수정하거나 되돌리지 말고,
- 새로운 사용자 요구사항을 최우선으로 처리해야 한다.

현재 저장소가 source of truth이다.
