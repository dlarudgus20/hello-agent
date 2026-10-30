"""Standalone isolated worker. Do not import hello_agent or authentication here."""

import base64
import json
import sys
import traceback
from pathlib import Path

from lupa.lua54 import LuaError, LuaMemoryError, LuaRuntime

MAX_MESSAGE = 3 * 1024 * 1024


class ScriptExit(BaseException):
    def __init__(self, code):
        self.code = code


def send(message):
    data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_MESSAGE:
        raise RuntimeError("Worker message is too large")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def receive():
    data = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if not data or not data.endswith(b"\n") or len(data) > MAX_MESSAGE:
        raise RuntimeError("Invalid parent message")
    return json.loads(data)


def pack(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if value is None or type(value) in (int, float, bool):
        return value
    raise TypeError("Unexpected Lua bridge argument")


def deny_attribute(obj, name, setting):
    raise AttributeError("Python object access is disabled")


def main():
    request = receive()
    lua = LuaRuntime(
        encoding=None,
        register_eval=False,
        register_builtins=False,
        attribute_filter=deny_attribute,
        unpack_returned_tuples=True,
        max_memory=request["memory_bytes"],
    )

    def to_lua(value):
        if isinstance(value, dict):
            if set(value) == {"bytes"}:
                return base64.b64decode(value["bytes"], validate=True)
            result = lua.table()
            for key, item in value.items():
                result[key.encode()] = to_lua(item)
            return result
        if isinstance(value, list):
            result = lua.table()
            for index, item in enumerate(value, 1):
                result[index] = to_lua(item)
            return result
        return value.encode("utf-8") if isinstance(value, str) else value

    def host(operation, *args):
        if operation == b"exit":
            raise ScriptExit(args[0])
        if operation == b"emit":
            args = (args[0], args[1][: request["output_bytes"] + 1])
        if operation == b"write" and len(args[1]) > 1024 * 1024:
            return False, b"Lua file buffer limit is 1 MiB", b"file_too_large"
        send({"op": operation.decode("ascii"), "args": [pack(arg) for arg in args]})
        reply = receive()
        if reply["ok"]:
            return True, to_lua(reply.get("value"))
        return False, reply["message"].encode(), reply["code"].encode()

    # Only trusted bootstrap code sees real globals and the Python callback.
    factory = lua.execute(
        Path(__file__).with_name("_lua_env.lua").read_bytes(), mode="t"
    )
    source = base64.b64decode(request["source"]["bytes"], validate=True)
    send({"event": "ready"})
    try:
        factory(
            source,
            ("@" + request["path"]).encode(),
            to_lua(request["args"]),
            host,
            request["path"].encode(),
        )
    except ScriptExit as exc:
        send({"event": "exit", "state": "exited", "exit_code": exc.code})
    except LuaMemoryError:
        send(
            {
                "event": "exit",
                "state": "failed",
                "exit_code": 125,
                "error": {
                    "code": "memory_limit",
                    "message": "Lua memory limit exceeded",
                },
            }
        )
    except LuaError as exc:
        send(
            {
                "event": "exit",
                "state": "failed",
                "exit_code": 1,
                "error": {"code": "lua_error", "message": str(exc)[:8192]},
            }
        )
    else:
        send({"event": "exit", "state": "exited", "exit_code": 0})


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- report the original traceback across IPC
        # IPC boundary only: preserve programming tracebacks for the parent.
        send({"event": "internal_error", "traceback": traceback.format_exc()})
