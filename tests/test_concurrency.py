"""Real OS-process races against isolated shared stores; no real OAuth calls."""

import json
import multiprocessing
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hello_agent import auth, history, settings
from hello_agent.agent import Agent
from hello_agent.history import HistoryStore
from hello_agent.locking import file_lock
from hello_agent.tools import ToolRegistry
from hello_agent.vfs import Limits, SQLiteFileSystem


def paths(directory):
    directory = Path(directory)
    history.DATA_DIR = settings.CONFIG_DIR = auth.CONFIG_DIR = directory
    history.HISTORY_FILE = directory / "history.json"
    settings.SETTINGS_FILE = directory / "settings.json"
    auth.HOST_FILE = directory / "host-id"


def answer():
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }
    return SimpleNamespace(
        type="message", content=payload["content"], model_dump=lambda **_: payload
    )


def writer(directory, barrier, number):
    paths(directory)
    a = Agent(None, "test", ToolRegistry(), HistoryStore())
    barrier.wait(15)

    def generate(context):
        time.sleep(0.02)
        return SimpleNamespace(output=[answer()])

    a._generate = generate
    for turn in range(3):
        a.run(f"{number}:{turn}")


def setting_writer(directory, barrier, kind):
    paths(directory)
    barrier.wait(15)
    for _ in range(12):
        if kind == "model":
            settings.set_model("test", "high")
        else:
            settings.set_compact_threshold(64000)


def host_writer(directory, barrier, number):
    paths(directory)
    barrier.wait(15)
    (Path(directory) / f"host-{number}").write_text(auth.get_host_id())


def refresher(directory, barrier, number):
    paths(directory)
    credentials = Path(directory) / "credentials.json"
    auth.keyring.get_password = lambda *_: credentials.read_text()
    auth.keyring.set_password = lambda _service, _user, raw: credentials.write_text(raw)

    def post(*args, **kwargs):
        assert kwargs["data"]["refresh_token"] == "old-refresh"
        time.sleep(0.1)
        with (Path(directory) / "refresh-count").open("a") as output:
            output.write("refresh\n")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "fresh",
                "refresh_token": "rotated",
                "expires_in": 3600,
            },
        )

    auth.requests.post = post
    barrier.wait(15)
    (Path(directory) / f"token-{number}").write_text(auth.get_access_token())


def hold_lock(path, ready):
    with file_lock(Path(path)):
        ready.set()
        time.sleep(60)


def clear_history(directory, ready, done):
    paths(directory)
    ready.set()
    HistoryStore().clear()
    done.set()


def blocked_writer(directory, ready, release):
    paths(directory)
    a = Agent(None, "test", ToolRegistry(), HistoryStore())

    def generate(_):
        ready.set()
        assert release.wait(15)
        return SimpleNamespace(output=[answer()])

    a._generate = generate
    a.run("in flight")


def vfs_writer(directory, barrier, number):
    disk = SQLiteFileSystem(Path(directory) / "race.sqlite3", limits=Limits(8, 8))
    barrier.wait(15)
    for i in range(3):
        from hello_agent.vfs import VFSError

        try:
            disk.write_file(f"/{number}-{i}", b"12345678")
        except VFSError as exc:
            assert exc.code == "entry_limit"


def finish(processes):
    try:
        for process in processes:
            process.join(25)
            assert not process.is_alive(), "Process hung (possible deadlock)"
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def start_group(target, directory, count, args=None):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(count)
    processes = [
        ctx.Process(
            target=target, args=(str(directory), barrier, *(args(i) if args else (i,)))
        )
        for i in range(count)
    ]
    for process in processes:
        process.start()
        process._test_barrier = (
            barrier  # Keep semaphores alive until spawn unpickles them.
        )
    return processes


