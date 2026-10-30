import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from hello_agent import cli, shell
from hello_agent.errors import ToolError
from hello_agent.processes import TERMINAL
from hello_agent.tools import create_registry
from hello_agent.vfs import Limits, SQLiteFileSystem, VFSError, VirtualFileSystem


@pytest.fixture
def runtime(tmp_path):
    fs = VirtualFileSystem()
    fs.mount("/", SQLiteFileSystem(tmp_path / "lua.sqlite3"))
    with create_registry(fs) as registry:
        yield fs, registry


def execute(registry, name, **kwargs):
    return json.loads(registry.execute(name, json.dumps(kwargs)))


def run(runtime, source, **kwargs):
    fs, registry = runtime
    fs.write_file("/main.lua", source.encode())
    return execute(registry, "exec", path="/main.lua", **kwargs)


def boundary(runtime, pid, states=("waiting_input", *TERMINAL)):
    process = runtime[1].processes.get(pid)
    with process.condition:
        assert process.condition.wait_for(lambda: process.state in states, timeout=5)
    return json.loads(runtime[0].read_file(f"/sys/proc/{pid}/status"))


@pytest.mark.parametrize(
    "ending, code",
    [
        ("", 0),
        ("return 19, {}", 0),
        ("return function() end", 0),
        ("os.exit()", 0),
        ("os.exit(true)", 0),
        ("os.exit(false)", 1),
        ("os.exit(7)", 7),
        ("os.exit(-1)", 255),
        ("os.exit(256)", 0),
    ],
)
def test_exit_codes_and_discarded_return_values(runtime, ending, code):
    result = run(
        runtime, 'print("[red]literal[/red]"); io.stderr:write("err"); ' + ending
    )
    assert result["state"] == "exited" and result["exit_code"] == code
    assert result["stdout"] == "[red]literal[/red]\n" and result["stderr"] == "err"
    assert "return_values" not in result
    assert runtime[0].listdir("/sys/proc") == []


def test_arguments_varargs_and_foreground_round_trips(runtime):
    first = run(
        runtime,
        """
        local first, second = ...
        assert(arg[0] == "/main.lua" and first == arg[1] and second == arg[2])
        print(first, second)
        print("name?")
        local name = io.read()
        print("hello " .. name)
        local value = io.stdin:read("L")
        io.write(value)
        os.exit(4)
    """,
        args=["한글", "two words"],
    )
    pid = first["pid"]
    assert first["state"] == "waiting_input"
    assert first["waiting_for"] == {"format": "l"}
    assert first["stdout"] == "한글\ttwo words\nname?\n"
    assert runtime[0].read_file(f"/sys/proc/{pid}/stdout").decode() == first["stdout"]
    second = execute(runtime[1], "exec_input", pid=pid, input="Ada\n")
    assert second["state"] == "waiting_input" and second["stdout"] == "hello Ada\n"
    third = execute(runtime[1], "exec_input", pid=pid, input="bye\n")
    assert third["state"] == "exited" and third["exit_code"] == 4
    assert third["stdout"] == "bye\n"
    with pytest.raises(VFSError, match="not_found"):
        runtime[0].read_file(f"/sys/proc/{pid}/status")


def test_background_file_input_status_output_and_reap(runtime):
    fs, registry = runtime
    result = run(
        runtime, 'print("ready"); print(io.read()); os.exit(9)', background=True
    )
    pid = result["pid"]
    assert result["background"] and "stdout" not in result
    status = boundary(runtime, pid)
    assert status["background"] and status["exit_code"] is None
    assert [e.name for e in fs.listdir(f"/sys/proc/{pid}")] == [
        "control",
        "status",
        "stderr",
        "stdin",
        "stdout",
    ]
    assert fs.read_file(f"/sys/proc/{pid}/stdout") == b"ready\n"
    execute(registry, "write_file", path=f"/sys/proc/{pid}/stdin", content="answer\n")
    status = boundary(runtime, pid, TERMINAL)
    assert status["exit_code"] == 9
    assert fs.read_file(f"/sys/proc/{pid}/stdout") == b"ready\nanswer\n"
    assert fs.read_file(f"/sys/proc/{pid}/stdout") == b"ready\nanswer\n"
    with pytest.raises(VFSError, match="process_exited"):
        fs.write_file(f"/sys/proc/{pid}/stdin", b"late")
    execute(registry, "write_file", path=f"/sys/proc/{pid}/control", content="reap\n")
    assert fs.listdir("/sys/proc") == []


