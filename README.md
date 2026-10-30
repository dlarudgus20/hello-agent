# hello-agent

Python 기반 ChatGPT CLI 및 로컬 에이전트 실험 프로젝트입니다.
Python 3.14 이상과 uv를 사용합니다.

```bash
uv sync
uv run hello-agent login
uv run hello-agent model ls
uv run hello-agent model set <model-id>
uv run hello-agent model set <model-id> --reasoning high
uv run hello-agent ask "안녕"
uv run hello-agent chat
```

`ask`와 `chat`은 같은 영구 대화 기록을 사용합니다. `chat`에서는
`/exit`로 종료하고 `/clear`로 기록을 초기화합니다.
CLI에서는 `uv run hello-agent history clear`로 초기화할 수 있습니다.
`uv run hello-agent history ls`로 최근 기록 30개를 확인하고,
`uv run hello-agent history ls -n 10`으로 표시 개수를 지정합니다.
사용자·AI 메시지와 도구 호출·결과를 각각 한 항목으로 세며,
내부 reasoning 항목은 제외합니다. 선택한 최근 항목은 오래된 순서로 표시합니다.
터미널에서는 역할별 컬러 패널과 AI 답변의 Markdown 서식을 사용합니다.
선택한 모델은 `uv run hello-agent model show`로 확인합니다.
`model set`의 `--reasoning`(`-r`, `--reasoning-effort`) 옵션으로 추론 수준을
함께 저장하면 이후 `ask`와 `chat`의 모든 생성 요청에 적용됩니다.
선택한 모델이 지원하는 `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max` 중에서 지정할 수 있습니다. 미지원 값은 설정을 변경하지 않고 안내합니다.
옵션을 생략해 다시 `model set`하면 기존 추론 설정을 해제하고 모델 기본값을
사용합니다. `model ls`에서는 선택된 모델 옆에 추론 수준을 표시하며,
미지정이면 `추론: 기본값`으로 표시합니다.

전체 원문(`items`), 유지 원문(`context_items`), 누적 요약(`summary`)과
요약한 원문 항목 수(`summarized_items`)를 같은 `history.json`에 저장합니다.
현재 선택 모델의 일반 Responses 요청으로 과거 내용을 요약하며, 별도 compact
API나 서버 네이티브 압축은 사용하지 않습니다. 전체 원문은 유지합니다.

```bash
uv run hello-agent context show
uv run hello-agent context compact
uv run hello-agent context set-threshold 64000
```

REPL에서는 `/context`로 요약 상태·유지 원문 턴 수·추정 입력 토큰·기준을
확인하고 `/compact`로 즉시 요약합니다. 기본 기준은 **128,000 추정 입력 토큰**입니다.
압축 시 최근 완료 대화를 최대 6턴까지 원문으로 유지하며, 요약 공간까지 확보하도록
압축 전에 유지 턴 수를 줄입니다. 수동 압축에서는 요약할 과거 대화가 생기도록
최소 한 턴을 요약 대상으로 선택합니다. 진행 중인 턴은 자르거나 요약하지 않습니다.

일반 대화와 요약 요청 모두 지침·도구 명세·요약·원문·현재 입력을 합산해
기준 이하일 때만 전송합니다. 로컬 tiktoken의 모델 인코딩을 사용하며, 미등록
모델은 `o200k_base`로 계산합니다. 처음 사용하는 인코딩 데이터는 내려받아
캐시합니다. 표시하는 값은 요청 JSON과 프레이밍 여유분을 포함한 로컬 추정치로,
서버의 정확한 토큰 수나 모델의 실제 컨텍스트 한도와는 다릅니다.

요약 요청에는 도구를 제공하지 않고, 기존 요약과 새 과거 기록을 참고 자료로
전달합니다. 암호화된 reasoning 내용은 제외합니다. 긴 기록·도구 결과는 기준에
맞춰 텍스트로 분할해 순차 요약합니다. 요약 목표는 최대 4,096 추정 토큰이며
전체 기준의 1/4 이내입니다. 빈 응답·거절·중단·요약 실패는 저장하지 않습니다.
크기를 맞추지 못하면 최대 3회 재시도 후 오류로 안내하며, 현재 입력·진행 중인
턴만으로도 기준을 넘으면 API에 전송하지 않습니다. 요약은 일부 세부 내용을
잃을 수 있으므로 원문은 `history ls`로 조회할 수 있습니다.

