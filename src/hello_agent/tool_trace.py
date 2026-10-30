"""Compact human-facing tool activity; never changes model/history payloads."""

import json
import unicodedata

from rich.console import Console
from rich.live import Live
from rich.text import Text


def _object(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _line(value: str) -> str:
    value = " ".join(value.split()).encode("utf-8", errors="replace").decode("utf-8")
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in ("Cc", "Cf") else char
        for char in value
    )


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _error(value) -> str:
    if isinstance(value, dict):
        value = value.get("code") or value.get("message")
    if not isinstance(value, str) or not value:
        return "실패"
    return "실패 · " + _line(value.splitlines()[0])


def _target(name: str, arguments: dict) -> str:
    if name == "search_files":
        paths = arguments.get("paths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            return paths[0] + (f" (+{len(paths) - 1}곳)" if len(paths) > 1 else "")
    if isinstance(arguments.get("path"), str):
        return arguments["path"]
    if _count(arguments.get("pid")):
        return f"PID {arguments['pid']}"
    return ""


def _summary(name: str, result: dict) -> str:
    if result.get("status") in ("error", "failed"):
        return _error(result.get("error"))
    if name in ("exec", "exec_input"):
        parts = []
        if name == "exec" and _count(result.get("pid")):
            parts.append(f"PID {result['pid']}")
        if result.get("background") is True:
            parts.append("백그라운드")
        state = result.get("state")
        if state == "waiting_input":
            parts.append("입력 대기")
        elif state == "exited" and _count(result.get("exit_code")):
            parts.append(f"종료 코드 {result['exit_code']}")
        elif state == "failed":
            parts.append(_error(result.get("error")))
        elif state == "cancelled":
            parts.append("중단")
        elif state == "running" and not result.get("background"):
            parts.append("실행 중")
        return " · ".join(parts) or "완료"
    if name == "read_file" and _count(result.get("returned_lines")):
        start, end = result.get("start_line"), result.get("end_line")
        if result["returned_lines"] and _count(start) and _count(end):
            return f"{start}–{end}줄"
        return f"{result['returned_lines']}줄"
    if name == "search_files":
        returned, total = result.get("returned_matches"), result.get("total_matches")
        if _count(returned) and _count(total):
            return (
                f"{returned:,}건" if returned == total else f"{returned:,}/{total:,}건"
            )
    if name == "ls" and isinstance(result.get("entries"), list):
        return f"{len(result['entries']):,}개 항목"
    if name in ("write_file", "edit_file") and _count(result.get("size")):
        size = f"{result['size']:,} bytes"
        if name == "edit_file" and _count(result.get("edits")):
            return f"{result['edits']:,}곳 편집 · {size}"
        return size
    if name == "stat" and result.get("type") in ("file", "directory"):
        if result["type"] == "directory":
            return "폴더"
        if _count(result.get("size")):
            return f"파일 · {result['size']:,} bytes"
    if name in ("mkdir", "rm") and result.get("status") == "success":
        return "생성 완료" if name == "mkdir" else "삭제 완료"
    return "완료"


class ToolTrace:
    def __init__(self, console: Console | None = None):
        self.console = console if console is not None else Console()

    def call(self, name: str, arguments: str):
        return ToolActivity(self.console, name, _object(arguments))


class ToolActivity:
    def __init__(self, console: Console, name: str, arguments: dict):
        self.console = console
        self.name = name
        self.target = _target(name, arguments)
        self.detail = "실행 중…"
        self.live = None

    def _render(self) -> Text:
        label = " ".join(part for part in (self.name, self.target) if part)
        text = Text(_line(f"↳ {label} · {self.detail}"), style="grey62")
        # Leave one cell free so a full-width row doesn't wrap the terminal.
        text.truncate(max(1, self.console.width - 1), overflow="ellipsis")
        return text

    def __enter__(self):
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            self.live = Live(
                self._render(),
                console=self.console,
                auto_refresh=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start(refresh=True)
        return self

    def complete(self, result: str) -> None:
        self.detail = _summary(self.name, _object(result))

    def failed(self, error: Exception) -> None:
        self.detail = _error(getattr(error, "code", None) or str(error))

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.detail = "중단" if issubclass(exc_type, KeyboardInterrupt) else "오류"
        if self.live is not None:
            try:
                self.live.update(self._render())
            finally:
                self.live.stop()
        else:
            self.console.print(self._render())
        return False