def test_mode_switches_empty_attach_and_background_retention(runtime):
    fs, registry = runtime
    result = run(
        runtime, 'print("first"); print(io.read()); print(io.read()); os.exit(3)'
    )
    pid = result["pid"]
    detached = execute(registry, "exec_input", pid=pid, input="one\n", background=True)
    assert detached["background"] and "stdout" not in detached
    boundary(runtime, pid)
    attached = execute(registry, "exec_input", pid=pid)
    assert attached["state"] == "waiting_input" and attached["stdout"] == "one\n"
    assert not json.loads(fs.read_file(f"/sys/proc/{pid}/status"))["background"]
    execute(registry, "exec_input", pid=pid, input="two\n", background=True)
    boundary(runtime, pid, TERMINAL)
    assert fs.listdir("/sys/proc")
    completed = execute(registry, "exec_input", pid=pid, background=False)
    assert completed["stdout"] == "two\n" and completed["exit_code"] == 3
    assert fs.listdir("/sys/proc") == []


def test_partial_input_does_not_invent_newlines_and_eof_is_permanent(runtime):
    _, registry = runtime
    result = run(
        runtime,
        'local x=io.read(); print(x); print(io.read()==nil); print(io.read("*a"))',
    )
    pid = result["pid"]
    partial = execute(registry, "exec_input", pid=pid, input="partial")
    assert partial["state"] == "waiting_input" and partial["stdout"] == ""
    result = execute(registry, "exec_input", pid=pid, eof=True)
    assert result["stdout"] == "partial\ntrue\n\n" and result["exit_code"] == 0


def test_stdin_byte_reads_and_all_read_across_multiple_messages(runtime):
    result = run(runtime, 'print(#io.read(70000)); print(#io.read("a"))')
    pid = result["pid"]
    execute(runtime[1], "exec_input", pid=pid, input="x" * 40000)
    result = execute(runtime[1], "exec_input", pid=pid, input="x" * 40000)
    assert result["stdout"] == "70000\n" and result["state"] == "waiting_input"
    result = execute(runtime[1], "exec_input", pid=pid, input="last", eof=True)
    assert result["stdout"] == "10004\n" and result["state"] == "exited"


def test_proc_file_eof_cancel_live_reap_and_read_permissions(runtime):
    fs, registry = runtime
    first = run(runtime, 'print(io.read("a"))', background=True)
    pid = first["pid"]
    boundary(runtime, pid)
    with pytest.raises(VFSError, match="busy"):
        fs.write_file(f"/sys/proc/{pid}/control", b"reap")
    with pytest.raises(VFSError, match="invalid_control"):
        fs.write_file(f"/sys/proc/{pid}/control", b"anything")
    for filename in ("stdin", "control"):
        with pytest.raises(VFSError, match="not_readable"):
            fs.read_file(f"/sys/proc/{pid}/{filename}")
    fs.write_file(f"/sys/proc/{pid}/stdin", b"raw\n")
    fs.write_file(f"/sys/proc/{pid}/control", b"eof")
    boundary(runtime, pid, TERMINAL)
    assert fs.read_file(f"/sys/proc/{pid}/stdout") == b"raw\n\n"
    fs.write_file(f"/sys/proc/{pid}/control", b"reap")
    second = run(runtime, "io.read()", background=True)
    pid2 = second["pid"]
    assert pid2 > pid
    fs.write_file(f"/sys/proc/{pid2}/control", b"cancel")
    status = boundary(runtime, pid2, TERMINAL)
    assert status["state"] == "cancelled" and status["exit_code"] == 130
    execute(registry, "exec_input", pid=pid2)  # collect an already cancelled job
    assert fs.listdir("/sys/proc") == []


@pytest.mark.parametrize("file", ["status", "stdout", "stderr"])
def test_proc_metadata_is_read_only(runtime, file):
    fs, _ = runtime
    pid = run(runtime, "io.read()", background=True)["pid"]
    with pytest.raises(VFSError, match="read_only"):
        fs.write_file(f"/sys/proc/{pid}/{file}", b"bad")
    for operation, args in (
        (fs.mkdir, (f"/sys/proc/{pid}/new",)),
        (fs.remove, (f"/sys/proc/{pid}",)),
        (fs.edit_file, (f"/sys/proc/{pid}/control", [{"old": "x", "new": "y"}])),
    ):
        with pytest.raises(VFSError, match="read_only"):
            operation(*args)