`history clear`와 `/clear`는 원문·활성 컨텍스트·요약을 모두 초기화합니다.
기존 버전 2 기록은 원문을 보존하고 이전 네이티브 컨텍스트를 원문에서 다시
구성합니다. 이후 정상 저장부터 요약 필드를 함께 기록합니다. VFS와 인증정보는
변경하지 않습니다. 실패 시 저장 상태를 유지하며 다중 프로세스 잠금도 적용합니다.

API 권한·연결·요청 한도, 인증, 잘못된 설정·기록 파일, 파일 접근 실패 등
예상 가능한 오류는 traceback 대신 stderr 안내와 종료 코드 1로 보고합니다.
사용자 중단의 종료 코드는 130입니다. REPL에서는 예상 오류를 안내한 뒤
다음 입력을 받습니다. 프로그래밍 오류는 숨기지 않고 traceback을 표시합니다.
도구는 잘못된 JSON·함수 인수·미등록 도구 또는 명시적인 `ToolError`만
모델에 실패 결과로 전달하며, 핸들러 내부의 예상치 못한 예외는 전파합니다.

인증정보는 시스템 keyring에, 설정과 기록은 platformdirs가 정한 사용자
디렉터리에 저장합니다.

### 여러 프로세스에서 실행

같은 사용자와 로컬 데이터 디렉터리에서 여러 `hello-agent`를 실행할 수 있습니다.
공유 대화의 `ask`·`chat` 턴과 수동 압축·기록 초기화는 잠금으로 직렬화합니다.
턴 시작 시 디스크의 최신 기록을 다시 읽으므로, 오래 열린 REPL도 다른 프로세스의
완료된 대화와 초기화를 반영합니다. REPL의 `/context`도 최신 저장 상태를 조회합니다.
도구를 실행한 턴을 자동 재실행하지 않으며, 대화가 실패하더라도 이미 완료한
VFS 변경까지 되돌리지는 않습니다.

설정 갱신은 읽기·수정·저장을 묶어 다른 설정의 변경을 잃지 않게 합니다.
Agent의 모델·추론 수준·압축 기준은 생성 시 일관된 스냅샷으로 선택합니다.
OAuth 로그인과 토큰 갱신은 별도 잠금을 공유하며, 각 API 요청은 현재 토큰을
다시 가져옵니다. 호스트 ID 최초 생성도 잠금과 원자적 저장을 사용합니다.

JSON은 원자적으로 교체하며, 잠금은 별도 `.lock` 파일을 사용합니다.
실행 중에는 잠금 파일을 삭제하지 마세요. 프로세스 종료·강제 종료 시 OS가
잠금을 해제하고, 다른 프로세스는 최대 600초 대기 후 안내 오류를 반환합니다.
VFS는 SQLite 트랜잭션으로 개별 연산과 할당량을 보호하며 경합 시 최대 30초
기다립니다. 같은 파일의 전체 덮어쓰기는 마지막에 완료된 쓰기가 남습니다.
개발 코드에서 기록을 직접 읽고 수정할 때는 `HistoryStore.transaction()` 안에서
`load()`와 `save()`를 함께 실행해야 합니다.

모든 프로세스에 이 동시 실행 처리가 적용되도록, 업데이트 전에 시작한 프로세스는
재시작해야 합니다. 네트워크 공유 파일시스템의 잠금 동작은 별도 검증이 필요합니다.

### 가상 파일시스템

`uv run hello-agent sh`로 에이전트와 같은 도구를 직접 사용하는 셸을 엽니다.
로그인·모델 선택·API 호출 없이 실행하며, 동일한 영구 VFS를 공유합니다.
셸에서 실행한 명령은 대화 기록에 저장하지 않습니다.

