"""Generated process snapshots and write-only input/control endpoints."""

import json

from .core import Entry, Usage, VFSError, normalize_path


class ProcessFileSystem:
    backend_type = "procfs"
    read_only = False
    files = ("control", "status", "stderr", "stdin", "stdout")

    def __init__(self, manager):
        self.manager = manager

    def _path(self, path):
        path = normalize_path(path)
        if path == "/":
            return None, None
        parts = path[1:].split("/")
        if (
            not parts[0].isascii()
            or not parts[0].isdigit()
            or str(int(parts[0])) != parts[0]
            or len(parts) > 2
        ):
            raise VFSError("not_found", f"Process path does not exist: {path!r}")
        pid = int(parts[0])
        self.manager.get(pid)
        name = parts[1] if len(parts) == 2 else None
        if name is not None and name not in self.files:
            raise VFSError("not_found", f"Process file does not exist: {path!r}")
        return pid, name

    def stat(self, path):
        pid, name = self._path(path)
        if name is None:
            return Entry(str(pid) if pid is not None else "/", "directory")
        size = 0 if name in ("stdin", "control") else len(self.read_file(path))
        return Entry(name, "file", size)

    def listdir(self, path):
        pid, name = self._path(path)
        if name is not None:
            raise VFSError("not_directory", "Process file is not a directory")
        if pid is None:
            return [Entry(str(pid), "directory") for pid in self.manager.pids()]
        return [self.stat(f"/{pid}/{name}") for name in self.files]

    def read_file(self, path):
        pid, name = self._path(path)
        if name is None:
            raise VFSError("is_directory", "Cannot read a process directory")
        if name in ("stdin", "control"):
            raise VFSError("not_readable", "Process endpoint is write-only")
        if name == "status":
            return (
                json.dumps(self.manager.status(pid), ensure_ascii=False, indent=2)
                + "\n"
            ).encode()
        # Display streams are UTF-8; arbitrary binary output uses replacement chars.
        return self.manager.output(pid, name).decode("utf-8", errors="replace").encode()

    def is_stream(self, path):
        _, name = self._path(path)
        return name in ("stdin", "control")

    def write_file(self, path, content):
        pid, name = self._path(path)
        if name == "stdin":
            self.manager.send(pid, content)
        elif name == "control":
            command = content.removesuffix(b"\n").removesuffix(b"\r")
            if command == b"eof":
                self.manager.send(pid, b"", eof=True)
            elif command == b"cancel":
                self.manager.cancel(pid)
            elif command == b"reap":
                self.manager.reap(pid)
            else:
                raise VFSError("invalid_control", "Commands: eof, cancel, reap")
        else:
            raise VFSError("read_only", "Process metadata is read-only")

    def append_file(self, path, content):
        self.write_file(path, content)
        return len(content)

    def mkdir(self, path):
        raise VFSError("read_only", "Process directories are generated")

    def remove(self, path):
        raise VFSError("read_only", "Use the control endpoint to reap exited processes")

    def edit_file(self, path, edits):
        raise VFSError("read_only", "Process files cannot be edited")

    def usage(self):
        return Usage(1 + (1 + len(self.files)) * len(self.manager.pids()), None, None)
