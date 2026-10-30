from types import SimpleNamespace

import pytest

from hello_agent import history
from hello_agent.agent import Agent
from hello_agent.tools import ToolRegistry


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.json")
    return history.HistoryStore()


def test_successful_turn_is_shared_by_new_agent(store, monkeypatch):
    agent = Agent(None, "test-model", ToolRegistry(), store)
    output = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }
    message = SimpleNamespace(
        type="message", content=output["content"], model_dump=lambda **_: output
    )
    monkeypatch.setattr(agent, "_generate", lambda _: SimpleNamespace(output=[message]))

    agent.run("hello")

    restored = Agent(None, "test-model", ToolRegistry(), store)
    assert restored.history == [{"role": "user", "content": "hello"}, output]


def test_failed_turn_preserves_history(store, monkeypatch):
    original = [{"role": "user", "content": "previous"}]
    store.save(history.HistoryState(original, original))
    agent = Agent(None, "test-model", ToolRegistry(), store)

    def fail(_):
        raise RuntimeError("generation failed")

    monkeypatch.setattr(agent, "_generate", fail)

    with pytest.raises(RuntimeError, match="generation failed"):
        agent.run("new message")

    assert agent.history == original
    assert store.load() == history.HistoryState(original, original)


def test_empty_response_is_not_saved(store, monkeypatch):
    agent = Agent(None, "test-model", ToolRegistry(), store)
    monkeypatch.setattr(agent, "_generate", lambda _: SimpleNamespace(output=[]))

    with pytest.raises(RuntimeError, match="without an answer"):
        agent.run("hello")

    assert store.load() == history.HistoryState()
