from io import StringIO

from rich.console import Console

from hello_agent.history_view import print_history


def render(items, limit=30, *, color=False):
    output = StringIO()
    print_history(
        items,
        limit,
        console=Console(
            file=output,
            width=70,
            force_terminal=color,
            no_color=False,
            color_system="standard" if color else None,
        ),
    )
    return output.getvalue()


def test_recent_entries_exclude_reasoning_and_keep_order():
    items = [
        {"role": "user", "content": "old message"},
        {"type": "function_call", "name": "ls", "arguments": '{"path":"/"}'},
        {"type": "function_call_output", "output": '{"files":["README.txt"]}'},
        {"type": "reasoning", "summary": []},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "파일을 찾았습니다."}],
        },
    ]

    output = render(items, 3)

    assert "old message" not in output
    assert "최근 3개 / 전체 4개" in output
    assert (
        output.index("TOOL CALL")
        < output.index("TOOL RESULT")
        < output.index("004  AI")
    )
    assert "파일을 찾았습니다." in output
    assert "\x1b[" not in output


def test_default_limit_is_30():
    output = render(
        [{"role": "user", "content": f"message-{i:03d}"} for i in range(35)]
    )

    assert "message-004" not in output
    assert "message-005" in output
    assert "message-034" in output


def test_empty_history():
    assert "아직 대화 기록이 없습니다." in render([])


def test_color_and_literal_user_text():
    output = render([{"role": "user", "content": "[red]literal[/red]"}], color=True)

    assert "\x1b[" in output
    assert "[red]literal[/red]" in output
