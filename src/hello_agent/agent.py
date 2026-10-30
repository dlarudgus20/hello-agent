from contextlib import nullcontext
from copy import deepcopy
from functools import wraps

from openai import OpenAI
from rich.console import Console

from .context import (
    ContextManager,
    estimate,
    input_items,
    summary_input,
    summary_instructions,
)
from .errors import AppError, ToolError
from .history import HistoryState, HistoryStore
from .settings import DEFAULT_COMPACT_THRESHOLD
from .tool_trace import ToolTrace
from .tools import ToolRegistry

AGENT_INSTRUCTIONS = """
You are an assistant running in a virtual agent environment.

You may use only the tools explicitly provided to you.
Never claim to have performed an action unless a tool
actually completed that action.

If a required capability is unavailable, explain that
you cannot perform it.

Prefer responding to the user in Korean unless there is
a good reason to use another language, such as when the user
explicitly requests it, the context calls for it,
or another language would be more appropriate for the task.
"""


def history_operation(function):
    @wraps(function)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            state = self.store.load()
            self.history = state.items
            self.context_items = state.context_items
            self.summary = state.summary
            self.summarized_items = state.summarized_items
            return function(self, *args, **kwargs)

    return wrapper


TOOL_USAGE_REPEAT_LIMIT = 100


