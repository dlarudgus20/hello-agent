import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console

from hello_agent import cli
from hello_agent.agent import Agent
from hello_agent.errors import ToolError
from hello_agent.history import HistoryStore
from hello_agent.tool_trace import ToolTrace
from hello_agent.tools import Tool, ToolRegistry
from hello_agent.vfs import VFSError


def terminal(*, width=100, tty=False):
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=tty,
        color_system="truecolor" if tty else None,
        no_color=False,
    )
    return output, ToolTrace(console)


@pytest.mark.parametrize(
    "name, arguments, result, expected",
    [
        ("ls", {"path": "/notes"}, {"entries": [{}, {}]}, "/notes · 2개 항목"),
        (
            "read_file",
            {"path": "/a"},
            {"returned_lines": 20, "start_line": 10, "end_line": 29},
            "/a · 10–29줄",
        ),
        (
            "read_file",
            {"path": "/empty"},
            {"returned_lines": 0, "end_line": None},
            "/empty · 0줄",
        ),
        (
            "search_files",
            {"paths": ["/notes", "/main.lua"], "queries": ["secret query"]},
            {"returned_matches": 3, "total_matches": 100},
            "/notes (+1곳) · 3/100건",
        ),
        (
            "search_files",
            {"paths": ["/"]},
            {"returned_matches": 3, "total_matches": 3},
            "/ · 3건",
        ),
        (
            "write_file",
            {"path": "/a", "content": "secret"},
            {"size": 1234},
            "/a · 1,234 bytes",
        ),
        (
            "edit_file",
            {"path": "/a", "edits": [{"old": "secret old", "new": "secret new"}]},
            {"edits": 2, "size": 30},
            "/a · 2곳 편집 · 30 bytes",
        ),
        ("stat", {"path": "/a"}, {"type": "file", "size": 20}, "/a · 파일 · 20 bytes"),
        ("stat", {"path": "/d"}, {"type": "directory"}, "/d · 폴더"),
        ("mkdir", {"path": "/d"}, {"status": "success"}, "/d · 생성 완료"),
        ("rm", {"path": "/d"}, {"status": "success"}, "/d · 삭제 완료"),
        (
            "exec",
            {"path": "/job.lua", "args": ["secret"]},
            {"pid": 2, "state": "waiting_input"},
            "/job.lua · PID 2 · 입력 대기",
        ),
        (
            "exec",
            {"path": "/job.lua", "background": True},
            {"pid": 2, "state": "running", "background": True},
            "/job.lua · PID 2 · 백그라운드",
        ),
        (
            "exec_input",
            {"pid": 2, "input": "secret"},
            {"pid": 2, "state": "exited", "exit_code": 7},
            "PID 2 · 종료 코드 7",
        ),
        (
            "exec_input",
            {"pid": 2},
            {"state": "failed", "error": {"code": "memory_limit", "message": "secret"}},
            "PID 2 · 실패 · memory_limit",
        ),
        ("exec_input", {"pid": 2}, {"state": "cancelled"}, "PID 2 · 중단"),
        (
            "future",
            {"path": "/a", "password": "secret"},
            {"content": "secret"},
            "/a · 완료",
        ),
        (
            "future",
            {},
            {"status": "error", "error": {"code": "denied", "message": "secret"}},
            "future · 실패 · denied",
        ),
    ],
)
def test_tool_summaries_do_not_dump_payloads(name, arguments, result, expected):
    output, trace = terminal()
    result = {
        **result,
        "stdout": "secret stdout",
        "stderr": "secret stderr",
        "lines": [{"text": "secret file"}],
        "matches": [{"text": "secret match"}],
    }
    with trace.call(name, json.dumps(arguments)) as activity:
        assert output.getvalue() == ""  # no in-progress rows in redirected output
        activity.complete(json.dumps(result))
    assert output.getvalue().startswith(f"↳ {name}")
    assert output.getvalue().endswith(expected + "\n")
    assert len(output.getvalue().splitlines()) == 1
    assert "secret" not in output.getvalue()
    assert "\x1b" not in output.getvalue()


