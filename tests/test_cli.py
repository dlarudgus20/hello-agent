import sys

import pytest

from hello_agent import cli
from hello_agent.history import HistoryState


def test_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "hello-agent" in output
    assert "ask" in output
    assert "chat" in output


def test_ask_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "ask", "hello"])
    messages = []
    monkeypatch.setattr(cli, "ask", messages.append)

    cli.main()

    assert messages == ["hello"]


def test_missing_model_does_not_create_client(monkeypatch):
    monkeypatch.setattr(cli.settings, "get_model", lambda: None)

    def unexpected_client():
        pytest.fail("Client creation must wait until a model is selected")

    monkeypatch.setattr(cli.api, "create_client", unexpected_client)

    with pytest.raises(RuntimeError, match="hello-agent model set"):
        cli.create_agent()


@pytest.mark.parametrize("options, expected", [([], 30), (["-n", "2"], 2)])
def test_history_ls_dispatch(monkeypatch, options, expected):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "history", "ls", *options])
    monkeypatch.setattr(cli.HistoryStore, "load", lambda _: HistoryState())
    received = []
    monkeypatch.setattr(
        cli, "print_history", lambda items, limit: received.append((items, limit))
    )

    cli.main()

    assert received == [([], expected)]


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_history_ls_rejects_invalid_limit(monkeypatch, value):
    monkeypatch.setattr(sys, "argv", ["hello-agent", "history", "ls", "-n", value])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
