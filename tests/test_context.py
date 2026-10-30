from copy import deepcopy
from types import SimpleNamespace

import pytest

from hello_agent import cli, history, settings
from hello_agent.agent import AGENT_INSTRUCTIONS, Agent
from hello_agent.context import (
    ContextManager,
    estimate,
    summary_input,
    summary_instructions,
    transcript,
    turns,
)
from hello_agent.errors import AppError
from hello_agent.history import HistoryState, HistoryStore
from hello_agent.tools import ToolRegistry


def item(**data):
    return SimpleNamespace(**data, model_dump=lambda **_: deepcopy(data))


def answer():
    return item(
        type="message",
        role="assistant",
        content=[{"type": "output_text", "text": "answer"}],
    )


def archive(count=8, size=1):
    return [
        entry
        for i in range(count)
        for entry in (
            {"role": "user", "content": f"question {i} " + "alpha " * size},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"answer {i}"}],
            },
        )
    ]


@pytest.fixture
def store():
    return HistoryStore()


def make_agent(store):
    a = Agent(None, "test-model", ToolRegistry(), store)
    a._summarize = lambda previous, chunk, target: "이전 목표와 결정 요약"
    return a


def test_manual_summary_keeps_archive_and_six_recent_turns(store):
    original = archive()
    store.save(HistoryState(original, original))
    a = make_agent(store)
    assert a.compact()
    state = store.load()
    assert state.items == original
    assert state.summary
    assert state.summarized_items == 4
    assert state.context_items == original[4:]
    assert turns(state.context_items) == 6
    restored = make_agent(store)
    seen = []
    restored._generate = lambda context: (
        seen.append(deepcopy(context)) or SimpleNamespace(output=[answer()])
    )
    restored.run("next")
    assert seen[0][0]["role"] == "assistant"
    assert state.summary in seen[0][0]["content"]
    assert seen[0][1:-1] == state.context_items
    assert store.load().items[: len(original)] == original


def test_summary_is_cumulative_and_preserves_references(store):
    original = archive()
    store.save(HistoryState(original, original))
    a = make_agent(store)
    calls = []

    def summarize(previous, chunk, target):
        calls.append((previous, chunk))
        return "누적 요약"

    a._summarize = summarize
    a.compact()
    first = store.load().summarized_items
    a.compact()
    assert calls[-1][0] == "누적 요약"
    assert "question 2" in calls[-1][1]
    assert store.load().summarized_items > first


def test_automatic_summary_and_tool_pairs(store):
    original = archive(size=150)
    store.save(HistoryState(original, original))
    a = make_agent(store)
    a.compact_threshold = 1000
    seen = []
    responses = iter(
        [
            [item(type="function_call", name="check", call_id="call", arguments="{}")],
            [answer()],
        ]
    )
    a.tools.execute = lambda *_: "tool result"

    def generate(context):
        assert (
            estimate(a.model, AGENT_INSTRUCTIONS, context, a.tools.specifications())
            <= 1000
        )
        seen.append(deepcopy(context))
        return SimpleNamespace(output=next(responses))

    a._generate = generate
    a.run("current request")
    assert store.load().summary
    assert store.load().items[: len(original)] == original
    assert seen[-1][-2]["call_id"] == seen[-1][-1]["call_id"] == "call"
    assert seen[-1][-1]["type"] == "function_call_output"
    assert any(x.get("content") == "current request" for x in seen[-1])


def test_large_history_and_tool_results_are_split_within_summary_budget():
    data = [
        {"role": "user", "content": "한국어 " * 3000},
        {"type": "function_call_output", "call_id": "x", "output": "huge " * 4000},
    ]
    seen = []

    def summarize(previous, chunk, target):
        assert (
            estimate(
                "unknown", summary_instructions(target), summary_input(previous, chunk)
            )
            <= 700
        )
        seen.append((previous, chunk))
        return "요약"

    manager = ContextManager("unknown", 700, "instructions", [], summarize)
    updated = manager.prepare(HistoryState(data, data), force=True)
    assert len(seen) > 1
    assert "".join(chunk for _, chunk in seen) == transcript(data)
    assert seen[1][0] == "요약"
    manager.require_fit(updated.summary, updated.context_items)


def test_too_large_current_turn_blocks_every_call(store):
    a = make_agent(store)
    a.compact_threshold = 300
    a._generate = lambda _: pytest.fail("oversized request sent")
    a._summarize = lambda *_: pytest.fail("current turn must not be summarized")
    with pytest.raises(AppError, match="초과"):
        a.run("big " * 1000)
    assert store.load() == HistoryState()


def test_small_threshold_blocks_summary_request_before_api(store):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)
    a.compact_threshold = 100
    a._summarize = lambda *_: pytest.fail("oversized summary sent")
    with pytest.raises(AppError):
        a.compact()
    assert store.load() == HistoryState(old, old)


@pytest.mark.parametrize("value", ["", " ", None])
def test_empty_summary_is_not_committed(store, value):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)
    a._summarize = lambda *_: value
    with pytest.raises(AppError):
        a.compact()
    assert store.load() == HistoryState(old, old)