class Agent:
    def __init__(
        self,
        client: OpenAI,
        model: str,
        tools: ToolRegistry,
        history: HistoryStore,
        compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
        reasoning_effort: str | None = None,
        *,
        tool_trace: ToolTrace | None = None,
    ):
        self.client = client
        self.model = model
        self.tools = tools
        self.store = history
        self.compact_threshold = compact_threshold
        self.reasoning_effort = reasoning_effort
        self.tool_trace = tool_trace
        self.answer_prefix = ""

        state = self.store.load()
        self.history = state.items
        self.context_items = state.context_items
        self.summary = state.summary
        self.summarized_items = state.summarized_items

    def _commit(self, state: HistoryState) -> None:
        self.store.save(state)
        self.history = state.items
        self.context_items = state.context_items
        self.summary = state.summary
        self.summarized_items = state.summarized_items

    def close(self) -> None:
        self.tools.close()

    def _manager(self):
        return ContextManager(
            self.model,
            self.compact_threshold,
            AGENT_INSTRUCTIONS,
            self.tools.specifications(),
            self._summarize,
        )

    def _summarize(self, previous, chunk, target):
        instructions = summary_instructions(target)
        inputs = summary_input(previous, chunk)
        if estimate(self.model, instructions, inputs) > self.compact_threshold:
            raise AppError("요약 요청이 컨텍스트 기준을 초과합니다.")
        response = self._stream(
            {
                "model": self.model,
                "instructions": instructions,
                "input": inputs,
                "store": False,
                "max_output_tokens": 16384,
            },
            display=False,
        )
        parts = []
        for item in response.output:
            if item.type == "function_call":
                raise AppError("요약 응답에 허용되지 않은 도구 호출이 있습니다.")
            if item.type == "message":
                for part in item.content:
                    if part.type == "refusal":
                        raise AppError("모델이 요약 요청을 거절했습니다.")
                    if part.type == "output_text":
                        parts.append(part.text)
        return "".join(parts)

    def clear(self) -> None:
        with self.store.transaction():
            self.store.clear()
            self.history = []
            self.context_items = []
            self.summary = ""
            self.summarized_items = 0

    @history_operation
    def compact(self) -> bool:
        if not self.history and not self.summary:
            return False
        Console(stderr=True).print("컨텍스트 요약 중…", style="dim")
        state = HistoryState(
            self.history, self.context_items, self.summary, self.summarized_items
        )
        updated = self._manager().prepare(state, force=True)
        if updated == state:
            return False
        self._commit(updated)
        Console(stderr=True).print("컨텍스트 압축 완료.", style="green")
        return True

    @history_operation
    def run(self, message: str) -> None:
        # Work on a copy until the entire turn succeeds.
        user = {"role": "user", "content": message}
        current = [user]
        state = deepcopy(
            HistoryState(
                self.history, self.context_items, self.summary, self.summarized_items
            )
        )
        manager = self._manager()
        for _ in range(TOOL_USAGE_REPEAT_LIMIT):
            updated = manager.prepare(state, current)
            if updated != state:
                Console(stderr=True).print("컨텍스트 자동 요약 완료.", style="dim")
            state = updated
            context = input_items(state.summary, state.context_items + current)
            manager.require_fit(state.summary, state.context_items + current)
            response = self._generate(context)

            # Preserve all response output items, including reasoning.
            output = [
                item.model_dump(
                    mode="json",
                    exclude_none=True,
                )
                for item in response.output
            ]
            current.extend(output)

            calls = [item for item in response.output if item.type == "function_call"]

            if not calls:
                if not any(
                    item.type == "message" and item.content for item in response.output
                ):
                    raise AppError("Response completed without an answer or tool call")
                # Commit only after the response completes.
                self._commit(
                    HistoryState(
                        state.items + current,
                        state.context_items + current,
                        state.summary,
                        state.summarized_items,
                    )
                )
                return

            for call in calls:
                activity = (
                    self.tool_trace.call(call.name, call.arguments)
                    if self.tool_trace is not None
                    else nullcontext()
                )
                with activity as progress:
                    try:
                        output = self.tools.execute(call.name, call.arguments)
                    except ToolError as exc:
                        output = f"Tool execution failed: {exc}"
                        if progress is not None:
                            progress.failed(exc)
                    else:
                        if progress is not None:
                            progress.complete(output)

                tool_output = {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": output,
                }
                current.append(tool_output)

        raise AppError("Agent exceeded tool-call limit")

    def _generate(self, history: list[dict]):
        kwargs = {
            "model": self.model,
            "instructions": AGENT_INSTRUCTIONS,
            "input": history,
            "tools": self.tools.specifications(),
            "store": False,
        }
        if self.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}

        if (
            estimate(
                self.model, AGENT_INSTRUCTIONS, history, self.tools.specifications()
            )
            > self.compact_threshold
        ):
            raise AppError("컨텍스트 기준을 초과한 요청을 차단했습니다.")
        return self._stream(kwargs, display=True)

    def _stream(self, kwargs, *, display):
        if (
            estimate(
                self.model,
                kwargs["instructions"],
                kwargs["input"],
                kwargs.get("tools", []),
            )
            > self.compact_threshold
        ):
            raise AppError("컨텍스트 기준을 초과한 요청을 차단했습니다.")
        with self.client.responses.stream(**kwargs) as stream:
            printed = False
            completed_items = {}

            def emit(text):
                nonlocal printed
                if not text:
                    return
                if not printed and self.answer_prefix:
                    print(self.answer_prefix, end="", flush=True)
                print(text, end="", flush=True)
                printed = True

            for event in stream:
                if event.type == "response.output_item.done":
                    completed_items[event.output_index] = event.item
                if (
                    event.type == "response.output_text.delta"
                    or event.type == "response.refusal.delta"
                ):
                    if display:
                        emit(event.delta)
                elif event.type == "error":
                    raise AppError(f"Response error ({event.code}): {event.message}")
                elif event.type in ("response.failed", "response.incomplete"):
                    raise AppError(
                        f"Response {event.response.status}: "
                        f"{event.response.error or event.response.incomplete_details}"
                    )

            try:
                response = stream.get_final_response()
            except RuntimeError as exc:
                # The SDK exposes this specific incomplete-stream case as RuntimeError.
                if str(exc) != "Didn't receive a `response.completed` event.":
                    raise
                raise AppError(
                    "응답 스트림이 완료되기 전에 종료되었습니다. 다시 시도하세요."
                ) from exc

            # Some streams finalize items separately but leave final output empty.
            if completed_items:
                final_by_id = {
                    getattr(item, "id", None): item for item in response.output
                }
                recovered = [
                    final_by_id.get(getattr(item, "id", None), item)
                    if getattr(item, "id", None) is not None
                    else item
                    for _, item in sorted(completed_items.items())
                ]
                recovered_ids = {getattr(item, "id", None) for item in recovered}
                recovered.extend(
                    item
                    for item in response.output
                    if getattr(item, "id", None) not in recovered_ids
                )
                response.output = recovered

            if response.status != "completed":
                raise AppError(
                    f"Response {response.status}: "
                    f"{response.error or response.incomplete_details}"
                )

            if display and not printed:
                for item in response.output:
                    if item.type == "message":
                        for part in item.content:
                            if part.type == "output_text":
                                emit(part.text)
                            elif part.type == "refusal":
                                emit(part.refusal)

            if printed:
                print()

            return response