@pytest.mark.parametrize("arguments", ["{", "[]", "null", "42"])
@pytest.mark.parametrize(
    "result",
    [
        "not JSON",
        "[]",
        "null",
        '{"returned_lines":null}',
        '{"returned_lines":true}',
        '{"returned_lines":-1}',
    ],
)
def test_unknown_shapes_and_bad_json_use_short_fallback(arguments, result):
    output, trace = terminal()
    with trace.call("read_file", arguments) as activity:
        activity.complete(result)
    assert output.getvalue() == "↳ read_file · 완료\n"


def test_paths_markup_controls_surrogates_and_wide_characters_are_safe():
    output, trace = terminal(width=35)
    path = "/[red]한글[/red]\n\t\x1b[2J\u202e\ud800" + "긴경로" * 30
    with trace.call("read_file", json.dumps({"path": path})) as activity:
        activity.complete('{"returned_lines":0}')
    line = output.getvalue().rstrip("\n")
    assert len(output.getvalue().splitlines()) == 1
    assert cell_len(line) <= 34 and line.endswith("…")
    assert "[red]" in line and "\x1b" not in line and "\u202e" not in line
    output, trace = terminal(width=150)
    with trace.call("ls", json.dumps({"path": "/x\x1b[2J\u202e"})) as activity:
        activity.complete('{"entries":[]}')
    assert "\\u001b[2J\\u202e" in output.getvalue()


def test_terminal_progress_is_replaced_and_uses_gray(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    output, trace = terminal(tty=True)
    with trace.call("ls", '{"path":"/notes"}') as activity:
        started = output.getvalue()
        assert "실행 중…" in started and "\x1b[?25l" in started
        activity.complete('{"entries":[{}]}')
    rendered = output.getvalue()
    assert "\r\x1b[2K" in rendered  # clear/repaint the same row
    assert "1개 항목" in rendered and "\x1b[?25h" in rendered
    assert "\x1b[38;5;247m" in rendered  # grey62 in the 256-color palette
    assert rendered.count("1개 항목") == 1
    assert rendered.count("\n") == 1


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), TypeError("bug"), RuntimeError("bug")]
)
@pytest.mark.parametrize("tty", [False, True])
def test_interruption_and_bugs_clean_up_and_propagate(monkeypatch, error, tty):
    monkeypatch.setenv("TERM", "xterm-256color")
    output, trace = terminal(tty=tty)
    with pytest.raises(type(error)) as caught, trace.call("ls", '{"path":"/notes"}'):
        raise error
    assert caught.value is error
    assert (
        "중단" if isinstance(error, KeyboardInterrupt) else "오류"
    ) in output.getvalue()
    if tty:
        assert "\x1b[?25h" in output.getvalue()
        with trace.call("ls", '{"path":"/next"}') as activity:
            activity.complete('{"entries":[]}')
        assert output.getvalue().count("\x1b[?25l") == 2


def test_expected_error_uses_code_or_first_line_only():
    output, trace = terminal()
    with trace.call("read_file", '{"path":"/missing"}') as activity:
        activity.failed(VFSError("not_found", "secret file contents"))
    with trace.call("future", "{") as activity:
        activity.failed(ToolError("denied\nsecret detail"))
    assert (
        output.getvalue()
        == "↳ read_file /missing · 실패 · not_found\n↳ future · 실패 · denied\n"
    )


def item(type, **fields):
    value = {"type": type, **fields}
    return SimpleNamespace(**value, model_dump=lambda **_: value.copy())


def response(*items):
    return SimpleNamespace(status="completed", output=list(items))


class ReplayClient:
    def __init__(self, segments):
        self.segments = iter(segments)
        self.requests = []
        self.responses = self

    @contextmanager
    def stream(self, **kwargs):
        self.requests.append(kwargs)
        events, final = next(self.segments)
        yield ReplayStream(events, final)


class ReplayStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __iter__(self):
        return iter(self.events)

    def get_final_response(self):
        return self.final