def test_parallel_turns_keep_all_messages_and_context(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.json")
    finish(start_group(writer, tmp_path, 4))
    state = HistoryStore().load()
    assert len(state.items) == 24
    assert state.context_items == state.items
    assert {x["content"] for x in state.items if x.get("role") == "user"} == {
        f"{i}:{j}" for i in range(4) for j in range(3)
    }


def test_parallel_configuration_updates_preserve_both_fields(tmp_path):
    finish(
        start_group(
            setting_writer, tmp_path, 4, lambda i: ("model" if i % 2 else "threshold",)
        )
    )
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "model": "test",
        "reasoning_effort": "high",
        "compact_threshold": 64000,
    }


def test_concurrent_host_id_creation_is_stable(tmp_path):
    finish(start_group(host_writer, tmp_path, 4))
    ids = {(tmp_path / f"host-{i}").read_text() for i in range(4)}
    assert len(ids) == 1
    assert ids == {(tmp_path / "host-id").read_text()}


def test_concurrent_refresh_rotates_once(tmp_path):
    (tmp_path / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "test",
                "subject": "test-user",
                "id_token": "test-id",
                "refresh_token": "old-refresh",
                "access_token": "expired",
                "expires_at": 0,
                "scopes": ["chatgpt.tokens.use.direct"],
            }
        )
    )
    finish(start_group(refresher, tmp_path, 4))
    assert (tmp_path / "refresh-count").read_text() == "refresh\n"
    assert {(tmp_path / f"token-{i}").read_text() for i in range(4)} == {"fresh"}
    assert (
        json.loads((tmp_path / "credentials.json").read_text())["refresh_token"]
        == "rotated"
    )


def test_process_crash_releases_lock_and_timeout_is_friendly(tmp_path):
    from hello_agent.errors import AppError

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    path = tmp_path / "crash.lock"
    process = ctx.Process(target=hold_lock, args=(str(path), ready))
    process.start()
    try:
        assert ready.wait(15)
        with (
            pytest.raises(AppError, match="시간이 초과"),
            file_lock(path, timeout=0.05),
        ):
            pytest.fail("Lock was not held")
    finally:
        process.terminate()
        process.join(5)
    with file_lock(path, timeout=1):
        assert path.exists()


def test_clear_waits_for_running_turn(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release, clearing, done = [ctx.Event() for _ in range(4)]
    writer_process = ctx.Process(
        target=blocked_writer, args=(str(tmp_path), ready, release)
    )
    clearer = ctx.Process(target=clear_history, args=(str(tmp_path), clearing, done))
    writer_process.start()
    try:
        assert ready.wait(15)
        clearer.start()
        assert clearing.wait(15)
        assert not done.wait(0.15)
        release.set()
        finish([writer_process, clearer])
        assert done.is_set()
        assert not (tmp_path / "history.json").exists()
    finally:
        release.set()
        for process in (writer_process, clearer):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5)


def test_idle_agent_reloads_after_clear_and_other_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.json")
    store = HistoryStore()
    a = Agent(None, "test", ToolRegistry(), store)
    a._generate = lambda _: SimpleNamespace(output=[answer()])
    a.run("discarded")
    store.clear()
    b = Agent(None, "test", ToolRegistry(), store)
    b._generate = a._generate
    b.run("other")
    a.run("latest")
    assert [x["content"] for x in store.load().items if x.get("role") == "user"] == [
        "other",
        "latest",
    ]


def test_multiple_processes_enforce_vfs_quota(tmp_path):
    finish(start_group(vfs_writer, tmp_path, 4))
    disk = SQLiteFileSystem(tmp_path / "race.sqlite3")
    assert disk.usage().entries == 8
    assert disk.usage().file_bytes == 56


