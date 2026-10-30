"""Supervise disposable Lua workers; only the parent can access the VFS.

Worker messages are bounded JSON, never pickle. Thread-boundary exceptions are
retained and re-raised by the next related foreground/procfs operation.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .vfs.core import VFSError, normalize_path

MAX_MESSAGE = 3 * 1024 * 1024
FILE_BUFFER_LIMIT = 1024 * 1024
TERMINAL = frozenset(("exited", "failed", "cancelled"))


def pack(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, list):
        return [pack(item) for item in value]
    return value


def unpack(value):
    if isinstance(value, dict) and set(value) == {"bytes"}:
        return base64.b64decode(value["bytes"], validate=True)
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value


def text(value: bytes, name="path") -> str:
    if not isinstance(value, bytes):
        raise VFSError("invalid_arguments", f"{name} must be a string")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VFSError("invalid_arguments", f"{name} must be UTF-8") from exc


@dataclass(frozen=True)
class ProcessLimits:
    max_processes: int = 4
    memory_bytes: int = 64 * 1024 * 1024
    active_seconds: float = 5
    output_bytes: int = 64 * 1024
    input_bytes: int = 64 * 1024
    max_handles: int = 32
    startup_seconds: float = 10


@dataclass
class FileHandle:
    path: str
    mode: str
    data: bytes = b""
    position: int = 0
    stream: bool = False


@dataclass
class Process:
    pid: int
    path: str
    args: list[str]
    background: bool
    child: subprocess.Popen
    limits: ProcessLimits
    condition: threading.Condition = field(default_factory=threading.Condition)
    request_lock: threading.Lock = field(default_factory=threading.Lock)
    state: str = "running"
    exit_code: int | None = None
    error: dict | None = None
    fatal: Exception | None = None
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    delivered: dict = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    input: bytearray = field(default_factory=bytearray)
    input_eof: bool = False
    reading_bytes: int = 0
    waiting_for: dict | None = None
    handles: dict[int, FileHandle] = field(default_factory=dict)
    next_handle: int = 1
    ready: bool = False
    active_seconds: float = 0
    created: float = field(default_factory=time.monotonic)
    last_tick: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    thread: threading.Thread | None = None

    def tick(self):
        now = time.monotonic()
        if self.ready and self.state == "running":
            self.active_seconds += now - self.last_tick
        self.last_tick = now


class ProcessManager:
    def __init__(self, vfs, *, limits: ProcessLimits | None = None):
        self.vfs = vfs
        self.limits = limits or ProcessLimits()
        self._lock = threading.RLock()
        self._processes: dict[int, Process] = {}
        self._workers: dict[int, Process] = {}
        self._next_pid = 1
        self._closed = threading.Event()
        self._watcher = None

    def mount(self):
        from .vfs.proc import ProcessFileSystem
        from .vfs.system import SystemFileSystem

        paths = {mount.path for mount in self.vfs.mounts()}
        if "/sys" not in paths:
            self.vfs.mount("/sys", SystemFileSystem(self.vfs.mounts))
        if "/sys/proc" not in paths:
            self.vfs.mount("/sys/proc", ProcessFileSystem(self), name="proc")

    def pids(self):
        with self._lock:
            return sorted(self._processes)

    def get(self, pid) -> Process:
        if type(pid) is not int or pid < 1:
            raise VFSError("invalid_arguments", "pid must be a positive integer")
        with self._lock:
            process = self._processes.get(pid)
        if process is None:
            raise VFSError("not_found", f"Unknown process: {pid}")
        return process

    def status(self, pid):
        process = self.get(pid)
        with process.condition:
            self._check_fatal(process)
            process.tick()
            return {
                "pid": process.pid,
                "path": process.path,
                "args": process.args,
                "background": process.background,
                "state": process.state,
                "exit_code": process.exit_code,
                "error": process.error,
                "started_at": process.started_at,
                "active_seconds": round(process.active_seconds, 6),
                "waiting_for": process.waiting_for,
                "input_eof": process.input_eof,
                "input_bytes": len(process.input) + process.reading_bytes,
                "stdout_bytes": len(process.stdout),
                "stderr_bytes": len(process.stderr),
            }

    def output(self, pid, stream):
        process = self.get(pid)
        with process.condition:
            self._check_fatal(process)
            return bytes(getattr(process, stream))

    @staticmethod
    def _check_fatal(process):
        if process.fatal is not None:
            raise process.fatal

    def start(self, path, args=None, background=False):
        if type(background) is not bool:
            raise VFSError("invalid_arguments", "background must be a boolean")
        if args is None:
            args = []
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise VFSError("invalid_arguments", "args must be an array of strings")
        try:
            # Include one byte per argument so empty argv entries are bounded too.
            argument_bytes = sum(len(arg.encode("utf-8")) + 1 for arg in args)
        except UnicodeEncodeError as exc:
            raise VFSError("invalid_arguments", "args must be UTF-8") from exc
        if argument_bytes > self.limits.input_bytes:
            raise VFSError("input_limit", "Arguments exceed the input byte limit")
        raw_path = path
        path = normalize_path(path)
        if not path.endswith(".lua"):
            raise VFSError("not_executable", "Only .lua files are executable")
        if raw_path.endswith("/"):
            raise VFSError("not_directory", "Executable path must name a file")
        source = self.vfs.read_file(path)
        if len(source) > FILE_BUFFER_LIMIT:
            raise VFSError("file_too_large", "Lua source exceeds 1 MiB")
        if source.startswith(b"\x1bLua"):
            raise VFSError("not_executable", "Lua bytecode is not executable")
        try:
            source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VFSError("not_text", "Lua source must be UTF-8 text") from exc
        self.mount()
        with self._lock:
            if self._closed.is_set():
                raise VFSError("closed", "Execution environment is closed")
            if len(self._processes) >= self.limits.max_processes:
                raise VFSError(
                    "process_limit",
                    f"Reap an exited process first; existing PIDs: {self.pids()}",
                )
            pid = self._next_pid
            self._next_pid += 1
            # Isolated Python startup: no project cwd, PYTHONPATH, or credentials.
            env = {
                key: os.environ[key]
                for key in ("SystemRoot", "WINDIR")
                if key in os.environ
            }
            try:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("_lua_worker.py")),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=tempfile.gettempdir(),
                    env=env,
                )
            except OSError as exc:
                raise VFSError(
                    "startup_error", f"Cannot start Lua worker: {exc.strerror}"
                ) from exc
            process = Process(pid, path, list(args), background, child, self.limits)
            process.request_lock.acquire()
            self._processes[pid] = process
            self._workers[pid] = process
            process.thread = threading.Thread(
                target=self._serve,
                args=(process, source),
                daemon=True,
                name=f"lua-{pid}",
            )
            process.thread.start()
            if self._watcher is None:
                self._watcher = threading.Thread(
                    target=self._watch, daemon=True, name="lua-watchdog"
                )
                self._watcher.start()
        try:
            return self._observe(process, background)
        except KeyboardInterrupt:
            self._cancel(process)
            raise
        finally:
            process.request_lock.release()

    def input(self, pid, input="", eof=False, background=False):
        if type(background) is not bool or type(eof) is not bool:
            raise VFSError("invalid_arguments", "background and eof must be booleans")
        if not isinstance(input, str):
            raise VFSError("invalid_arguments", "input must be UTF-8 text")
        try:
            data = input.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise VFSError("invalid_arguments", "input must be UTF-8") from exc
        process = self.get(pid)
        if not process.request_lock.acquire(blocking=False):
            raise VFSError("busy", "Another foreground request owns this process")
        try:
            with process.condition:
                self._check_fatal(process)
                # An empty foreground request can attach to/collect a background job.
                if process.state in TERMINAL and (data or eof):
                    raise VFSError("process_exited", "Process has already exited")
                self._add_input(process, data, eof)
                process.background = background
            return self._observe(process, background)
        except KeyboardInterrupt:
            self._cancel(process)
            raise
        finally:
            process.request_lock.release()

    def send(self, pid, data: bytes, eof=False):
        process = self.get(pid)
        with process.condition:
            if process.state in TERMINAL:
                raise VFSError("process_exited", "Process has already exited")
            self._add_input(process, data, eof)

    @staticmethod
    def _add_input(process, data, eof):
        if (
            len(data) > process.limits.input_bytes
            or len(process.input) + len(data) > process.limits.input_bytes
        ):
            raise VFSError("input_limit", "Input buffer exceeds the byte limit")
        if process.input_eof and data:
            raise VFSError("stdin_closed", "Standard input is already at EOF")
        process.input.extend(data)
        process.input_eof |= eof
        if process.state == "waiting_input" and (data or eof):
            process.tick()
            process.state = "running"
        process.condition.notify_all()

    def _observe(self, process, background):
        with process.condition:
            if not background:
                while process.state == "running":
                    process.condition.wait()
            self._check_fatal(process)
            result = {
                "status": "success",
                "pid": process.pid,
                "proc_path": f"/sys/proc/{process.pid}",
                "background": background,
                "state": process.state,
                "exit_code": process.exit_code,
                "error": process.error,
            }
            if not background:
                for stream in ("stdout", "stderr"):
                    output = getattr(process, stream)
                    result[stream] = bytes(output[process.delivered[stream] :]).decode(
                        "utf-8", errors="replace"
                    )
                    process.delivered[stream] = len(output)
                result["waiting_for"] = process.waiting_for
                if process.state in TERMINAL:
                    # An explicit procfs reap can race result delivery; the held
                    # Process still owns the final snapshot in that case.
                    with self._lock:
                        self._processes.pop(process.pid, None)
            return result

    def cancel(self, pid):
        self._cancel(self.get(pid))

    def _cancel(self, process):
        with process.condition:
            if process.state not in TERMINAL:
                self._finish(
                    process,
                    "cancelled",
                    130,
                    {"code": "cancelled", "message": "Execution cancelled"},
                )
                self._kill(process)

    def reap(self, pid):
        process = self.get(pid)
        with process.condition:
            if process.state not in TERMINAL:
                raise VFSError("busy", "Only exited processes can be reaped")
            with self._lock:
                if self._processes.get(pid) is not process:
                    raise VFSError("not_found", f"Unknown process: {pid}")
                del self._processes[pid]

    @staticmethod
    def _finish(process, state, code, error=None):
        with process.condition:
            if process.state in TERMINAL:
                return
            process.tick()
            process.state, process.exit_code, process.error = state, code, error
            process.waiting_for = None
            process.condition.notify_all()

    @staticmethod
    def _kill(process):
        try:
            process.child.kill()
        except ProcessLookupError:
            pass

    def _watch(self):
        while not self._closed.wait(0.02):
            with self._lock:
                workers = list(self._workers.values())
            for process in workers:
                with process.condition:
                    process.tick()
                    startup = (
                        not process.ready
                        and time.monotonic() - process.created
                        > process.limits.startup_seconds
                    )
                    if process.state not in TERMINAL and (
                        startup
                        or process.active_seconds >= process.limits.active_seconds
                    ):
                        self._finish(
                            process,
                            "failed",
                            124,
                            {
                                "code": "timeout",
                                "message": "Lua execution time limit exceeded",
                            },
                        )
                        self._kill(process)

    @staticmethod
    def _send(process, message):
        data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_MESSAGE:
            raise VFSError("message_limit", "Worker message exceeds the byte limit")
        process.child.stdin.write(data)
        process.child.stdin.flush()

    def _serve(self, process, source):
        try:
            self._send(
                process,
                {
                    "source": pack(source),
                    "path": process.path,
                    "args": process.args,
                    "memory_bytes": process.limits.memory_bytes,
                    "output_bytes": process.limits.output_bytes,
                },
            )
            while True:
                line = process.child.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    if process.state not in TERMINAL:
                        raise RuntimeError(
                            f"Lua worker {process.pid} exited without a result"
                        )
                    break
                if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    raise RuntimeError("Invalid Lua worker message length")
                message = json.loads(line)
                if message.get("event") == "ready":
                    with process.condition:
                        process.ready = True
                        process.last_tick = time.monotonic()
                elif message.get("event") == "exit":
                    self._finish(
                        process,
                        message["state"],
                        message["exit_code"],
                        message.get("error"),
                    )
                    break
                elif message.get("event") == "internal_error":
                    raise RuntimeError(
                        f"Lua worker implementation error:\n{message['traceback']}"
                    )
                else:
                    try:
                        value = self._rpc(
                            process, message["op"], unpack(message["args"])
                        )
                    except VFSError as exc:
                        response = {"ok": False, "code": exc.code, "message": str(exc)}
                    else:
                        response = {"ok": True, "value": pack(value)}
                    if process.state in TERMINAL:
                        break
                    self._send(process, response)
        except BrokenPipeError, ConnectionResetError:
            if process.state not in TERMINAL:
                self._internal_error(
                    process, RuntimeError("Lua worker pipe closed unexpectedly")
                )
        except Exception as exc:  # noqa: BLE001 -- propagate across the thread boundary
            # A thread cannot raise into its caller: retain the actual exception.
            self._internal_error(process, exc)
        finally:
            with process.condition:
                process.condition.notify_all()
            if process.child.poll() is None:
                try:
                    process.child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._kill(process)
            process.child.wait()
            for stream in (
                process.child.stdin,
                process.child.stdout,
                process.child.stderr,
            ):
                try:
                    stream.close()
                except BrokenPipeError, ConnectionResetError:
                    pass
            process.handles.clear()
            with self._lock:
                self._workers.pop(process.pid, None)

    def _internal_error(self, process, exc):
        with process.condition:
            if process.state not in TERMINAL:
                process.fatal = exc
                self._finish(
                    process,
                    "failed",
                    125,
                    {
                        "code": "internal_error",
                        "message": "Lua execution implementation failed",
                    },
                )
                self._kill(process)

    def _rpc(self, process, op, args):
        handlers = {
            "stdin_read": self._stdin_read,
            "emit": self._emit,
            "open": self._open,
            "read": self._read,
            "write": self._write,
            "seek": self._seek,
            "close": self._close_handle,
            "flush": self._flush,
            "listdir": self._listdir,
            "mkdir": self._mkdir,
            "remove": self._remove,
        }
        if op not in handlers or not isinstance(args, list):
            raise RuntimeError("Invalid Lua worker request")
        return handlers[op](process, *args)

    def _emit(self, process, stream, data):
        stream = text(stream, "stream")
        if stream not in ("stdout", "stderr") or not isinstance(data, bytes):
            raise RuntimeError("Invalid output request")
        with process.condition:
            available = (
                process.limits.output_bytes - len(process.stdout) - len(process.stderr)
            )
            getattr(process, stream).extend(data[:available])
            if len(data) > available:
                self._finish(
                    process,
                    "failed",
                    125,
                    {
                        "code": "output_limit",
                        "message": "Lua output byte limit exceeded",
                    },
                )
                self._kill(process)
        return True

    @staticmethod
    def _read_part(data, position, format, eof=True):
        remaining = data[position:]
        if type(format) is int:
            if format < 0 or format > FILE_BUFFER_LIMIT:
                raise VFSError("invalid_arguments", "Read byte count is out of range")
            if not remaining:
                return (None, position) if eof else (False, position)
            if len(remaining) < format and not eof:
                return False, position
            return remaining[:format], position + min(format, len(remaining))
        format = text(format, "format").removeprefix("*")
        if format == "a":
            return (remaining, len(data)) if eof else (False, position)
        if format not in ("l", "L"):
            raise VFSError(
                "invalid_arguments", "Supported read formats: l, L, a, byte count"
            )
        index = remaining.find(b"\n")
        if index < 0:
            if not eof:
                return False, position
            return (remaining or None), len(data)
        line = remaining[: index + 1]
        if format == "l":
            line = line[:-1].removesuffix(b"\r")
        return line, position + index + 1

    def _stdin_read(self, process, format):
        pending = bytearray()
        with process.condition:
            while process.state not in TERMINAL:
                pending.extend(process.input)
                process.input.clear()
                process.reading_bytes = len(pending)
                if len(pending) > FILE_BUFFER_LIMIT:
                    self._finish(
                        process,
                        "failed",
                        125,
                        {
                            "code": "input_limit",
                            "message": "A single stdin read exceeds 1 MiB",
                        },
                    )
                    self._kill(process)
                    return None
                value, end = self._read_part(pending, 0, format, process.input_eof)
                if value is not False:
                    process.input.extend(pending[end:])
                    process.reading_bytes = 0
                    process.tick()
                    process.state = "running"
                    process.waiting_for = None
                    return bytes(value) if value is not None else None
                process.tick()
                process.state = "waiting_input"
                process.waiting_for = {
                    "format": format if type(format) is int else text(format)
                }
                process.condition.notify_all()
                process.condition.wait()
            return None

    @staticmethod
    def _handle(process, handle):
        if type(handle) is not int or handle not in process.handles:
            raise VFSError("closed_file", "File is already closed")
        return process.handles[handle]

    def _open(self, process, path, mode):
        path = normalize_path(text(path))
        mode = text(mode, "mode").removesuffix("b")
        if mode not in ("r", "w", "a"):
            raise VFSError("invalid_arguments", "Supported modes: r, w, a, rb, wb, ab")
        if len(process.handles) >= process.limits.max_handles:
            raise VFSError("handle_limit", "Too many open VFS files")
        stream = self.vfs.is_stream(path)
        data = b""
        if stream:
            if mode == "r":
                raise VFSError("not_readable", "Endpoint is write-only")
        elif mode == "r":
            data = self.vfs.read_file(path)
            if len(data) > FILE_BUFFER_LIMIT:
                raise VFSError("file_too_large", "Lua file buffer limit is 1 MiB")
        elif mode == "w":
            self.vfs.write_file(path, b"")
        else:
            self.vfs.append_file(path, b"")
        handle = process.next_handle
        process.next_handle += 1
        process.handles[handle] = FileHandle(path, mode, data, stream=stream)
        return handle

    def _read(self, process, handle, format):
        file = self._handle(process, handle)
        if file.mode != "r":
            raise VFSError("not_readable", "File is not open for reading")
        value, file.position = self._read_part(file.data, file.position, format)
        return value

    def _write(self, process, handle, data):
        file = self._handle(process, handle)
        if file.mode == "r":
            raise VFSError("not_writable", "File is not open for writing")
        if not isinstance(data, bytes):
            raise TypeError("Invalid file data")
        if not data:
            return True
        if file.stream:
            self.vfs.write_file(file.path, data)
        elif file.mode == "a":
            file.position = self.vfs.append_file(file.path, data)
        else:
            end = file.position + len(data)
            if end > FILE_BUFFER_LIMIT:
                raise VFSError("file_too_large", "Lua file buffer limit is 1 MiB")
            updated = (
                file.data.ljust(file.position, b"\x00")[: file.position]
                + data
                + file.data[end:]
            )
            self.vfs.write_file(file.path, updated)
            file.data, file.position = updated, end
        return True

    def _seek(self, process, handle, whence, offset):
        file = self._handle(process, handle)
        whence = text(whence, "whence")
        if file.stream:
            raise VFSError("not_seekable", "Endpoint is not seekable")
        if whence not in ("set", "cur", "end") or type(offset) is not int:
            raise VFSError("invalid_arguments", "Invalid seek")
        end = self.vfs.stat(file.path).size if file.mode == "a" else len(file.data)
        position = {"set": 0, "cur": file.position, "end": end}[whence] + offset
        if not 0 <= position <= FILE_BUFFER_LIMIT:
            raise VFSError("invalid_arguments", "Seek is outside the file buffer limit")
        file.position = position
        return position

    def _close_handle(self, process, handle):
        self._handle(process, handle)
        del process.handles[handle]
        return True

    def _flush(self, process, handle):
        self._handle(process, handle)
        return True

    def _listdir(self, process, path):
        from dataclasses import asdict

        return [asdict(entry) for entry in self.vfs.listdir(text(path))]

    def _mkdir(self, process, path):
        self.vfs.mkdir(text(path))
        return True

    def _remove(self, process, path):
        self.vfs.remove(text(path))
        return True

    def close(self):
        self._closed.set()
        with self._lock:
            workers = list(self._workers.values())
        for process in workers:
            self._finish(
                process,
                "cancelled",
                130,
                {"code": "cancelled", "message": "Execution environment closed"},
            )
            self._kill(process)
        for process in workers:
            process.thread.join(timeout=35)
        if self._watcher is not None:
            self._watcher.join(timeout=1)
        with self._lock:
            self._processes.clear()