def make_replay_agent(tmp_path, trace, *, prefix="", failure=None):
    registry = ToolRegistry()
    executed = []

    def read(path):
        executed.append(path)
        if failure is not None:
            raise failure
        return {
            "status": "success",
            "start_line": 1,
            "end_line": 2,
            "returned_lines": 2,
            "lines": [{"text": "RAW SECRET"}],
        }

    registry.register(Tool("read_file", "read", {}, read))
    call1 = item(
        "function_call",
        id="fc1",
        call_id="call1",
        name="read_file",
        arguments='{"path":"/a"}',
    )
    call2 = item(
        "function_call",
        id="fc2",
        call_id="call2",
        name="read_file",
        arguments='{"path":"/b"}',
    )
    before = item(
        "message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text="먼저 확인합니다.")],
    )
    # model_dump must contain JSON data; use a plain content dict in persistence.
    before.model_dump = lambda **_: {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "먼저 확인합니다."}],
    }
    after = item(
        "message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text="확인했습니다.")],
    )
    after.model_dump = lambda **_: {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "확인했습니다."}],
    }
    client = ReplayClient(
        [
            (
                [
                    SimpleNamespace(
                        type="response.output_text.delta", delta="먼저 확인합니다."
                    ),
                    SimpleNamespace(
                        type="response.output_item.done", output_index=0, item=before
                    ),
                    SimpleNamespace(
                        type="response.output_item.done", output_index=1, item=call1
                    ),
                    SimpleNamespace(
                        type="response.output_item.done", output_index=2, item=call2
                    ),
                ],
                response(),
            ),
            ([], response(after)),
        ]
    )
    store = HistoryStore()
    agent = Agent(client, "test-model", registry, store, tool_trace=trace)
    agent.answer_prefix = prefix
    return agent, executed, client


def test_execution_order_recovery_payloads_and_history(tmp_path, capsys):
    trace = ToolTrace(Console(width=100, color_system=None))
    agent, executed, client = make_replay_agent(tmp_path, trace, prefix="AI> ")
    agent.run("check")
    output = capsys.readouterr().out
    assert (
        output
        == "AI> 먼저 확인합니다.\n↳ read_file /a · 1–2줄\n↳ read_file /b · 1–2줄\nAI> 확인했습니다.\n"
    )
    assert executed == ["/a", "/b"]
    calls = [
        entry
        for entry in agent.store.load().items
        if entry.get("type") == "function_call_output"
    ]
    assert [entry["call_id"] for entry in calls] == ["call1", "call2"]
    assert all("RAW SECRET" in entry["output"] for entry in calls)
    assert "RAW SECRET" in json.dumps(client.requests[1]["input"])
    assert "↳" not in json.dumps(agent.store.load().items)


def test_expected_error_and_handler_bug_are_not_changed_by_trace(tmp_path, capsys):
    trace = ToolTrace(Console(width=100, color_system=None))
    agent, _, _ = make_replay_agent(tmp_path, trace, failure=ToolError("denied"))
    agent.run("check")
    output = capsys.readouterr().out
    assert output.count("실패 · denied") == 2 and "확인했습니다." in output
    assert any(
        "Tool execution failed: denied" in entry.get("output", "")
        for entry in agent.history
    )
    agent, _, _ = make_replay_agent(
        tmp_path, trace, failure=TypeError("implementation bug")
    )
    before = agent.store.load()
    with pytest.raises(TypeError, match="implementation bug"):
        agent.run("check")
    assert "read_file /a · 오류" in capsys.readouterr().out
    assert agent.store.load() == before


@pytest.mark.parametrize("display", [True, False])
def test_prefix_is_lazy_for_streamed_fallback_and_hidden_output(
    tmp_path, capsys, display
):
    agent, _, _ = make_replay_agent(tmp_path, None, prefix="AI> ")
    final = response(
        item("message", content=[SimpleNamespace(type="output_text", text="fallback")])
    )
    agent.client = ReplayClient(
        [
            ([], response(item("function_call", name="ls", arguments="{}"))),
            (
                [
                    SimpleNamespace(type="response.output_text.delta", delta=""),
                    SimpleNamespace(type="response.output_text.delta", delta="stream"),
                ],
                final,
            ),
            ([], final),
        ]
    )
    for _ in range(3):
        agent._stream(
            {"model": "test-model", "instructions": "", "input": []}, display=display
        )
    assert capsys.readouterr().out == ("AI> stream\nAI> fallback\n" if display else "")


