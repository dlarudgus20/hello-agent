"""A human REPL over the same tool registry used by the agent."""

import json
import shlex

from rich.console import Console
from rich.json import JSON
from rich.table import Table
from rich.text import Text

from .errors import EXPECTED_ERRORS, AppError, report_error
from .tools import ToolRegistry, create_registry


def read_command() -> list[str]:
    """Read shell words, allowing newlines inside an unfinished quoted string."""
    pending = None
    while True:
        try:
            line = input("sh> " if pending is None else "...> ")
        except EOFError:
            if pending is not None:
                report_error(AppError("따옴표가 닫히지 않아 입력을 취소했습니다."))
            raise
        pending = line if pending is None else pending + "\n" + line
        try:
            return shlex.split(pending)
        except ValueError as exc:
            if str(exc) == "No closing quotation":
                continue
            raise AppError(f"입력 문법 오류: {exc}") from exc


class ToolShell:
    def __init__(self, registry: ToolRegistry, console: Console):
        self.registry = registry
        self.console = console
        self.specs = {spec["name"]: spec for spec in registry.specifications()}

    @staticmethod
    def _usage(spec: dict) -> str:
        schema = spec["parameters"]
        required = schema.get("required", [])
        arguments = [
            f"<{name}>" if name in required else f"[{name}]"
            for name in schema.get("properties", {})
        ]
        return " ".join([spec["name"], *arguments])

    def help(self, name: str | None = None) -> None:
        if name is None:
            table = Table(title="도구 셸", border_style="cyan")
            table.add_column("사용법", style="cyan")
            table.add_column("설명")
            for spec in self.specs.values():
                table.add_row(Text(self._usage(spec)), Text(spec["description"]))
            table.add_row(Text("help [도구명]"), Text("도구 목록 또는 상세 도움말"))
            table.add_row(Text("exit"), Text("종료 (또는 Ctrl+D)"))
            self.console.print(table)
            return
        if name not in self.specs:
            raise AppError(f"등록되지 않은 도구입니다: {name}")
        spec = self.specs[name]
        self.console.print(Text(self._usage(spec), style="bold cyan"))
        self.console.print(Text(spec["description"]))
        table = Table("인수", "타입", "필수", "설명", border_style="cyan")
        schema = spec["parameters"]
        for key, field in schema.get("properties", {}).items():
            table.add_row(
                Text(key),
                Text(str(field.get("type", "any"))),
                "예" if key in schema.get("required", []) else "아니오",
                Text(field.get("description", "")),
            )
        self.console.print(table)

    def execute(self, words: list[str]) -> bool:
        """Execute one command. False means the user requested exit."""
        if not words:
            return True
        name, *arguments = words
        if name == "exit":
            if arguments:
                raise AppError("사용법: exit")
            return False
        if name == "help":
            if len(arguments) > 1:
                raise AppError("사용법: help [도구명]")
            self.help(arguments[0] if arguments else None)
            return True
        if name not in self.specs:
            raise AppError(
                f"등록되지 않은 도구입니다: {name}. help로 목록을 확인하세요."
            )
        spec = self.specs[name]
        schema = spec["parameters"]
        fields = list(schema.get("properties", {}))
        values = dict(zip(fields, arguments, strict=False))
        if len(arguments) > len(fields) or any(
            key not in values for key in schema.get("required", [])
        ):
            raise AppError(f"사용법: {self._usage(spec)}")
        for key, value in values.items():
            if schema["properties"][key].get("type", "string") != "string":
                try:
                    values[key] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise AppError(
                        f"{key} 인수는 유효한 JSON이어야 합니다: {exc.msg}"
                    ) from exc
        result = json.loads(
            self.registry.execute(name, json.dumps(values, ensure_ascii=False))
        )
        self.display(name, result)
        return True

    def display(self, name: str, result) -> None:
        if name in ("exec", "exec_input"):
            for stream in ("stdout", "stderr"):
                content = result.get(stream, "")
                if content:
                    self.console.print(
                        Text(content, style="red" if stream == "stderr" else None),
                        end="",
                        soft_wrap=True,
                    )
                    if not content.endswith("\n"):
                        self.console.print()
            table = Table("속성", "값", border_style="cyan")
            for key, value in result.items():
                if key not in ("stdout", "stderr", "status"):
                    table.add_row(
                        Text(key), Text(json.dumps(value, ensure_ascii=False))
                    )
            self.console.print(table)
        elif name in ("read_file", "search_files"):
            if name == "read_file":
                for line in result["lines"]:
                    rendered = Text(f"{line['line_number']} | ", style="cyan")
                    rendered.append(line["text"], style="default")
                    self.console.print(rendered, soft_wrap=True)
                excluded = {"status", "lines"}
            else:
                for match in result["matches"]:
                    rendered = Text(
                        f"{match['path']}:{match['line_number']}: ", style="cyan"
                    )
                    rendered.append(match["text"], style="default")
                    self.console.print(rendered, soft_wrap=True)
                excluded = {"status", "matches"}
            table = Table("속성", "값", border_style="cyan")
            for key, value in result.items():
                if key not in excluded:
                    table.add_row(
                        Text(key), Text(json.dumps(value, ensure_ascii=False))
                    )
            self.console.print(table)
        elif name == "ls":
            table = Table(title=Text(result["path"]), border_style="cyan")
            table.add_column("이름")
            table.add_column("종류")
            table.add_column("크기 (bytes)", justify="right")
            for entry in result["entries"]:
                table.add_row(
                    Text(
                        entry["name"],
                        style="bold cyan" if entry["type"] == "directory" else "green",
                    ),
                    Text(entry["type"]),
                    str(entry["size"]),
                )
            self.console.print(table)
        elif name == "stat":
            table = Table("속성", "값", border_style="cyan")
            for key, value in result.items():
                if key != "status":
                    table.add_row(Text(key), Text(str(value)))
            self.console.print(table)
        elif name in ("write_file", "edit_file", "mkdir", "rm"):
            suffix = (
                f" ({result['size']:,} bytes)"
                if name in ("write_file", "edit_file")
                else ""
            )
            self.console.print(Text(f"완료{suffix}", style="green"))
        else:
            self.console.print(JSON(json.dumps(result, ensure_ascii=False, indent=2)))


def run_shell(
    registry: ToolRegistry | None = None, *, console: Console | None = None
) -> None:
    shell = ToolShell(
        registry if registry is not None else create_registry(), console or Console()
    )
    try:
        shell.console.print("도구 셸입니다. help: 도움말, exit: 종료.")
        while True:
            try:
                try:
                    words = read_command()
                except EOFError:
                    shell.console.print()
                    break
                if not shell.execute(words):
                    break
            except KeyboardInterrupt:
                Console(stderr=True).print("\n명령을 중단했습니다.")
            except EXPECTED_ERRORS as exc:
                report_error(exc)
    finally:
        shell.registry.close()
