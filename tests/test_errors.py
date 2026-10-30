import errno
import json
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from hello_agent import auth, cli, history, settings
from hello_agent.agent import Agent
from hello_agent.errors import AppError, ToolError
from hello_agent.history import HistoryState
from hello_agent.tools import Tool, ToolRegistry, create_registry


def status_error(code=401):
    return APIStatusError(
        "denied",
        response=httpx.Response(
            code,
            request=httpx.Request(
                "POST", "https://api.openai.com/v1/responses/compact"
            ),
        ),
        body={
            "message": "This ChatPass credential is not authorized for the requested operation."
        },
    )


def raise_error(error):
    def fail(*args, **kwargs):
        raise error

    return fail


@pytest.mark.parametrize(
    "error, text",
    [
        (status_error(), "HTTP 401"),
        (status_error(429), "HTTP 429"),
        (status_error(503), "HTTP 503"),
        (
            APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")),
            "네트워크",
        ),
        (
            APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")),
            "시간",
        ),
        (AppError("사용할 수 없는 모델"), "사용할 수 없는 모델"),
    ],
)
def test_expected_cli_errors_are_stderr_and_nonzero(monkeypatch, capsys, error, text):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "ask", "hello"])
    monkeypatch.setattr(cli, "ask", raise_error(error))
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert text in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out


@pytest.mark.parametrize(
    "error", [ValueError("bug"), TypeError("bug"), KeyError("bug"), RuntimeError("bug")]
)
def test_programming_errors_are_not_swallowed(monkeypatch, error):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "ask", "hello"])
    monkeypatch.setattr(cli, "ask", raise_error(error))
    with pytest.raises(type(error), match="bug"):
        cli.main()


def test_programming_error_has_real_traceback():
    script = """
from hello_agent import cli
import sys
sys.argv = ["hello-agent", "ask", "hello"]
def bug(_):
    raise TypeError("handler bug")
cli.ask = bug
sys.exit(cli.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "Traceback" in result.stderr
    assert "TypeError: handler bug" in result.stderr


def test_repl_recovers_from_expected_error(monkeypatch, capsys):
    a = SimpleNamespace(
        model="test",
        compact=raise_error(status_error()),
        run=lambda _: print("recovered"),
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "create_agent", lambda: a)
    commands = iter(["/compact", "hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    cli.chat()
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert "recovered" in captured.out
    assert "Traceback" not in captured.err


def test_repl_does_not_swallow_bug(monkeypatch):
    a = SimpleNamespace(
        model="test", run=raise_error(TypeError("bug")), close=lambda: None
    )
    monkeypatch.setattr(cli, "create_agent", lambda: a)
    monkeypatch.setattr("builtins.input", lambda _: "hello")
    with pytest.raises(TypeError, match="bug"):
        cli.chat()


@pytest.mark.parametrize("arguments", ['{"unknown":1}', "{", "[]"])
def test_invalid_tool_arguments_are_expected(arguments):
    with pytest.raises(ToolError):
        create_registry().execute("ls", arguments)


@pytest.mark.parametrize(
    "error",
    [
        TypeError("inside handler"),
        ValueError("inside handler"),
        RuntimeError("inside handler"),
    ],
)
def test_tool_handler_bugs_escape_agent(error, monkeypatch):
    registry = ToolRegistry()
    registry.register(Tool("broken", "test", {}, raise_error(error)))
    a = Agent(
        None,
        "test",
        registry,
        SimpleNamespace(load=HistoryState, transaction=nullcontext),
    )
    call = SimpleNamespace(
        type="function_call",
        name="broken",
        arguments="{}",
        call_id="call",
        model_dump=lambda **_: {"type": "function_call"},
    )
    monkeypatch.setattr(a, "_generate", lambda _: SimpleNamespace(output=[call]))
    with pytest.raises(type(error), match="inside handler"):
        a.run("hello")


@pytest.mark.parametrize(
    "data",
    [
        "{",
        "[]",
        '{"version":2}',
        '{"version":1}',
        '{"version":2,"items":[],"context_items":null}',
    ],
)
def test_invalid_history_is_reported(tmp_path, monkeypatch, capsys, data):
    path = tmp_path / "history.json"
    path.write_text(data)
    monkeypatch.setattr(history, "HISTORY_FILE", path)
    monkeypatch.setattr(sys, "argv", ["hello-agent", "history", "ls"])
    assert cli.main() == 1
    assert "Traceback" not in capsys.readouterr().err
    assert path.read_text() == data


@pytest.mark.parametrize("data", ["{", "[]", '{"compact_threshold":0}', '{"model":42}'])
def test_invalid_settings_are_reported(tmp_path, monkeypatch, data):
    path = tmp_path / "settings.json"
    path.write_text(data)
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    with pytest.raises(AppError):
        settings.get_compact_threshold()
        settings.get_model()


def test_disk_error_preserves_state_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    path = tmp_path / "history.json"
    monkeypatch.setattr(history, "HISTORY_FILE", path)
    store = history.HistoryStore()
    state = HistoryState([{"role": "user", "content": "old"}], [])
    store.save(state)
    monkeypatch.setattr(
        history.os, "replace", raise_error(OSError(errno.ENOSPC, "disk full"))
    )
    with pytest.raises(AppError, match="disk full"):
        store.save(HistoryState())
    assert store.load() == state


def test_corrupt_credentials_are_expected(monkeypatch):
    monkeypatch.setattr(
        auth.keyring, "get_password", lambda *_: json.dumps({"client_id": "bad"})
    )
    with pytest.raises(AppError, match="인증정보"):
        auth.load_credentials()


def test_compact_401_cli_preserves_history(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.json")
    store = history.HistoryStore()
    state = HistoryState(
        [{"role": "user", "content": "hello"}], [{"role": "user", "content": "hello"}]
    )
    store.save(state)
    agent = Agent(None, "test", create_registry(), store)
    monkeypatch.setattr(agent, "_summarize", raise_error(status_error()))
    monkeypatch.setattr(cli, "create_agent", lambda: agent)
    monkeypatch.setattr(sys, "argv", ["hello-agent", "context", "compact"])

    assert cli.main() == 1

    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert "ChatPass" in captured.err
    assert "Traceback" not in captured.err
    assert store.load() == state


def test_sdk_stream_error_is_expected(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "ask", "hello"])
    error = APIError(
        "stream failed",
        request=httpx.Request("POST", "https://api.openai.com"),
        body=None,
    )
    monkeypatch.setattr(cli, "ask", raise_error(error))
    assert cli.main() == 1
    assert "stream failed" in capsys.readouterr().err