@pytest.mark.parametrize("command", ["ask", "chat"])
def test_cli_connects_trace_and_only_chat_uses_prefix(
    tmp_path, monkeypatch, capsys, command
):
    agent, _, _ = make_replay_agent(tmp_path, None)
    monkeypatch.setattr(cli.api, "create_client", lambda: agent.client)
    monkeypatch.setattr(cli, "create_registry", lambda: agent.tools)
    monkeypatch.setattr(cli, "resolve_model", lambda: "test-model")
    commands = iter(["check", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    cli.ask("check") if command == "ask" else cli.chat()
    output = capsys.readouterr().out
    assert "↳ read_file /a · 1–2줄\n" in output
    assert ("AI> " in output) == (command == "chat")
    assert "AI> ↳" not in output


CLI_REPLAY = r"""
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace as NS
from hello_agent import cli
from hello_agent.tools import ToolRegistry, Tool

def item(type, **kwargs):
    data={"type":type, **kwargs}
    return NS(**data, model_dump=lambda **_: data)
call=item("function_call", id="fc", call_id="call", name="read_file", arguments='{"path":"/한글"}')
answer=item("message",role="assistant",content=[NS(type="output_text", text="답변입니다.")])
answer.model_dump=lambda **_: {"type":"message","role":"assistant","content":[{"type":"output_text","text":"답변입니다."}]}
responses=iter([NS(status="completed",output=[call]),NS(status="completed",output=[answer])])
@contextmanager
def stream(**kwargs):
    final=next(responses)
    class Stream:
        def __iter__(self): return iter([])
        def get_final_response(self): return final
    yield Stream()
registry=ToolRegistry()
registry.register(Tool("read_file","read",{},lambda path: {"returned_lines":2,"start_line":1,"end_line":2,"content":"DO NOT SHOW"}))
cli.api.create_client=lambda: NS(responses=NS(stream=stream))
cli.create_registry=lambda: registry
cli.resolve_model=lambda: "test-model"
sys.argv=["hello-agent", *sys.argv[1:]]
raise SystemExit(cli.main())
"""


@pytest.mark.parametrize("command", ["ask", "chat"])
def test_real_cli_redirected_stdout(tmp_path, command):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_REPLAY,
            command,
            *(["check"] if command == "ask" else []),
        ],
        input="check\n/exit\n",
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "XDG_DATA_HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path),
        },
    )
    assert result.returncode == 0 and result.stderr == "", result.stderr
    assert result.stdout.count("↳ read_file /한글 · 1–2줄") == 1
    assert "실행 중" not in result.stdout and "\x1b" not in result.stdout
    assert "DO NOT SHOW" not in result.stdout
    assert ("AI> 답변입니다." in result.stdout) == (command == "chat")


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is POSIX-only")
@pytest.mark.parametrize("command", ["ask", "chat"])
def test_real_cli_terminal_repaints_one_row(tmp_path, command):
    import errno
    import pty
    import select

    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                CLI_REPLAY,
                command,
                *(["check"] if command == "ask" else []),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                "XDG_DATA_HOME": str(tmp_path),
                "XDG_CONFIG_HOME": str(tmp_path),
            },
        ) as child:
            os.close(slave)
            slave = None
            chunks = []
            sent = 0
            while True:
                if not select.select([master], [], [], 10)[0]:
                    child.kill()
                    pytest.fail("CLI did not respond")
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if command == "chat":
                    prompts = b"".join(chunks).count(b"You> ")
                    if prompts > sent:
                        os.write(master, b"check\n" if sent == 0 else b"/exit\n")
                        sent += 1
            assert child.wait(timeout=10) == 0
        output = b"".join(chunks).decode()
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
    assert "실행 중…" in output and "\r\x1b[2K" in output
    assert "DO NOT SHOW" not in output
    # Reconstruct the painted rows: CR/erase replaces progress, not an extra row.
    screen = []
    for line in output.split("\n"):
        visible = line.split("\x1b[2K")[-1]
        visible = re.sub(r"\x1b\[[?\d;]*[A-Za-z]", "", visible).replace("\r", "")
        if visible:
            screen.append(visible)
    relevant = [line for line in screen if "↳" in line or "답변입니다." in line]
    assert relevant == [
        "↳ read_file /한글 · 1–2줄",
        ("AI> " if command == "chat" else "") + "답변입니다.",
    ]
    assert "AI> ↳" not in output
