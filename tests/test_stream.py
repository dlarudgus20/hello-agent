from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hello_agent.agent import Agent
from hello_agent.history import HistoryState
from hello_agent.tools import ToolRegistry


def make_agent(events, response, requests=None):
    @contextmanager
    def stream(**kwargs):
        if requests is not None:
            requests.append(kwargs)

        class Stream:
            def __iter__(self):
                return iter(events)

            def get_final_response(self):
                return response

        yield Stream()

    client = SimpleNamespace(responses=SimpleNamespace(stream=stream))
    return Agent(
        client, "test-model", ToolRegistry(), SimpleNamespace(load=HistoryState)
    )


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_stream_failure_is_reported(status):
    response = SimpleNamespace(
        status=status, error="failure reason", incomplete_details=None
    )
    agent = make_agent(
        [SimpleNamespace(type=f"response.{status}", response=response)], response
    )

    with pytest.raises(RuntimeError, match="failure reason"):
        agent._generate([])


def test_final_text_without_deltas_is_printed(capsys):
    response = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="hello")],
            )
        ],
    )
    agent = make_agent([], response)

    agent._generate([])

    assert capsys.readouterr().out == "hello\n"


def test_empty_final_output_preserves_completed_tool_calls():
    call = SimpleNamespace(type="function_call", name="ls", arguments='{"path":"/"}')
    response = SimpleNamespace(status="completed", output=[])
    event = SimpleNamespace(type="response.output_item.done", output_index=0, item=call)
    agent = make_agent([event], response)

    result = agent._generate([])

    assert result.output == [call]


def test_native_compaction_is_not_requested():
    response = SimpleNamespace(status="completed", output=[])
    requests = []
    agent = make_agent([], response, requests)
    agent._generate([])
    assert "context_management" not in requests[0]
    assert requests[0]["store"] is False


@pytest.mark.parametrize(
    "effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_reasoning_effort_is_sent_on_every_generation(effort):
    response = SimpleNamespace(status="completed", output=[])
    requests = []
    agent = make_agent([], response, requests)
    agent.reasoning_effort = effort

    # Each follow-up, including those after tool execution, uses this same path.
    agent._generate([])
    agent._generate([{"role": "user", "content": "next"}])

    for request in requests:
        if effort is None:
            assert "reasoning" not in request
        else:
            assert request["reasoning"] == {"effort": effort}