def test_stale_agent_compacts_latest_state(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DATA_DIR", tmp_path)
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "history.json")
    store = HistoryStore()
    inputs = []

    def summarize(previous, chunk, target):
        inputs.append(chunk)
        return "added elsewhere"

    stale = Agent(None, "test", ToolRegistry(), store)
    stale._summarize = summarize
    current = Agent(None, "test", ToolRegistry(), store)
    current._generate = lambda _: SimpleNamespace(output=[answer()])
    current.run("added elsewhere")
    assert stale.compact()
    assert "added elsewhere" in inputs[0]
    assert store.load().items == current.history
    assert store.load().context_items == []
    assert store.load().summary == "added elsewhere"
    store.clear()
    assert stale.compact() is False


def test_lock_does_not_hide_handler_errors(tmp_path):
    for error in (
        OSError("handler bug"),
        json.JSONDecodeError("bug", "", 0),
        TypeError("bug"),
    ):
        with pytest.raises(type(error)), file_lock(tmp_path / "bug.lock"):
            raise error
        with file_lock(tmp_path / "bug.lock", timeout=0.1):
            pass


def test_client_fetches_current_token_for_each_http_request(monkeypatch):
    import httpx2
    from openai import OpenAI

    from hello_agent import api

    original = OpenAI
    tokens = iter(["first-token", "second-token"])
    seen = []

    def handle(request):
        seen.append(request.headers["Authorization"])
        return httpx2.Response(200, json={"object": "list", "data": []})

    def client(**kwargs):
        return original(
            **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle))
        )

    monkeypatch.setattr(api, "OpenAI", client)
    monkeypatch.setattr(api, "get_access_token", lambda: next(tokens))
    with api.create_client() as connection:
        connection.models.list()
        connection.models.list()
    assert seen == ["Bearer first-token", "Bearer second-token"]


def login_waiter(directory, ready, release):
    from hello_agent.errors import AppError

    paths(directory)

    def pause():
        ready.set()
        assert release.wait(15)
        raise AppError("cancelled login")

    auth.load_credentials = pause
    try:
        auth.login()
    except AppError as exc:
        assert str(exc) == "cancelled login"


def token_waiter(directory, ready, done):
    paths(directory)
    auth.keyring.get_password = lambda *_: json.dumps(
        {
            "client_id": "test",
            "subject": "user",
            "id_token": "test",
            "access_token": "cached",
            "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
            "scopes": [],
        }
    )
    ready.set()
    assert auth.get_access_token() == "cached"
    done.set()


def test_login_and_token_access_share_one_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release, requesting, done = [ctx.Event() for _ in range(4)]
    login = ctx.Process(target=login_waiter, args=(str(tmp_path), ready, release))
    token = ctx.Process(target=token_waiter, args=(str(tmp_path), requesting, done))
    login.start()
    try:
        assert ready.wait(15)
        token.start()
        assert requesting.wait(15)
        assert not done.wait(0.15)
        release.set()
        finish([login, token])
        assert done.is_set()
    finally:
        release.set()
        for process in (login, token):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5)


def test_thread_reentrancy_and_exclusion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    target = tmp_path / "counter"
    target.write_text("0")

    def increment(_):
        with file_lock(tmp_path / "counter.lock"), file_lock(tmp_path / "counter.lock"):
            value = int(target.read_text())
            time.sleep(0.005)
            target.write_text(str(value + 1))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(increment, range(24)))
    assert target.read_text() == "24"


def test_parallel_real_shells_share_initialization_and_files(tmp_path):
    import os
    import subprocess
    import sys

    executable = str(Path(sys.executable).parent / "hello-agent")
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    processes = [
        subprocess.Popen(
            [executable, "sh"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(4)
    ]
    try:
        for index, process in enumerate(processes):
            process.stdin.write(f'write_file /file-{index} "value-{index}"\nexit\n')
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, (stdout, stderr)
        disk = SQLiteFileSystem(tmp_path / "hello-agent/vfs.sqlite3")
        assert disk.usage().entries == 6  # root, initial README, four files
        for index in range(4):
            assert disk.read_file(f"/file-{index}") == f"value-{index}".encode()
        assert not (tmp_path / "hello-agent/history.json").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
