import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from hello_agent import api, auth, cli, history, settings, shell, vfs
from hello_agent.errors import AppError
from hello_agent.tools import Tool, ToolRegistry, create_registry


def feed(monkeypatch, commands):
    commands = iter(commands)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        command = next(commands, EOFError())
        if isinstance(command, BaseException):
            raise command
        return command

    monkeypatch.setattr("builtins.input", read)
    return prompts


def terminal():
    output = StringIO()
    return output, Console(file=output, width=110, color_system=None)


def test_file_tools_and_shared_registry(monkeypatch):
    output, console = terminal()
    feed(
        monkeypatch,
        [
            "mkdir /notes",
            'write_file /notes/memo.txt "안녕하세요"',
            "read_file /notes/memo.txt",
            "ls /notes",
            "stat /notes/memo.txt",
            "exit",
        ],
    )
    shell.run_shell(console=console)
    assert (
        json.loads(
            create_registry().execute("read_file", '{"path":"/notes/memo.txt"}')
        )["lines"][0]["text"]
        == "안녕하세요"
    )
    assert "15 bytes" in output.getvalue()
    assert "file" in output.getvalue()
    assert "안녕하세요" in output.getvalue()
    feed(
        monkeypatch,
        ["read_file /notes/memo.txt", "rm /notes/memo.txt", "rm /notes", "exit"],
    )
    shell.run_shell(console=console)
    assert "안녕하세요" in output.getvalue()
    assert [e.name for e in vfs.create_vfs().listdir("/")] == ["README.txt", "sys"]


def test_quoting_empty_content_and_multiline(monkeypatch):
    prompts = feed(
        monkeypatch,
        [
            'write_file "/space name" "first line',
            "",
            'last line"',
            'write_file /empty ""',
            r"""write_file /literal 'backslash\n and "quotes"' """,
            "exit",
        ],
    )
    shell.run_shell(console=terminal()[1])
    registry = create_registry()
    assert (
        "\n".join(
            line["text"]
            for line in json.loads(
                registry.execute("read_file", '{"path":"/space name"}')
            )["lines"]
        )
        == "first line\n\nlast line"
    )
    assert json.loads(registry.execute("read_file", '{"path":"/empty"}'))["lines"] == []
    assert (
        json.loads(registry.execute("read_file", '{"path":"/literal"}'))["lines"][0][
            "text"
        ]
        == 'backslash\\n and "quotes"'
    )
    assert prompts[:3] == ["sh> ", "...> ", "...> "]


@pytest.mark.parametrize(
    "command, text",
    [
        ("unknown", "등록되지 않은 도구"),
        ("read_file", "사용법: read_file"),
        ("ls / extra", "사용법: ls"),
        ("mkdir relative", "invalid_path"),
        ("read_file /missing", "not_found"),
        ("read_file /missing 0", "invalid_arguments"),
        ("read_file /missing one", "유효한 JSON"),
        ("search_files", "사용법: search_files"),
        ("search_files '[]' '[\"x\"]'", "invalid_arguments"),
        ("write_file /sys/new nope", "read_only"),
        ("help missing", "등록되지 않은 도구"),
        ("help a b", "사용법: help"),
        ("exit extra", "사용법: exit"),
        ("read_file /bad\\", "입력 문법 오류"),
    ],
)
def test_expected_errors_continue(monkeypatch, capsys, command, text):
    output, console = terminal()
    feed(monkeypatch, [command, "help read_file", "exit"])
    shell.run_shell(console=console)
    assert text in capsys.readouterr().err
    assert "Read a UTF-8 virtual file" in output.getvalue()


def test_help_uses_registry_metadata(monkeypatch):
    output, console = terminal()
    registry = ToolRegistry()
    registry.register(
        Tool(
            "greet",
            "Newly registered greeting",
            {
                "type": "object",
                "properties": {
                    "who": {"type": "string", "description": "Greeting target"}
                },
                "required": ["who"],
            },
            lambda who: {"greeting": who},
        )
    )
    feed(monkeypatch, ["help", "help greet", 'greet "한글 이름"', "exit"])
    shell.run_shell(registry, console=console)
    rendered = output.getvalue()
    assert "greet <who>" in rendered
    assert "Newly registered greeting" in rendered
    assert "Greeting target" in rendered
    assert '"greeting"' in rendered
    assert "한글 이름" in rendered


@pytest.mark.parametrize(
    "content", ["[red]literal[/red]", "", "already\n", "a" * 200, "가나\n다라\n"]
)
def test_read_file_does_not_interpret_markup_or_wrap(content):
    output, console = terminal()
    registry = create_registry()
    registry.execute("write_file", json.dumps({"path": "/text", "content": content}))
    assert shell.ToolShell(registry, console).execute(["read_file", "/text"])
    for number, line in enumerate(content.splitlines(), 1):
        assert f"{number} | {line}\n" in output.getvalue()
    assert "sha256" in output.getvalue()