```text
sh> help
sh> help write_file
sh> mkdir /notes
sh> write_file /notes/memo.txt "안녕하세요"
sh> read_file /notes/memo.txt
sh> read_file /notes/memo.txt 10 20
sh> search_files '["/notes/memo.txt","/notes/"]' '["DEBUG","TIMEOUT"]' 100 0
sh> ls /notes
sh> stat /notes/memo.txt
sh> rm /notes/memo.txt
sh> exit
```

경로는 절대 경로만 지원합니다. 공백이 있는 인수는 따옴표로 묶고,
여러 줄 내용은 따옴표를 연 뒤 `...> ` 프롬프트에서 계속 입력합니다.
문자열을 그대로 전달하므로 `\n` 같은 이스케이프를 개행으로 변환하지 않습니다.
`help`·`exit`는 셸의 예약 명령입니다. EOF(Ctrl+D)로도 종료하며,
Ctrl+C는 현재 입력 또는 명령을 중단하고 다음 입력을 받습니다.
파이프·리다이렉션·변수 확장·명령 치환·호스트 명령 실행은 제공하지 않습니다.
예상 가능한 오류는 stderr로 알리고 계속 실행하며, 내부 프로그래밍 오류는
전파합니다. 정상 종료는 0, 시작 실패는 1이며 개별 명령 실패는 누적하지 않습니다.

모델은 `ls`, `read_file`, `search_files`, `write_file`, `mkdir`, `rm`, `stat`, `edit_file` 도구로
영구 VFS를 사용합니다. 기본 DB를 처음 생성할 때 `/README.txt`에
`A sole, lonely existence in this unknown world.`를 저장합니다. 이후 수정하거나
삭제한 README는 재시작해도 자동으로 덮어쓰거나 다시 생성하지 않습니다.
기본 `/`에는 읽기 전용 시스템 파일시스템 `/sys`가 마운트됩니다. 파일과 폴더는
사용자 데이터 디렉터리의 **단일 `vfs.sqlite3`** 안에 저장됩니다.
Linux의 기본 경로는 `~/.local/share/hello-agent/vfs.sqlite3`입니다.
각 가상 파일에 대응하는 실제 파일·폴더를 만들지 않습니다.

- 기본 제한: **파일당 1,048,576바이트(1 MiB)**, **파일·폴더 총 512개**.
- 제한은 저장소별이며 루트 디렉터리도 1개로 셉니다. `/sys/fs/`의 JSON 파일로
  사용량과 제한을 조회합니다. 텍스트 크기는 글자 수가 아닌 UTF-8 바이트 수입니다.
- 파일 쓰기는 생성 또는 전체 덮어쓰기입니다. 상위 디렉터리는 먼저
  생성해야 합니다. `rm`은 파일과 빈 디렉터리만 삭제합니다.
- VFS 경로는 절대 경로입니다. `.`·`..`·중복 `/`는 정규화하며, 루트를
  벗어나는 경로와 NUL을 거부합니다. 이름은 255, 경로는 4,096 UTF-8
  바이트까지입니다. 호스트 경로 접근과 심볼릭 링크는 제공하지 않습니다.
- SQLite 트랜잭션 안에서 제한 검사와 변경을 처리하므로 동시 생성도
  항목 수 제한을 초과하지 않습니다. 거부된 쓰기는 기존 내용을 보존합니다.
- `ask`·`chat` 재시작 후에도 파일이 유지됩니다. 히스토리 초기화는 VFS를
  지우지 않습니다. 도구가 완료한 파일 변경은 이후 대화 실패로 롤백되지 않습니다.

