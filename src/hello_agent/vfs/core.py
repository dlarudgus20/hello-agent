"""Backend-neutral filesystem API and virtual mount routing."""

from dataclasses import dataclass, replace
from typing import Literal, Protocol

from ..errors import ToolError


class VFSError(ToolError):
    """An expected virtual filesystem failure, safe to return to the model."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 1024 * 1024
    max_entries: int = 512

    def __post_init__(self):
        if any(
            type(n) is not int or n < 1 for n in (self.max_file_bytes, self.max_entries)
        ):
            raise ValueError("VFS limits must be positive integers")


@dataclass(frozen=True)
class Entry:
    name: str
    type: Literal["file", "directory"]
    size: int = 0


@dataclass(frozen=True)
class Usage:
    entries: int
    file_bytes: int | None
    limits: Limits | None


def normalize_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise VFSError(
            "invalid_path", "Use an absolute virtual path without NUL characters"
        )
    try:
        if len(path.encode("utf-8")) > 4096:
            raise VFSError("invalid_path", "Path exceeds 4096 UTF-8 bytes")
        parts = []
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise VFSError("invalid_path", "Path escapes the virtual root")
                parts.pop()
            else:
                if len(part.encode("utf-8")) > 255:
                    raise VFSError("invalid_path", "Name exceeds 255 UTF-8 bytes")
                parts.append(part)
    except UnicodeError as exc:
        raise VFSError("invalid_path", "Path must be valid UTF-8") from exc
    return "/" + "/".join(parts)


class FileSystem(Protocol):
    """Paths are absolute within this backend, independent of its mount point.

    Backends own persistence and enforce their own quotas atomically. They report
    expected failures as VFSError, and must provide a directory at '/'.
    """

    backend_type: str
    read_only: bool

    def stat(self, path: str) -> Entry: ...
    def listdir(self, path: str) -> list[Entry]: ...
    def read_file(self, path: str) -> bytes: ...
    def write_file(self, path: str, content: bytes) -> None: ...
    def append_file(self, path: str, content: bytes) -> int: ...
    def edit_file(self, path: str, edits: list[dict]) -> int: ...
    def mkdir(self, path: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def usage(self) -> Usage: ...


def contains(parent: str, path: str) -> bool:
    return parent == "/" or path == parent or path.startswith(parent + "/")


@dataclass(frozen=True)
class Mount:
    name: str
    path: str
    filesystem: FileSystem


class VirtualFileSystem:
    """Route by the longest matching mount point; no host paths are exposed."""

    def __init__(self):
        self._mounts: dict[str, Mount] = {}
        self.processes = None

    def mount(self, path: str, backend: FileSystem, *, name: str | None = None) -> None:
        path = normalize_path(path)
        if path in self._mounts:
            raise VFSError(
                "already_mounted", f"A filesystem is already mounted at {path!r}"
            )
        if any(contains(path, mount) for mount in self._mounts):
            raise VFSError("busy", "Cannot hide an existing mount beneath a new mount")
        name = name if name is not None else (path.rsplit("/", 1)[-1] or "root")
        if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
            raise VFSError(
                "invalid_mount_name", "Mount name must be one filename component"
            )
        normalize_path("/" + name)
        if len(name.encode("utf-8")) > 250:
            raise VFSError(
                "invalid_mount_name",
                "Mount name exceeds 250 UTF-8 bytes; specify a shorter name",
            )
        if any(mount.name == name for mount in self._mounts.values()):
            raise VFSError(
                "duplicate_mount_name",
                f"Mount name {name!r} is in use; specify an explicit unique name",
            )
        if path != "/":
            parent = path.rsplit("/", 1)[0] or "/"
            if self.stat(parent).type != "directory":
                raise VFSError(
                    "not_directory", f"Mount parent is not a directory: {parent!r}"
                )
            try:
                target = self.stat(path)
            except VFSError as exc:
                if exc.code != "not_found":
                    raise
            else:
                if target.type != "directory":
                    raise VFSError(
                        "not_directory", f"Mount point is not a directory: {path!r}"
                    )
        self._mounts[path] = Mount(name, path, backend)

    def mounts(self) -> tuple[Mount, ...]:
        """An immutable snapshot of mount records, ordered by virtual path."""
        return tuple(self._mounts[path] for path in sorted(self._mounts))

    def _resolve(self, path: str) -> tuple[FileSystem, str, str]:
        path = normalize_path(path)
        candidates = [mount for mount in self._mounts if contains(mount, path)]
        if not candidates:
            raise VFSError("not_mounted", f"No filesystem mounted for {path!r}")
        mount = max(candidates, key=len)
        relative = path if mount == "/" else (path[len(mount) :] or "/")
        return self._mounts[mount].filesystem, relative, mount

    def stat(self, path: str) -> Entry:
        path = normalize_path(path)
        backend, relative, _ = self._resolve(path)
        return replace(backend.stat(relative), name=path.rsplit("/", 1)[-1] or "/")

    def listdir(self, path: str) -> list[Entry]:
        path = normalize_path(path)
        backend, relative, _ = self._resolve(path)
        # Mounted directories may shadow a parent backend's directory metadata.
        entries = {}
        for entry in backend.listdir(relative):
            child = path.rstrip("/") + "/" + entry.name
            entries[entry.name] = self.stat(child) if child in self._mounts else entry
        for mount in self.mounts():
            if mount.path != "/" and (mount.path.rsplit("/", 1)[0] or "/") == path:
                entry = self.stat(mount.path)
                entries[entry.name] = entry
        return [entries[name] for name in sorted(entries)]

    def read_file(self, path: str) -> bytes:
        backend, relative, _ = self._resolve(path)
        return backend.read_file(relative)

    def write_file(self, path: str, content: bytes) -> None:
        backend, relative, _ = self._resolve(path)
        self._writable(backend)
        backend.write_file(relative, content)

    def append_file(self, path: str, content: bytes) -> int:
        backend, relative, _ = self._resolve(path)
        self._writable(backend)
        return backend.append_file(relative, content)

    def is_stream(self, path: str) -> bool:
        """Generated write endpoints do not have normal file truncation semantics."""
        backend, relative, _ = self._resolve(path)
        check = getattr(backend, "is_stream", None)
        return check(relative) if check is not None else False

    def mkdir(self, path: str) -> None:
        backend, relative, _ = self._resolve(path)
        self._writable(backend)
        backend.mkdir(relative)

    def edit_file(self, path: str, edits: list[dict]) -> int:
        backend, relative, _ = self._resolve(path)
        self._writable(backend)
        return backend.edit_file(relative, edits)

    def remove(self, path: str) -> None:
        path = normalize_path(path)
        backend, relative, _ = self._resolve(path)
        self._writable(backend)
        if any(contains(path, mount) for mount in self._mounts):
            raise VFSError("busy", "Cannot remove a mount point or its ancestor")
        backend.remove(relative)

    @staticmethod
    def _writable(backend: FileSystem) -> None:
        if backend.read_only:
            raise VFSError("read_only", "Filesystem is read-only")

    def usage(self, path: str = "/") -> tuple[str, Usage]:
        backend, _, mount = self._resolve(path)
        return mount, backend.usage()
