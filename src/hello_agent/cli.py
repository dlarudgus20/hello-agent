import argparse

from rich.console import Console
from rich.table import Table

from . import api, auth, settings, shell
from .agent import AGENT_INSTRUCTIONS, Agent
from .context import estimate, input_items, turns
from .errors import EXPECTED_ERRORS, AppError, report_error
from .history import HistoryState, HistoryStore
from .history_view import print_history
from .tools import create_registry
from .vfs import VirtualFileSystem


def main() -> int:
    try:
        _main()
    except EXPECTED_ERRORS as exc:
        report_error(exc)
        return 1
    except KeyboardInterrupt:
        Console(stderr=True).print("중단했습니다.")
        return 130
    return 0


def _main():
    parser = argparse.ArgumentParser(
        prog="hello-agent",
        description="ChatGPT-powered local agent",
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser("login")
    commands.add_parser("chat")
    commands.add_parser("sh", help="Open an interactive tool shell")

    model_parser = commands.add_parser("model")
    model_commands = model_parser.add_subparsers(
        dest="model_command",
        required=True,
    )

    model_commands.add_parser("ls")
    model_commands.add_parser("show")

    set_parser = model_commands.add_parser("set")
    set_parser.add_argument("name")
    set_parser.add_argument(
        "-r",
        "--reasoning",
        "--reasoning-effort",
        dest="reasoning_effort",
        choices=settings.REASONING_EFFORTS,
        help="Reasoning effort (omit to use the model default)",
    )

    history_parser = commands.add_parser("history")
    history_commands = history_parser.add_subparsers(
        dest="history_command",
        required=True,
    )

    history_commands.add_parser("clear")
    history_ls = history_commands.add_parser("ls", help="Show recent history")
    history_ls.add_argument(
        "-n",
        type=positive_int,
        default=30,
        metavar="maxnum",
        help="Maximum number of entries to show (default: 30)",
    )

    context_parser = commands.add_parser("context")
    context_commands = context_parser.add_subparsers(
        dest="context_command", required=True
    )
    context_commands.add_parser("show")
    context_commands.add_parser("compact")
    threshold_parser = context_commands.add_parser("set-threshold")
    threshold_parser.add_argument("tokens", type=positive_int)

    ask_parser = commands.add_parser("ask")
    ask_parser.add_argument("message")

    args = parser.parse_args()

    if args.command == "login":
        login()

    elif args.command == "model":
        if args.model_command == "ls":
            list_models()
        elif args.model_command == "set":
            set_model(args.name, args.reasoning_effort)
        elif args.model_command == "show":
            show_model()

    elif args.command == "ask":
        ask(args.message)

    elif args.command == "chat":
        chat()
    elif args.command == "sh":
        shell.run_shell()

    elif args.command == "history" and args.history_command == "clear":
        clear_history()
    elif args.command == "history" and args.history_command == "ls":
        print_history(HistoryStore().load().items, limit=args.n)
    elif args.command == "context":
        if args.context_command == "show":
            show_context(HistoryStore().load())
        elif args.context_command == "compact":
            state = HistoryStore().load()
            if state.items or state.summary:
                create_agent().compact()
            else:
                print("압축할 대화 기록이 없습니다.")
        elif args.context_command == "set-threshold":
            settings.set_compact_threshold(args.tokens)
            print(f"자동 압축 기준: {args.tokens:,} 토큰")


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def login() -> None:
    auth.login()


def list_models() -> None:
    models = api.list_models()
    with settings.transaction():
        selected = settings.get_model()
        effort = settings.get_reasoning_effort()

    for model in models:
        slug = model["slug"]
        marker = "*" if slug == selected else " "

        suffix = ""
        if slug == selected:
            # Catalog UI defaults need not match the inference API's default.
            label = effort if effort is not None else "기본값"
            suffix = f" · 추론: {label}"
        print(f"{marker} {slug:28} {model.get('display_name', '')}{suffix}")


def set_model(name: str, reasoning_effort: str | None = None) -> None:
    available = {model["slug"]: model for model in api.list_models()}

    if name not in available:
        raise AppError(f"사용할 수 없는 모델입니다: {name}")

    if reasoning_effort is not None:
        supported = [
            level["effort"]
            for level in available[name].get("supported_reasoning_levels", [])
            if level["effort"] in settings.REASONING_EFFORTS
        ]
        if reasoning_effort not in supported:
            choices = ", ".join(supported) or "없음"
            raise AppError(
                f"{name}에서 추론 수준 {reasoning_effort}을 사용할 수 없습니다. "
                f"지원 수준: {choices}"
            )

    settings.set_model(name, reasoning_effort)
    print(f"Selected model: {name} · 추론: {reasoning_effort or '기본값'}")


def show_model() -> None:
    with settings.transaction():
        model = settings.get_model()
        effort = settings.get_reasoning_effort()
    if model is None:
        print("No model selected.")
    else:
        print(f"{model} · 추론: {effort or '기본값'}")


def resolve_model() -> str:
    model = settings.get_model()

    if model is None:
        raise AppError("No model selected. Use 'hello-agent model set <name>' first.")

    return model


def create_agent() -> Agent:
    with settings.transaction():
        model = resolve_model()
        reasoning_effort = settings.get_reasoning_effort()
        compact_threshold = settings.get_compact_threshold()
    return Agent(
        client=api.create_client(),
        model=model,
        tools=create_registry(),
        history=HistoryStore(),
        compact_threshold=compact_threshold,
        reasoning_effort=reasoning_effort,
    )


def ask(message: str) -> None:
    agent = create_agent()
    agent.run(message)


def chat() -> None:
    agent = create_agent()

    print(f"Chat started (model: {agent.model})")
    print("Commands: /exit, /clear, /context, /compact")
    print()

    while True:
        try:
            message = input("You> ").strip()
        except EOFError, KeyboardInterrupt:
            print()
            break

        if not message:
            continue

        if message == "/exit":
            break

        try:
            if message == "/clear":
                agent.clear()
                print("History cleared.")
                continue
            if message == "/context":
                show_context(
                    agent.store.load(),
                    model=agent.model,
                    threshold=agent.compact_threshold,
                )
                continue
            if message == "/compact":
                if not agent.compact():
                    print("압축할 대화 기록이 없습니다.")
                continue
            print("AI> ", end="", flush=True)
            agent.run(message)
        except KeyboardInterrupt:
            print("\nInterrupted. Turn not saved.")
        except EXPECTED_ERRORS as exc:
            report_error(exc)

    print("Goodbye.")


def clear_history() -> None:
    HistoryStore().clear()
    print("History cleared.")


def show_context(
    state: HistoryState, *, model: str | None = None, threshold: int | None = None
) -> None:
    with settings.transaction():
        model = model or settings.get_model() or "unknown-model"
        threshold = (
            threshold if threshold is not None else settings.get_compact_threshold()
        )
    table = Table(title="컨텍스트", border_style="cyan", show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("원문 항목", f"{len(state.items):,}")
    table.add_row("활성 컨텍스트 항목", f"{len(state.context_items):,}")
    table.add_row("요약 상태", "압축됨" if state.summary else "압축 전")
    table.add_row("유지 원문 턴", str(turns(state.context_items)))
    tools = create_registry(VirtualFileSystem()).specifications()
    size = estimate(
        model,
        AGENT_INSTRUCTIONS,
        input_items(state.summary, state.context_items),
        tools,
    )
    table.add_row("추정 입력 토큰", f"{size:,}")
    table.add_row("컨텍스트 기준 (추정)", f"{threshold:,} 토큰")
    Console().print(table)