`read_file(path, start_line=1, max_lines=200)`은 UTF-8 파일의 지정 범위를
1부터 시작하는 줄 번호와 함께 읽습니다. `lines`의 각 항목은 `line_number`와
`text`를 담으며, 줄 구분 문자만 제거하고 공백·긴 줄은 그대로 보존합니다.
LF·CRLF를 구분자로 취급하며 빈 파일은 0줄, 마지막 개행 뒤에는 추가 줄이 없습니다.
결과에는 정규화된 `path`, `file_size_bytes`, `sha256`, `total_lines`, 요청한
`start_line`, 실제 `end_line`, `returned_lines`, `has_more`, `next_start_line`이
포함됩니다. 시작 줄이 EOF를 넘으면 빈 결과와 `end_line=null`을 반환합니다.
다음 페이지가 없으면 `next_start_line=null`입니다. 시작 줄과 최대 줄 수는 양의 정수입니다.
바이트 크기·해시·줄 번호는 파일을 한 번 읽은 동일 내용에서 계산합니다.

`search_files(paths, queries, max_results=100, offset=0)`는 여러 절대 파일·폴더
경로를 검색합니다. 폴더는 끝 `/`를 허용하고 하위 폴더·마운트까지 재귀 탐색하며,
겹치는 범위는 파일당 한 번만 검색합니다. 파일 경로에 끝 `/`를 붙이면 오류입니다.
`queries`는 비어 있지 않은 단일 줄 문자열 배열이며 중복은 제거합니다.
검색은 **대소문자를 구분하는 리터럴 부분 문자열 OR** 방식으로,
정규식·단어 경계·공백 정규화를 사용하지 않습니다.

- `matches`는 `path`, `line_number`, `text`, `matched_strings`를 담습니다.
  같은 줄은 여러 문자열이 일치해도 한 건이며 경로 사전순·줄 번호순으로 정렬합니다.
- 전체 범위를 검색해 `total_matches`(일치 줄 수)를 계산하되 요청한 페이지의
  본문만 보관합니다. `returned_matches`, `files_scanned`(읽기에 성공한 UTF-8 파일 수),
  `files_matched`, `has_more`, `next_offset`, `skipped_files`를 함께 반환합니다.
  `max_results`는 양의 정수, `offset`은 0 이상의 정수이며 다음 페이지가 없으면
  `next_offset=null`입니다.
- 처음 지정한 경로가 없으면 오류입니다. 탐색 중 사라지거나 종류가 바뀐 경로와
  비 UTF-8 파일은 `skipped_files`에 경로와 오류 코드로 기록합니다.
  저장소 오류와 내부 버그는 전파합니다.

셸은 읽기를 `줄 번호 | 내용`, 검색을 `경로:줄 번호: 내용`으로 표시하고
조회·페이지 메타데이터를 표로 보여줍니다. 파일 내용을 Rich 마크업으로 해석하지 않습니다.
페이지 사이에 파일이 바뀌면 결과도 바뀔 수 있습니다. 읽기는 `sha256`으로
변경을 확인할 수 있고, 검색 페이지는 고정 스냅샷을 보장하지 않습니다.
긴 줄을 자르지 않으므로 줄 수 제한은 토큰 크기를 보장하지 않습니다.
에이전트의 기존 컨텍스트 기준 검사는 계속 적용됩니다.

`edit_file`은 기존 UTF-8 파일의 여러 구간을 한 번에 수정합니다.

```json
{
  "path": "/workspace/main.py",
  "edits": [
    {"old": "DEBUG = False", "new": "DEBUG = True"},
    {"old": "TIMEOUT = 10", "new": "TIMEOUT = 30"}
  ]
}
```

모든 `old`는 수정 전 원본에서 정확히 한 번 등장해야 하고 수정 구간끼리
겹치면 안 됩니다. 검사가 끝나면 뒤쪽부터 치환하며, 하나라도 실패하거나
최종 UTF-8 바이트 크기가 파일 한도를 넘으면 원본을 그대로 유지합니다.
`edits`는 비어 있지 않은 배열이고 `old`는 빈 문자열을 허용하지 않습니다.
`new`는 빈 문자열로 지정해 해당 구간을 삭제할 수 있습니다.
읽기·검사·저장은 같은 SQLite 트랜잭션에서 실행해 동시 편집도 보호합니다.
새 백엔드는 동일한 원자성을 보장하는 `edit_file(path, edits)`를 구현해야 합니다.

도구 셸에서는 배열을 하나의 JSON 인수로 전달합니다.

