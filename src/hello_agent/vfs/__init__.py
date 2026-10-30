"""Virtual filesystems and the default persistent root volume."""

from pathlib import Path

from platformdirs import user_data_path

from .core import Entry, FileSystem, Limits, Mount, Usage, VFSError, VirtualFileSystem
from .directory import ReadOnlyDirectoryFileSystem
from .sqlite import SQLiteFileSystem
from .system import SystemFileSystem

__all__ = [
    "Entry",
    "FileSystem",
    "Limits",
    "Mount",
    "ReadOnlyDirectoryFileSystem",
    "SQLiteFileSystem",
    "SystemFileSystem",
    "Usage",
    "VFSError",
    "VirtualFileSystem",
    "create_vfs",
]

VFS_DATABASE = user_data_path("hello-agent", appauthor=False) / "vfs.sqlite3"
DEFAULT_README = b"A sole, lonely existence in this unknown world."


def create_vfs() -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    vfs.mount(
        "/",
        SQLiteFileSystem(VFS_DATABASE, initial_files={"README.txt": DEFAULT_README}),
    )
    vfs.mount("/sys", SystemFileSystem(vfs.mounts))
    vfs.mount(
        "/home", ReadOnlyDirectoryFileSystem(Path(__file__).parent.parent / "bundled")
    )
    from ..processes import ProcessManager

    vfs.processes = ProcessManager(vfs)
    vfs.processes.mount()
    return vfs