def test_search_skips_write_only_endpoints_and_proc_usage_has_no_recursion(runtime):
    fs, registry = runtime
    pid = run(runtime, 'print("needle"); io.read()', background=True)["pid"]
    boundary(runtime, pid)
    result = execute(registry, "search_files", paths=["/sys/proc"], queries=["needle"])
    assert {match["path"] for match in result["matches"]} == {f"/sys/proc/{pid}/stdout"}
    assert result["skipped_files"] == [
        {"path": f"/sys/proc/{pid}/control", "reason": "not_readable"},
        {"path": f"/sys/proc/{pid}/stdin", "reason": "not_readable"},
    ]
    info = json.loads(fs.read_file("/sys/fs/proc.json"))
    assert info["backend"] == "procfs" and info["entries"] == 7
    assert info["file_bytes"] is None and info["limits"] is None
    assert "database" not in fs.read_file(f"/sys/proc/{pid}/status").decode()


def test_file_io_binary_modes_seek_lines_close_and_extensions(runtime):
    fs, _ = runtime
    result = run(
        runtime,
        """
        assert(os.mkdir("/notes"))
        local f=assert(io.open("/notes/data", "wb"))
        assert(io.type(f)=="file")
        assert(f:write("a\\0b\\n", "한글", "\\nlast"))
        assert(f:seek("set", 0)==0)
        assert(f:write("A"))
        assert(f:flush()); assert(f:close())
        assert(io.type(f)=="closed file" and io.type({})==nil)
        local a=assert(io.open("/notes/data", "ab")); assert(a:write("!")); assert(a:close())
        local r <close> = assert(io.open("/notes/data", "rb"))
        assert(r:read(0)=="")
        assert(r:read(4)=="A\\0b\\n")
        assert(r:read("l")=="한글")
        assert(r:seek("end", -5))
        assert(r:read("*a")=="last!")
        assert(r:read()==nil)
        local entries=assert(io.listdir("/notes"))
        assert(entries[1].name=="data" and entries[1].type=="file")
        for line in io.lines("/notes/data") do io.write(line, "|") end
        assert(os.remove("/notes/data")); assert(os.remove("/notes"))
    """,
    )
    assert result["exit_code"] == 0, result
    assert result["stdout"] == "A\x00b|한글|last!|"
    assert [entry.name for entry in fs.listdir("/")] == ["main.lua", "sys"]


def test_io_expected_errors_and_mount_quotas_do_not_corrupt_files(runtime, tmp_path):
    fs, _ = runtime
    fs.mount("/small", SQLiteFileSystem(tmp_path / "small.db", limits=Limits(3, 3)))
    result = run(
        runtime,
        """
        local f=assert(io.open("/small/a", "w")); assert(f:write("abc"))
        local ok, message, code=f:write("d")
        assert(ok==nil and code=="file_too_large")
        assert(f:close())
        local a=assert(io.open("/small/a", "a"))
        ok,message,code=a:write("x"); assert(ok==nil and code=="file_too_large")
        local r=assert(io.open("/small/a")); assert(r:read("a")=="abc"); r:close()
        ok,message,code=io.open("/missing"); assert(ok==nil and code=="not_found")
        ok,message,code=io.open("/", "r"); assert(ok==nil and code=="is_directory")
        ok,message,code=io.open("/sys/fs/root.json", "w"); assert(ok==nil and code=="read_only")
        ok,message,code=io.open("/small/a", "r+"); assert(ok==nil and code=="invalid_arguments")
        ok,message,code=io.open(string.char(255)); assert(ok==nil and code=="invalid_arguments")
        ok,message,code=io.open("/etc/passwd"); assert(ok==nil and code=="not_found")
        ok,message,code=io.open("relative"); assert(ok==nil and code=="invalid_path")
    """,
    )
    assert result["exit_code"] == 0, result
    assert fs.read_file("/small/a") == b"abc"


def test_atomic_append_from_concurrent_workers(runtime):
    fs, registry = runtime
    fs.write_file("/shared", b"")
    fs.write_file(
        "/append.lua",
        b'local f=assert(io.open("/shared","a")); for i=1,30 do assert(f:write(arg[1])) end',
    )
    jobs = [
        execute(registry, "exec", path="/append.lua", args=[letter], background=True)
        for letter in ("x", "y")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda job: execute(registry, "exec_input", pid=job["pid"]), jobs)
        )
    assert all(result["exit_code"] == 0 for result in results), results
    assert sorted(fs.read_file("/shared")) == sorted(b"x" * 30 + b"y" * 30)