```text
sh> edit_file /notes/memo.txt '[{"old":"안녕하세요","new":"반갑습니다"}]'
```

`FileSystem` 프로토콜은 저장 백엔드의 인터페이스이고,
`VirtualFileSystem`은 가장 긴 마운트 경로로 요청을 라우팅합니다.
다른 백엔드를 구현하거나 SQLite 저장소를 여러 경로에 연결할 수 있습니다.
이 마운트는 모델에게 보이는 프로그램 내부의 경로 연결입니다.

```python
from pathlib import Path
from hello_agent.vfs import Limits, SQLiteFileSystem, create_vfs
from hello_agent.tools import create_registry

vfs = create_vfs()  # 영구 루트 저장소와 /sys를 기본 마운트합니다.
vfs.mount(
    "/notes",
    SQLiteFileSystem(
        Path("notes.sqlite3"),
        limits=Limits(max_file_bytes=4096, max_entries=32),
    ),
    name="notes",  # 생략 시 마지막 경로 요소를 사용합니다.
)
registry = create_registry(vfs)
```

`mount(path, backend, name=...)`의 이름은 `/sys/fs/<name>.json` 파일명에
사용합니다. `/`의 기본 이름은 `root`, 다른 경로는 마지막 요소이며, 중복이면
명시적인 고유 이름을 지정해야 합니다. 이름은 `/`·NUL·`.`·`..`를 허용하지
않는 단일 요소로, `.json`을 제외하고 최대 250 UTF-8 바이트입니다.
상위 디렉터리는 존재해야 하며, 마운트 지점 자체는 DB에 저장하지 않는 가상
디렉터리로 만들 수 있습니다. 기존 디렉터리에 마운트하는 것도 지원합니다.
`vfs.mounts()`는 경로 순서의 불변 마운트 레코드 목록(tuple)을 반환합니다.

```text
/sys/fs/root.json   # / 저장소
/sys/fs/sys.json    # 생성형 시스템 파일시스템
/sys/fs/notes.json  # 위 예시의 /notes 저장소
```

이 파일들은 `ls`·`read_file`·`stat`으로 조회하며, 읽을 때 현재 상태를 생성합니다.
JSON 필드는 `mount`, `backend`, `read_only`, `entries`, `file_bytes`, `limits`입니다.
`limits`에는 `max_file_bytes`와 `max_entries`가 들어갑니다. 호스트 DB 경로는
노출하지 않습니다. 시스템 파일시스템은 별도 저장 공간이나 용량 제한이 없으므로
`file_bytes`와 `limits`가 `null`이며, 자체 항목 수만 보고합니다.
`/sys`와 그 내용은 SQLite의 512개 한도를 소비하지 않고 변경 요청은
`read_only` 오류로 거부합니다. 별도 `fs_info` 도구는 제공하지 않습니다.
새 마운트를 추가하면 해당 정보 파일도 자동으로 나타납니다.

백엔드는 `FileSystem` 프로토콜의 파일 연산과 `usage()`, `backend_type`,
`read_only`를 제공합니다. 생성형 백엔드는 `Usage.file_bytes`와 `Usage.limits`에
`None`을 사용할 수 있습니다. SQLite의 `ls`·`stat`은 파일 BLOB을 가져오지 않고
메타데이터만 조회하며, 실제 내용은 읽기 요청에서만 조회합니다.

마운트 구성은 코드에서 지정하고, 파일과 제한은 각 DB에 영구 저장합니다.
기존 DB를 제한 인수 없이 열면 저장된 제한을 사용하며, 다른 제한을 지정해
열면 오류로 알립니다. 루트와 마운트 지점, 그 상위 디렉터리는 삭제할 수
없습니다. 기존 하위 마운트를 가리는 상위 마운트도 거부합니다.
예상 가능한 경로·제한·저장소 오류는 `VFSError`(`ToolError`의 하위 타입)로
모델에 전달하고, SQL 오류나 핸들러의 프로그래밍 오류는 숨기지 않습니다.

개발 검사:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

설계 배경은 [MIGRATED.md](MIGRATED.md)를 참고하세요.
