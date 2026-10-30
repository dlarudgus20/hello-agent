"""Read-only host directories, with descriptor-relative, no-follow traversal."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .core import Entry, Usage, VFSError, normalize_path


class ReadOnlyDirectoryFileSystem:
    backend_type = "directory"
    read_only = True

    def __init__(self, directory: Path):
        self.directory = Path(directory).absolute()
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise VFSError(
                "unsupported",
                "Secure directory mounts require no-follow descriptor operations",
            )

    @contextmanager
    def _open(self, path, *, directory=False):
        path = normalize_path(path)
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            current = os.open(self.directory, flags)
            descriptors.append(current)
            parts = path.strip("/").split("/") if path != "/" else []
            for index, part in enumerate(parts):
                candidate = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not (
                    stat.S_ISREG(candidate.st_mode) or stat.S_ISDIR(candidate.st_mode)
                ):
                    raise VFSError(
                        "unsupported_entry",
                        "Only regular files and directories are accessible",
                    )
                final = index == len(parts) - 1
                child_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if not final or directory:
                    child_flags |= os.O_DIRECTORY
                current = os.open(part, child_flags, dir_fd=current)
                descriptors.append(current)
            metadata = os.fstat(current)
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise VFSError(
                    "unsupported_entry",
                    "Only regular files and directories are accessible",
                )
            yield current, metadata
        except OSError as exc:
            code = {
                errno.ENOENT: "not_found",
                errno.ENOTDIR: "not_directory",
                errno.EISDIR: "is_directory",
                errno.ELOOP: "unsupported_entry",
                errno.EACCES: "permission_denied",
                errno.EPERM: "permission_denied",
            }.get(exc.errno, "storage_error")
            # Never include the external path or the raw OSError filename.
            raise VFSError(code, "Cannot access read-only directory entry") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _entry(name, metadata):
        directory = stat.S_ISDIR(metadata.st_mode)
        return Entry(
            name,
            "directory" if directory else "file",
            0 if directory else metadata.st_size,
        )

    def stat(self, path):
        path = normalize_path(path)
        with self._open(path) as (_, metadata):
            return self._entry(path.rsplit("/", 1)[-1] or "/", metadata)

    def listdir(self, path):
        with self._open(path, directory=True) as (descriptor, _):
            entries = []
            for name in sorted(os.listdir(descriptor)):
                try:
                    metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode):
                    entries.append(self._entry(name, metadata))
            return entries

    def read_file(self, path):
        with self._open(path) as (descriptor, metadata):
            if stat.S_ISDIR(metadata.st_mode):
                raise VFSError("is_directory", "Cannot read a directory")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                return stream.read()

    def write_file(self, path, content):
        raise VFSError("read_only", "Directory mount is read-only")

    def append_file(self, path, content):
        raise VFSError("read_only", "Directory mount is read-only")

    def edit_file(self, path, edits):
        raise VFSError("read_only", "Directory mount is read-only")

    def mkdir(self, path):
        raise VFSError("read_only", "Directory mount is read-only")

    def remove(self, path):
        raise VFSError("read_only", "Directory mount is read-only")

    def usage(self):
        entries, file_bytes = 1, 0
        pending = ["/"]
        while pending:
            path = pending.pop()
            for entry in self.listdir(path):
                entries += 1
                if entry.type == "directory":
                    pending.append(path.rstrip("/") + "/" + entry.name)
                else:
                    file_bytes += entry.size
        return Usage(entries, file_bytes, None)