def test_lua_can_use_proc_streams_without_truncation_or_reentrant_wait(runtime):
    fs, registry = runtime
    target = run(runtime, "print(io.read()); os.exit(8)", background=True)["pid"]
    boundary(runtime, target)
    source = f"""
        local f=assert(io.open("/sys/proc/{target}/stdin", "w"))
        assert(f:write("from lua\\n")); f:close()
        print("sent")
    """
    sender = run(runtime, source)
    assert sender["exit_code"] == 0 and sender["stdout"] == "sent\n"
    boundary(runtime, target, TERMINAL)
    assert fs.read_file(f"/sys/proc/{target}/stdout") == b"from lua\n"
    result = execute(registry, "exec_input", pid=target)
    assert result["exit_code"] == 8


def test_sandbox_allowlist_and_pure_operations(runtime):
    result = run(
        runtime,
        """
        assert(_G==_ENV and _VERSION=="Lua 5.4")
        assert(python==nil and package==nil and require==nil and load==nil)
        assert(loadfile==nil and dofile==nil and debug==nil and coroutine==nil)
        assert(getmetatable==nil and setmetatable==nil and rawget==nil and rawset==nil)
        assert(pcall==nil and collectgarbage==nil)
        assert(string.dump==nil and ("x").dump==nil and ("x").__class__==nil)
        assert(io.popen==nil and io.tmpfile==nil and os.execute==nil and os.getenv==nil)
        assert(math.random==nil and math.randomseed==nil)
        assert(string.upper("hello")=="HELLO" and string.match("abc123", "%d+")=="123")
        local t={3,1,2}; table.sort(t); assert(table.concat(t, ",")=="1,2,3")
        assert(utf8.len("한글")==2 and math.sqrt(9)==3)
        print("safe")
    """,
    )
    assert result["exit_code"] == 0 and result["stdout"] == "safe\n", result


@pytest.mark.parametrize(
    "source",
    [
        "local = bad",
        'error("failure")',
        "return io.open.__globals__",
        'os.exit("x")',
        'io.read("n")',
        'io.read("l", "a")',
    ],
)
def test_lua_errors_are_results_with_path_not_python_tracebacks(runtime, source):
    result = run(runtime, source)
    assert result["state"] == "failed" and result["exit_code"] == 1, result
    assert result["error"]["code"] == "lua_error"
    assert "/main.lua" in result["error"]["message"]
    assert "Traceback (most recent call last)" not in result["error"]["message"]


def test_timeout_memory_output_and_handle_limits(runtime):
    fs, registry = runtime
    manager = registry.processes
    manager.limits = replace(manager.limits, active_seconds=0.1)
    result = run(runtime, 'print("before"); while true do end')
    assert result["exit_code"] == 124 and result["error"]["code"] == "timeout"
    assert result["stdout"] == "before\n"
    manager.limits = replace(manager.limits, active_seconds=5, memory_bytes=512 * 1024)
    result = run(runtime, "local t={} for i=1,1000000 do t[i]=i end")
    assert result["exit_code"] == 125 and result["error"]["code"] == "memory_limit", (
        result
    )
    manager.limits = replace(
        manager.limits, memory_bytes=64 * 1024 * 1024, output_bytes=100
    )
    result = run(runtime, 'io.write(string.rep("x", 4000000))')
    assert result["exit_code"] == 125 and result["error"]["code"] == "output_limit", (
        result
    )
    assert result["stdout"] == "x" * 100
    manager.limits = replace(manager.limits, output_bytes=65536, max_handles=2)
    fs.write_file("/data", b"x")
    result = run(
        runtime,
        """
        local a=assert(io.open("/data")); local b=assert(io.open("/data"))
        local c,message,code=io.open("/data"); assert(c==nil and code=="handle_limit")
        a:close(); assert(io.open("/data")); print("reused")
    """,
    )
    assert result["exit_code"] == 0 and result["stdout"] == "reused\n", result


