"""Terminal presentation of persisted conversation items."""

import json

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


def _pretty(value) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _entry(item: dict) -> tuple[str, str, str] | None:
    kind = item.get("type")
    if kind == "function_call":
        return (
            f"TOOL CALL · {item.get('name', '?')}",
            "yellow",
            _pretty(item.get("arguments", "")),
        )
    if kind == "function_call_output":
        return (
            "TOOL RESULT",
            "magenta",
            _pretty(item.get("output", "")),
        )
    role = item.get("role")
    if role not in ("user", "assistant", "system", "developer"):
        return None
    content = item.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", part.get("refusal", f"[{part.get('type', 'content')}]"))
            for part in content
        )
    labels = {
        "user": ("YOU", "cyan"),
        "assistant": ("AI", "green"),
        "system": ("SYSTEM", "blue"),
        "developer": ("DEVELOPER", "blue"),
    }
    label, color = labels[role]
    return label, color, content or "(내용 없음)"


def print_history(
    items: list[dict],
    limit: int = 30,
    *,
    console: Console | None = None,
) -> None:
    console = console or Console()
    entries = [entry for item in items if (entry := _entry(item)) is not None]
    console.rule(Text(" 대화 기록 ", style="bold cyan"))
    if not entries:
        console.print("아직 대화 기록이 없습니다.", style="dim")
        return
    start = max(0, len(entries) - limit)
    console.print(
        f"최근 {len(entries) - start}개 / 전체 {len(entries)}개 · 오래된 순",
        style="dim",
        justify="right",
    )
    console.print()
    for index, (label, color, body) in enumerate(entries[start:], start=start + 1):
        console.print(
            Panel(
                Markdown(body) if label == "AI" else Text(body),
                title=Text(f"{index:03d}  {label}", style=f"bold {color}"),
                title_align="left",
                border_style=color,
                padding=(1, 2),
            )
        )