def test_oversized_summary_has_bounded_retries(store):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)
    a.compact_threshold = 600
    calls = []
    a._summarize = lambda *_: calls.append(1) or "too long " * 1000
    with pytest.raises(AppError, match="목표 크기"):
        a.compact()
    assert len(calls) == 3
    assert store.load() == HistoryState(old, old)


@pytest.mark.parametrize("error", [AppError("failed"), KeyboardInterrupt()])
def test_failure_or_interrupt_preserves_all_state(store, error):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)

    def fail(*_):
        raise error

    a._summarize = fail
    with pytest.raises(type(error)):
        a.compact()
    assert a.summary == ""
    assert store.load() == HistoryState(old, old)


def test_failed_turn_after_summary_does_not_commit(store):
    old = archive(size=200)
    store.save(HistoryState(old, old))
    a = make_agent(store)
    a.compact_threshold = 900
    a._generate = lambda _: SimpleNamespace(output=[])
    with pytest.raises(AppError, match="without an answer"):
        a.run("new")
    assert store.load() == HistoryState(old, old)
    assert a.summary == ""


def test_summary_request_is_normal_response_without_tools_or_native_compact(store):
    a = make_agent(store)
    seen = []

    def stream(kwargs, display):
        seen.append((kwargs, display))
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="요약")],
                )
            ]
        )

    a._stream = stream
    assert Agent._summarize(a, "previous", "new data", 100) == "요약"
    request, display = seen[0]
    assert request["model"] == a.model
    assert request["store"] is False
    assert "tools" not in request
    assert "context_management" not in request
    assert not display


def test_refused_summary_is_an_error(store):
    a = make_agent(store)
    a._stream = lambda *args, **kwargs: SimpleNamespace(
        output=[
            SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])
        ]
    )
    with pytest.raises(AppError, match="거절"):
        Agent._summarize(a, "", "data", 100)


def test_old_native_context_is_rebuilt_from_archive(store):
    old = archive(1)
    history.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    import json

    history.HISTORY_FILE.write_text(
        json.dumps(
            {
                "version": 2,
                "items": old,
                "context_items": [
                    {"type": "compaction", "encrypted_content": "opaque"}
                ],
            }
        )
    )
    assert store.load() == HistoryState(old, old)
    assert "opaque" not in transcript(
        [{"type": "reasoning", "encrypted_content": "opaque", "summary": []}]
    )


def test_clear_and_empty_compaction(store):
    a = make_agent(store)
    assert not a.compact()
    old = archive(1)
    store.save(HistoryState(old, old))
    a.compact()
    a.clear()
    assert store.load() == HistoryState()
    assert a.summary == "" and not a.context_items


def test_context_cli_is_local_and_reports_estimates(store, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "create_agent", lambda: pytest.fail("No authentication needed")
    )
    for command in ("show", "compact"):
        monkeypatch.setattr("sys.argv", ["hello-agent", "context", command])
        assert cli.main() == 0
    output = capsys.readouterr().out
    assert "128,000" in output and "추정 입력 토큰" in output


def test_repl_summary_show_clear(store, monkeypatch, capsys):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)
    monkeypatch.setattr(cli, "create_agent", lambda: a)
    commands = iter(["/compact", "/context", "/clear", "/context", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    cli.chat()
    captured = capsys.readouterr()
    assert "압축됨" in captured.out and "압축 전" in captured.out
    assert "압축 완료" in captured.err
    assert store.load() == HistoryState()


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_threshold(value, monkeypatch):
    monkeypatch.setattr("sys.argv", ["hello-agent", "context", "set-threshold", value])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_threshold_persists(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["hello-agent", "context", "set-threshold", "64000"]
    )
    assert cli.main() == 0
    assert settings.get_compact_threshold() == 64000


def test_failed_save_does_not_update_summary_in_memory(store, monkeypatch):
    old = archive(1)
    store.save(HistoryState(old, old))
    a = make_agent(store)

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        a.compact()
    assert a.summary == "" and a.summarized_items == 0
    assert store.load() == HistoryState(old, old)


def test_oversized_tool_result_is_not_sent_or_replayed(store):
    a = make_agent(store)
    a.compact_threshold = 800
    calls = []
    executed = []

    def generate(context):
        calls.append(context)
        return SimpleNamespace(
            output=[
                item(type="function_call", name="large", arguments="{}", call_id="call")
            ]
        )

    a._generate = generate
    a.tools.execute = lambda *_: executed.append(1) or "huge " * 2000
    with pytest.raises(AppError, match="초과"):
        a.run("read")
    assert len(calls) == len(executed) == 1
    assert store.load() == HistoryState()


def test_budget_gate_prevents_actual_stream_call(store):
    a = make_agent(store)
    a.compact_threshold = 10
    a.client = SimpleNamespace(
        responses=SimpleNamespace(
            stream=lambda **_: pytest.fail("request escaped budget gate")
        )
    )
    with pytest.raises(AppError):
        a._stream(
            {
                "instructions": "instructions",
                "input": [{"role": "user", "content": "big " * 100}],
            },
            display=False,
        )