def test_input_limits_preserve_live_state_and_waiting_does_not_use_time(runtime):
    _, registry = runtime
    registry.processes.limits = replace(
        registry.processes.limits, active_seconds=0.1, input_bytes=10
    )
    result = run(runtime, "print(io.read())")
    pid = result["pid"]
    process = registry.processes.get(pid)
    with process.condition:
        process.condition.wait(timeout=0.2)
    assert registry.processes.status(pid)["state"] == "waiting_input"
    with pytest.raises(VFSError, match="input_limit"):
        execute(registry, "exec_input", pid=pid, input="x" * 11)
    assert registry.processes.status(pid)["state"] == "waiting_input"
    result = execute(registry, "exec_input", pid=pid, input="ok\n")
    assert result["exit_code"] == 0 and result["stdout"] == "ok\n"


def test_process_slots_include_unreaped_exits_and_are_reusable(runtime):
    fs, registry = runtime
    registry.processes.limits = replace(registry.processes.limits, max_processes=2)
    first = run(runtime, "return 7", background=True)
    second = run(runtime, "return 7", background=True)
    for result in (first, second):
        boundary(runtime, result["pid"], TERMINAL)
    with pytest.raises(VFSError, match="process_limit"):
        run(runtime, "return")
    fs.write_file(f"/sys/proc/{first['pid']}/control", b"reap")
    assert run(runtime, "return")["exit_code"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "/no.txt"},
        {"path": "relative.lua"},
        {"path": "/missing.lua"},
        {"args": "string"},
        {"args": [1]},
        {"args": ["\ud800"]},
        {"background": 1},
    ],
)
def test_exec_invalid_arguments_are_tool_errors(runtime, kwargs):
    runtime[0].write_file("/main.lua", b"return")
    with pytest.raises(ToolError):
        execute(runtime[1], "exec", **{"path": "/main.lua", **kwargs})


def test_bytecode_binary_directory_and_trailing_slash_are_rejected(runtime):
    fs, registry = runtime
    fs.write_file("/bytecode.lua", b"\x1bLua")
    fs.write_file("/binary.lua", b"\xff")
    fs.mkdir("/directory.lua")
    fs.write_file("/main.lua", b"return")
    for path, code in (
        ("/bytecode.lua", "not_executable"),
        ("/binary.lua", "not_text"),
        ("/directory.lua", "is_directory"),
        ("/main.lua/", "not_directory"),
    ):
        with pytest.raises(VFSError, match=code):
            execute(registry, "exec", path=path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pid": True},
        {"pid": 0},
        {"pid": 999},
        {"input": 1},
        {"eof": 1},
        {"background": 1},
    ],
)
def test_exec_input_invalid_arguments(runtime, kwargs):
    pid = run(runtime, "io.read()")["pid"]
    with pytest.raises(ToolError):
        execute(runtime[1], "exec_input", **{"pid": pid, **kwargs})


def test_programming_errors_cross_the_supervisor_boundary(runtime, monkeypatch):
    def bug(*args):
        raise TypeError("broker bug")

    monkeypatch.setattr(runtime[1].processes, "_rpc", bug)
    with pytest.raises(TypeError, match="broker bug"):
        run(runtime, "io.read()")


def test_worker_python_errors_preserve_traceback(runtime):
    # Invalid bootstrap configuration is an implementation error, not Lua code.
    manager = runtime[1].processes
    with pytest.raises(RuntimeError, match="implementation error"):
        # Test-only invalid bootstrap input demonstrates the cross-process boundary.
        original = manager._send

        def corrupt(process, message):
            if "memory_bytes" in message:
                message = {**message, "memory_bytes": "invalid"}
            original(process, message)

        manager._send = corrupt
        run(runtime, "return")


def test_initial_foreground_call_owns_delivery_until_it_returns(runtime, monkeypatch):
    manager = runtime[1].processes
    entered, release = threading.Event(), threading.Event()
    original = manager._rpc

    def paused(process, op, args):
        if op == "emit":
            entered.set()
            assert release.wait(timeout=3)
        return original(process, op, args)

    monkeypatch.setattr(manager, "_rpc", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, runtime, 'print("ready"); io.read()')
        assert entered.wait(timeout=3)
        pid = manager.pids()[0]
        try:
            with pytest.raises(VFSError, match="busy"):
                execute(runtime[1], "exec_input", pid=pid)
        finally:
            release.set()
        assert future.result(timeout=3)["state"] == "waiting_input"