def test_shell_search_and_read_ranges_display_literals_and_pagination():
    output, console = terminal()
    registry = create_registry()
    registry.execute(
        "write_file",
        json.dumps({"path": "/text", "content": "[red]literal[/red]\n" + "x" * 200}),
    )
    tool_shell = shell.ToolShell(registry, console)
    assert tool_shell.execute(["read_file", "/text", "2", "1"])
    assert "2 | " + "x" * 200 + "\n" in output.getvalue()
    assert tool_shell.execute(
        ["search_files", '["/text"]', '["literal","x"]', "1", "0"]
    )
    rendered = output.getvalue()
    assert "/text:1: [red]literal[/red]\n" in rendered
    assert "total_matches" in rendered and "next_offset" in rendered
    assert tool_shell.execute(
        ["search_files", '["/text"]', '["literal","x"]', "1", "1"]
    )
    assert "/text:2: " + "x" * 200 + "\n" in output.getvalue()


def test_eof_in_unfinished_quote_discards_input(monkeypatch, capsys):
    feed(monkeypatch, ['write_file /unfinished "never finished', EOFError()])
    shell.run_shell(console=terminal()[1])
    assert "따옴표가 닫히지 않아" in capsys.readouterr().err
    assert [e.name for e in vfs.create_vfs().listdir("/")] == ["README.txt", "sys"]


def test_ctrl_c_cancels_multiline_and_returns_to_prompt(monkeypatch, capsys):
    prompts = feed(
        monkeypatch,
        ['write_file /cancelled "open', KeyboardInterrupt(), "help read_file", "exit"],
    )
    output, console = terminal()
    shell.run_shell(console=console)
    assert prompts == ["sh> ", "...> ", "sh> ", "sh> "]
    assert "중단" in capsys.readouterr().err
    assert "Read a UTF-8 virtual file" in output.getvalue()
    assert [e.name for e in vfs.create_vfs().listdir("/")] == ["README.txt", "sys"]


def test_ctrl_c_during_tool_execution_continues(monkeypatch, capsys):
    registry = ToolRegistry()

    def interrupt():
        raise KeyboardInterrupt()

    registry.register(Tool("interrupt", "interrupt", {"properties": {}}, interrupt))
    output, console = terminal()
    feed(monkeypatch, ["interrupt", "help interrupt", "exit"])
    shell.run_shell(registry, console=console)
    assert "중단" in capsys.readouterr().err
    assert "interrupt" in output.getvalue()


@pytest.mark.parametrize(
    "error", [TypeError("bug"), KeyError("bug"), RuntimeError("bug"), EOFError("bug")]
)
def test_handler_bugs_propagate(monkeypatch, error):
    registry = ToolRegistry()

    def bug():
        raise error

    registry.register(Tool("bug", "bug", {"properties": {}}, bug))
    feed(monkeypatch, ["bug"])
    with pytest.raises(type(error), match="bug"):
        shell.run_shell(registry, console=terminal()[1])


def test_shell_never_expands_or_executes_host_syntax(monkeypatch, tmp_path):
    marker = tmp_path / "should-not-exist"
    text = f"$(touch {marker}) ; | > $HOME *"
    feed(monkeypatch, [f"write_file /literal '{text}'", "exit"])
    shell.run_shell(console=terminal()[1])
    assert (
        json.loads(create_registry().execute("read_file", '{"path":"/literal"}'))[
            "lines"
        ][0]["text"]
        == text
    )
    assert not marker.exists()


def test_cli_shell_needs_no_auth_model_or_history(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Shell accessed authentication, model, API, or history")

    for target, name in [
        (auth, "login"),
        (auth, "get_access_token"),
        (api, "create_client"),
        (api, "list_models"),
        (settings, "get_model"),
        (settings, "get_reasoning_effort"),
        (history.HistoryStore, "load"),
        (history.HistoryStore, "save"),
        (history.HistoryStore, "clear"),
    ]:
        monkeypatch.setattr(target, name, unexpected)
    monkeypatch.setattr(sys, "argv", ["hello-agent", "sh"])
    feed(monkeypatch, ["read_file relative", 'write_file /shared "hello"', "exit"])
    assert cli.main() == 0  # Per-command failures are not accumulated.
    assert vfs.create_vfs().read_file("/shared") == b"hello"


def test_shell_startup_error_is_nonzero(monkeypatch, capsys):
    def fail():
        raise AppError("storage unavailable")

    monkeypatch.setattr(shell, "create_registry", fail)
    monkeypatch.setattr(sys, "argv", ["hello-agent", "sh"])
    assert cli.main() == 1
    assert "storage unavailable" in capsys.readouterr().err


def test_real_cli_with_piped_input_and_restart(tmp_path):
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    executable = str(Path(sys.executable).parent / "hello-agent")
    first = subprocess.run(
        [executable, "sh"],
        input=(
            'mkdir /notes\nwrite_file /notes/saved "CLI 공유 확인\nDEBUG TIMEOUT"\n'
            "read_file /notes/saved 1 1\n"
            'search_files \'["/notes/","/notes/saved"]\' \'["DEBUG","TIMEOUT"]\' 1 0\n'
            "exit\n"
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert first.stderr == ""
    assert "CLI 공유 확인" in first.stdout
    assert "1 | CLI 공유 확인" in first.stdout
    assert "/notes/saved:2: DEBUG TIMEOUT" in first.stdout
    second = subprocess.run(
        [executable, "sh"],
        input="read_file /notes/saved\nls /notes\nstat /notes/saved\nrm /notes/saved\nrm /notes\n",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert second.stderr == ""
    assert "CLI 공유 확인" in second.stdout
    assert "saved" in second.stdout
    assert not (tmp_path / "hello-agent/history.json").exists()
    assert (tmp_path / "hello-agent/vfs.sqlite3").exists()
