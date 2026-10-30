"""Read-only generated information about the current virtual mount table."""

import json
from collections.abc import Callable
from dataclasses import asdict

from .core import Entry, Mount, Usage, VFSError, normalize_path


class SystemFileSystem:
    backend_type = "sysfs"
    read_only = True

    def __init__(self, mounts: Callable[[], tuple[Mount, ...]]):
        self._mounts = mounts

    def _files(self) -> dict[str, Mount]:
        return {f"/fs/{mount.name}.json": mount for mount in self._mounts()}

    @staticmethod
    def _content(mount: Mount) -> bytes:
        fs = mount.filesystem
        payload = {
            "mount": mount.path,
            "backend": fs.backend_type,
            "read_only": fs.read_only,
            **asdict(fs.usage()),
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )

    def stat(self, path: str) -> Entry:
        path = normalize_path(path)
        if path in ("/", "/fs"):
            return Entry(path.rsplit("/", 1)[-1] or "/", "directory")
        mount = self._files().get(path)
        if mount is None:
            raise VFSError("not_found", f"System file does not exist: {path!r}")
        return Entry(path.rsplit("/", 1)[-1], "file", len(self._content(mount)))

    def listdir(self, path: str) -> list[Entry]:
        path = normalize_path(path)
        if self.stat(path).type != "directory":
            raise VFSError("not_directory", f"Not a directory: {path!r}")
        if path == "/":
            return [Entry("fs", "directory")]
        return [self.stat(file) for file in sorted(self._files())]

    def read_file(self, path: str) -> bytes:
        path = normalize_path(path)
        if path in ("/", "/fs"):
            raise VFSError("is_directory", f"Cannot read a directory: {path!r}")
        mount = self._files().get(path)
        if mount is None:
            raise VFSError("not_found", f"System file does not exist: {path!r}")
        return self._content(mount)

    def write_file(self, path: str, content: bytes) -> None:
        raise VFSError("read_only", "System filesystem is read-only")

    def append_file(self, path: str, content: bytes) -> int:
        raise VFSError("read_only", "System filesystem is read-only")

    def mkdir(self, path: str) -> None:
        raise VFSError("read_only", "System filesystem is read-only")

    def edit_file(self, path: str, edits: list[dict]) -> int:
        raise VFSError("read_only", "System filesystem is read-only")

    def remove(self, path: str) -> None:
        raise VFSError("read_only", "System filesystem is read-only")

    def usage(self) -> Usage:
        # Do not read generated files here: sys.json itself calls this method.
        return Usage(entries=2 + len(self._files()), file_bytes=None, limits=None)