def test_interrupt_after_auto_reap_is_not_masked_by_unknown_pid(runtime, monkeypatch):
    manager = runtime[1].processes
    original = manager._observe

    def interrupted(process, background):
        original(process, background)
        raise KeyboardInterrupt()

    monkeypatch.setattr(manager, "_observe", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(runtime, "return")
    assert manager.pids() == []


def test_self_stdin_and_control_writes_are_nonblocking(runtime):
    result = run(
        runtime,
        """
        local f=assert(io.open("/sys/proc/1/stdin", "w"))
        assert(f:write("self\\n")); assert(f:close())
        print(io.read())
        local c=assert(io.open("/sys/proc/1/control", "w"))
        assert(c:write("eof")); c:close()
        assert(io.read()==nil)
    """,
    )
    assert result["state"] == "exited" and result["stdout"] == "self\n", result


def test_process_mounts_do_not_consume_full_db_quota(tmp_path):
    fs = VirtualFileSystem()
    fs.mount("/", SQLiteFileSystem(tmp_path / "full.db", limits=Limits(1024, 2)))
    fs.write_file("/main.lua", b"io.read()")
    with create_registry(fs) as registry:
        pid = execute(registry, "exec", path="/main.lua")["pid"]
        assert fs.usage("/")[1].entries == 2
        assert fs.usage("/sys/proc")[1].entries == 7
        assert execute(registry, "exec_input", pid=pid, eof=True)["exit_code"] == 0
        assert fs.usage("/")[1].entries == 2


def test_close_kills_waiting_children_and_schemas_have_only_two_execution_tools(
    runtime,
):
    fs, registry = runtime
    pid = run(runtime, "io.read()", background=True)["pid"]
    process = registry.processes.get(pid)
    specs = {spec["name"]: spec for spec in registry.specifications()}
    assert {name for name in specs if name.startswith("exec")} == {"exec", "exec_input"}
    assert all("/sys" not in spec["description"] for spec in specs.values())
    assert list(specs["exec_input"]["parameters"]["properties"]) == [
        "pid",
        "input",
        "eof",
        "background",
    ]
    registry.close()
    assert process.child.poll() is not None and not process.thread.is_alive()
    assert fs.listdir("/sys/proc") == []


def test_shell_output_and_mode_arguments_are_literal(runtime):
    fs, registry = runtime
    fs.write_file("/main.lua", b'print("[red]literal[/red]"); print(io.read())')
    output = StringIO()
    tool_shell = shell.ToolShell(registry, Console(file=output, width=100))
    tool_shell.execute(["exec", "/main.lua", "[]", "false"])
    pid = registry.processes.pids()[0]
    tool_shell.execute(["exec_input", str(pid), "done\n", "false", "false"])
    assert "[red]literal[/red]\n" in output.getvalue() and "done\n" in output.getvalue()
    assert "exit_code" in output.getvalue()


def test_real_sh_foreground_detach_proc_control_and_restart(tmp_path):
    executable = str(Path(sys.executable).parent / "hello-agent")
    commands = """write_file /main.lua 'print("ready"); print(io.read()); os.exit(6)'
exec /main.lua
exec_input 1 "hello
" false true
exec_input 1
exec /main.lua '[]' true
read_file /sys/proc/2/status
write_file /sys/proc/2/control cancel
read_file /sys/proc/2/status
write_file /sys/proc/2/control reap
ls /sys/proc
exit
"""
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    result = subprocess.run(
        [executable, "sh"],
        input=commands,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == "", result
    assert (
        "hello" in result.stdout
        and "exit_code" in result.stdout
        and "cancelled" in result.stdout
    )
    assert not (tmp_path / "hello-agent/history.json").exists()
    restarted = subprocess.run(
        [executable, "sh"],
        input="exec /main.lua '[]' true\nexit\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert restarted.returncode == 0 and restarted.stderr == ""


@pytest.mark.parametrize("command", ["ask", "chat"])
def test_agent_cli_lifetime_always_closes_tools(monkeypatch, command):
    class FakeAgent:
        model = "fake"
        closed = False

        def run(self, message):
            raise TypeError("bug")

        def close(self):
            self.closed = True

    agent = FakeAgent()
    monkeypatch.setattr(cli, "create_agent", lambda: agent)
    monkeypatch.setattr("builtins.input", lambda _: "message")
    with pytest.raises(TypeError, match="bug"):
        cli.ask("message") if command == "ask" else cli.chat()
    assert agent.closed
