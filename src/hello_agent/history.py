import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_data_path

from .errors import AppError, file_errors
from .locking import file_lock, locked

DATA_DIR = user_data_path("hello-agent", appauthor=False)
HISTORY_FILE = DATA_DIR / "history.json"


@dataclass
class HistoryState:
    items: list[dict] = field(default_factory=list)
    context_items: list[dict] = field(default_factory=list)
    summary: str = ""
    summarized_items: int = 0


class HistoryStore:
    def transaction(self):
        return file_lock(HISTORY_FILE.with_suffix(".lock"))

    @locked(lambda: HISTORY_FILE.with_suffix(".lock"))
    def load(self) -> HistoryState:
        with file_errors(HISTORY_FILE):
            if not HISTORY_FILE.exists():
                return HistoryState()

            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))

            if not isinstance(data, dict) or data.get("version") != 2:
                raise AppError(f"지원하지 않는 기록 형식입니다: {HISTORY_FILE}")

            for key in ("items", "context_items"):
                if not isinstance(data.get(key), list) or not all(
                    isinstance(item, dict) for item in data[key]
                ):
                    raise AppError(
                        f"기록 파일의 {key} 항목이 잘못되었습니다: {HISTORY_FILE}"
                    )

            if "summarized_items" not in data:
                # Old native contexts cannot be decoded; rebuild from the archive.
                return HistoryState(data["items"], list(data["items"]))
            summary, count = data.get("summary", ""), data["summarized_items"]
            if (
                not isinstance(summary, str)
                or type(count) is not int
                or not 0 <= count <= len(data["items"])
            ):
                raise AppError("요약 상태가 잘못되었습니다.")
            return HistoryState(data["items"], data["context_items"], summary, count)

    @locked(lambda: HISTORY_FILE.with_suffix(".lock"))
    def save(self, state: HistoryState) -> None:
        with file_errors(HISTORY_FILE):
            DATA_DIR.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )

            # Restrict the data directory on Unix as well.
            if os.name == "posix":
                DATA_DIR.chmod(0o700)

            fd, temporary = tempfile.mkstemp(
                prefix=".history-",
                dir=DATA_DIR,
            )

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "version": 2,
                            **asdict(state),
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(temporary, HISTORY_FILE)
            finally:
                Path(temporary).unlink(missing_ok=True)

    @locked(lambda: HISTORY_FILE.with_suffix(".lock"))
    def clear(self) -> None:
        with file_errors(HISTORY_FILE):
            HISTORY_FILE.unlink(missing_ok=True)
