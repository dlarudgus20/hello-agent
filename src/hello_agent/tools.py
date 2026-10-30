import inspect
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import text_files
from .errors import ToolError
from .processes import ProcessManager
from .vfs import VFSError, VirtualFileSystem, create_vfs
from .vfs.core import normalize_path


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def specification(self) -> dict:
        """Schema supplied to the OpenAI model."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }

    def execute(self, arguments: str) -> str:
        """Execute a model-requested function call."""
        try:
            kwargs = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolError("Tool arguments must be valid JSON") from exc

        if not isinstance(kwargs, dict):
            raise ToolError("Tool arguments must be an object")

        try:
            inspect.signature(self.handler).bind(**kwargs)
        except TypeError as exc:
            raise ToolError(f"Invalid arguments for tool {self.name}: {exc}") from exc

        # Handler exceptions are bugs unless the handler explicitly raises ToolError.
        result = self.handler(**kwargs)
        return json.dumps(result, ensure_ascii=False)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self.processes: ProcessManager | None = None

    def close(self) -> None:
        if self.processes is not None:
            self.processes.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")

        self._tools[tool.name] = tool

    def specifications(self) -> list[dict]:
        return [tool.specification() for tool in self._tools.values()]

    def execute(self, name: str, arguments: str) -> str:
        tool = self._tools.get(name)

        if tool is None:
            raise ToolError(f"Unknown tool: {name}")

        return tool.execute(arguments)


def create_registry(vfs: VirtualFileSystem | None = None) -> ToolRegistry:
    vfs = vfs if vfs is not None else create_vfs()
    registry = ToolRegistry()
    if vfs.processes is None:
        vfs.processes = ProcessManager(vfs)
    registry.processes = vfs.processes

    def exec(path: str, args: list[str] | None = None, background: bool = False):
        return registry.processes.start(path, args, background)

    def exec_input(
        pid: int, input: str = "", eof: bool = False, background: bool = False
    ):
        return registry.processes.input(pid, input, eof, background)

    def ls(path: str):
        path = normalize_path(path)
        return {
            "status": "success",
            "path": path,
            "entries": [asdict(entry) for entry in vfs.listdir(path)],
        }

    def read_file(path: str, start_line: int = 1, max_lines: int = 200):
        return text_files.read_file(vfs, path, start_line, max_lines)

    def search_files(
        paths: list[str], queries: list[str], max_results: int = 100, offset: int = 0
    ):
        return text_files.search_files(vfs, paths, queries, max_results, offset)

    def write_file(path: str, content: str):
        if not isinstance(content, str):
            raise VFSError("invalid_content", "Content must be UTF-8 text")
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise VFSError("invalid_content", "Content must be valid UTF-8") from exc
        vfs.write_file(path, data)
        return {"status": "success", "size": len(data)}

    def mkdir(path: str):
        vfs.mkdir(path)
        return {"status": "success"}

    def edit_file(path: str, edits: list[dict]):
        size = vfs.edit_file(path, edits)
        return {"status": "success", "size": size, "edits": len(edits)}

    def rm(path: str):
        vfs.remove(path)
        return {"status": "success"}

    def stat(path: str):
        return {"status": "success", **asdict(vfs.stat(path))}

    path_schema = {
        "type": "string",
        "description": "Absolute path in the virtual filesystem (not a host path)",
    }
    background_schema = {
        "type": "boolean",
        "default": False,
        "description": "Return immediately in background; false waits for input or exit and collects an exited process",
    }
    registry.register(
        Tool(
            "exec",
            "Execute a UTF-8 .lua virtual file in an isolated Lua 5.4 process. Foreground returns at input wait or exit; background returns immediately. Lua return values are ignored; os.exit sets the exit code",
            {
                "type": "object",
                "properties": {
                    "path": path_schema,
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "background": background_schema,
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            exec,
        )
    )
    registry.register(
        Tool(
            "exec_input",
            "Send literal stdin to a Lua process and choose foreground or background. No newline is added. Empty input can attach without sending data; EOF permanently closes stdin. Foreground returns at the next input wait or exit",
            {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "minimum": 1},
                    "input": {"type": "string", "default": ""},
                    "eof": {"type": "boolean", "default": False},
                    "background": background_schema,
                },
                "required": ["pid"],
                "additionalProperties": False,
            },
            exec_input,
        )
    )
    for name, description, handler in [
        (
            "ls",
            "List virtual files and directories",
            ls,
        ),
        (
            "read_file",
            "Read a UTF-8 virtual file with 1-based line numbers and range metadata. Lines are not truncated",
            read_file,
        ),
        (
            "search_files",
            "Recursively search UTF-8 virtual files across absolute file/directory paths and mounts. Case-sensitive literal substring OR search; one result per matching line, ordered by path then line. Lines are not truncated; pagination is not a fixed snapshot",
            search_files,
        ),
        (
            "write_file",
            "Create or replace a UTF-8 virtual file. Parent directory must exist; volume quotas apply",
            write_file,
        ),
        (
            "mkdir",
            "Create one virtual directory. Parent must exist; volume entry quota applies",
            mkdir,
        ),
        (
            "edit_file",
            "Atomically edit a UTF-8 virtual file. Each nonempty old must occur exactly once in the original; ranges must not overlap. All edits succeed together or the file is unchanged",
            edit_file,
        ),
        (
            "rm",
            "Remove a virtual file or empty directory. Mount points cannot be removed",
            rm,
        ),
        (
            "stat",
            "Inspect a virtual file or directory, including file size in bytes",
            stat,
        ),
    ]:
        properties = {"path": path_schema}
        required = ["path"]
        if name == "read_file":
            properties.update(
                {
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "First line to read (1-based)",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 200,
                        "description": "Maximum number of lines to return",
                    },
                }
            )
        if name == "search_files":
            properties = {
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": path_schema,
                    "description": "Absolute file or recursive directory scopes; overlapping files are searched once",
                },
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Nonempty single-line literal strings; match any, deduplicated",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 100,
                    "description": "Maximum matching lines in this page",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Matching lines to skip",
                },
            }
            required = ["paths", "queries"]
        if name == "write_file":
            properties["content"] = {
                "type": "string",
                "description": "Complete UTF-8 file contents",
            }
            required.append("content")
        if name == "edit_file":
            properties["edits"] = {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "minLength": 1},
                        "new": {"type": "string"},
                    },
                    "required": ["old", "new"],
                    "additionalProperties": False,
                },
            }
            required.append("edits")
        registry.register(
            Tool(
                name,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                handler,
            )
        )
    return registry
