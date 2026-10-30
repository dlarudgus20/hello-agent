import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hello_agent.errors import ToolError
from hello_agent.tools import create_registry
from hello_agent.vfs import (
    DEFAULT_README,
    Limits,
    SQLiteFileSystem,
    SystemFileSystem,
    VFSError,
    VirtualFileSystem,
    create_vfs,
)
from hello_agent.vfs import sqlite as sqlite_backend


def info(vfs, name="root"):
    return json.loads(vfs.read_file(f"/sys/fs/{name}.json"))


def test_default_sysfs_and_live_usage():
    vfs = create_vfs()
    assert [e.name for e in vfs.listdir("/")] == ["README.txt", "sys"]
    assert [e.name for e in vfs.listdir("/sys")] == ["fs"]
    assert [e.name for e in vfs.listdir("/sys/fs")] == ["root.json", "sys.json"]
    assert info(vfs) == {
        "mount": "/",
        "backend": "sqlite",
        "read_only": False,
        "entries": 2,
        "file_bytes": len(DEFAULT_README),
        "limits": {"max_file_bytes": 1048576, "max_entries": 512},
    }
    assert info(vfs, "sys") == {
        "mount": "/sys",
        "backend": "sysfs",
        "read_only": True,
        "entries": 4,
        "file_bytes": None,
        "limits": None,
    }
    vfs.mkdir("/notes")
    vfs.write_file("/notes/memo", "가나".encode())
    assert info(vfs)["entries"] == 4
    assert info(vfs)["file_bytes"] == len(DEFAULT_README) + 6
    vfs.write_file("/notes/memo", b"x")
    assert info(vfs)["file_bytes"] == len(DEFAULT_README) + 1
    vfs.remove("/notes/memo")
    assert info(vfs)["entries"] == 3
    assert info(vfs)["file_bytes"] == len(DEFAULT_README)
    assert vfs.stat("/sys/fs/root.json").size == len(vfs.read_file("/sys/fs/root.json"))


def test_new_mounts_and_nested_mounts_are_discoverable(tmp_path):
    vfs = create_vfs()
    snapshot = vfs.mounts()
    notes = SQLiteFileSystem(tmp_path / "notes.sqlite3", limits=Limits(32, 5))
    vfs.mount("/notes", notes)
    vfs.mount("/notes/cache", SQLiteFileSystem(tmp_path / "cache.sqlite3"))
    vfs.write_file("/notes/data", b"abc")
    assert [m.name for m in snapshot] == ["root", "sys"]
    assert [m.path for m in vfs.mounts()] == ["/", "/notes", "/notes/cache", "/sys"]
    assert [e.name for e in vfs.listdir("/sys/fs")] == [
        "cache.json",
        "notes.json",
        "root.json",
        "sys.json",
    ]
    assert info(vfs, "notes")["mount"] == "/notes"
    assert (
        info(vfs, "notes")["entries"] == 2
    )  # Its synthetic cache mount uses no DB entry.
    assert info(vfs, "notes")["file_bytes"] == 3
    assert info(vfs, "notes")["limits"] == {"max_file_bytes": 32, "max_entries": 5}
    assert info(vfs, "cache")["mount"] == "/notes/cache"
    assert info(vfs, "sys")["entries"] == 6
    assert [e.name for e in vfs.listdir("/notes")] == ["cache", "data"]
    assert info(vfs)["entries"] == 2


def test_sysfs_never_discloses_host_database_path(tmp_path):
    vfs = create_vfs()
    vfs.mount("/private", SQLiteFileSystem(tmp_path / "secret-host-name.sqlite3"))
    content = vfs.read_file("/sys/fs/private.json").decode()
    assert "secret-host-name" not in content
    assert str(tmp_path) not in content


@pytest.mark.parametrize(
    "path", ["/sys", "/sys/fs", "/sys/fs/root.json", "/sys/fs/unknown.json", "/sys/new"]
)
@pytest.mark.parametrize("operation", ["write_file", "mkdir", "remove"])
def test_sysfs_all_mutations_are_expected_read_only_errors(path, operation):
    vfs = create_vfs()
    before = info(vfs)
    args = (path, b"change") if operation == "write_file" else (path,)
    with pytest.raises(VFSError, match="read_only"):
        getattr(vfs, operation)(*args)
    assert info(vfs) == before


def test_backend_itself_rejects_mutations_and_invalid_reads():
    vfs = create_vfs()
    system = next(m.filesystem for m in vfs.mounts() if m.name == "sys")
    for operation, args in [
        (system.mkdir, ("/a",)),
        (system.write_file, ("/a", b"x")),
        (system.remove, ("/a",)),
    ]:
        with pytest.raises(VFSError, match="read_only"):
            operation(*args)
    with pytest.raises(VFSError, match="is_directory"):
        system.read_file("/fs")
    for operation in (system.read_file, system.stat, system.listdir):
        with pytest.raises(VFSError, match="not_found"):
            operation("/missing")
    with pytest.raises(VFSError, match="not_directory"):
        system.listdir("/fs/root.json")


def test_mount_name_collision_and_explicit_name(tmp_path):
    vfs = create_vfs()
    backend = SQLiteFileSystem(tmp_path / "another.sqlite3")
    before = vfs.mounts()
    with pytest.raises(VFSError, match="duplicate_mount_name"):
        vfs.mount("/root", backend)
    assert vfs.mounts() == before
    vfs.mount("/root", backend, name="other-root")
    assert info(vfs, "other-root")["mount"] == "/root"
    vfs.mount("/alias", backend, name="alias")
    assert info(vfs, "alias")["entries"] == 1


@pytest.mark.parametrize(
    "name", ["", ".", "..", "a/b", "x" * 251, None, "a\x00b", "\ud800"]
)
def test_invalid_mount_names(tmp_path, name):
    vfs = create_vfs()
    # None is a valid omitted name, so exercise an invalid non-string separately.
    name = 42 if name is None else name
    with pytest.raises(VFSError, match="invalid_mount_name|invalid_path"):
        vfs.mount("/new", SQLiteFileSystem(tmp_path / "new.sqlite3"), name=name)
    assert [m.name for m in vfs.mounts()] == ["root", "sys"]


def test_existing_directory_mount_is_still_supported(tmp_path):
    vfs = create_vfs()
    vfs.mkdir("/notes")
    vfs.write_file("/notes/hidden", b"old")
    vfs.mount("/notes", SQLiteFileSystem(tmp_path / "notes.sqlite3"))
    assert vfs.listdir("/notes") == []
    assert [e.name for e in vfs.listdir("/")] == ["README.txt", "notes", "sys"]
    assert info(vfs)["entries"] == 4  # Physical entries remain in their parent volume.


def test_fs_info_removed_and_sysfs_accessible_through_existing_tools():
    registry = create_registry()
    specs = registry.specifications()
    assert {s["name"] for s in specs} == {
        "ls",
        "read_file",
        "search_files",
        "write_file",
        "edit_file",
        "mkdir",
        "rm",
        "stat",
    }
    with pytest.raises(ToolError, match="Unknown tool"):
        registry.execute("fs_info", "{}")
    entries = json.loads(registry.execute("ls", '{"path":"/sys/fs"}'))["entries"]
    assert [e["name"] for e in entries] == ["root.json", "sys.json"]
    payload = json.loads(registry.execute("read_file", '{"path":"/sys/fs/root.json"}'))
    assert (
        json.loads("\n".join(line["text"] for line in payload["lines"]))["limits"][
            "max_entries"
        ]
        == 512
    )


def test_one_mib_boundary_preserves_file_on_rejection(tmp_path):
    disk = SQLiteFileSystem(tmp_path / "large.sqlite3")
    data = b"x" * 1048576
    disk.write_file("/full", data)
    assert disk.read_file("/full") == data
    with pytest.raises(VFSError, match="file_too_large"):
        disk.write_file("/full", data + b"x")
    with pytest.raises(VFSError, match="file_too_large"):
        disk.write_file("/new", data + b"x")
    assert disk.read_file("/full") == data
    assert disk.usage().entries == 2


def test_default_512_entry_boundary_and_reuse(tmp_path):
    disk = SQLiteFileSystem(tmp_path / "entries.sqlite3")
    disk.mkdir("/dir")
    for i in range(510):
        disk.write_file(f"/dir/{i}", b"")
    assert disk.usage().entries == 512
    with pytest.raises(VFSError, match="entry_limit"):
        disk.mkdir("/overflow")
    with pytest.raises(VFSError, match="entry_limit"):
        disk.write_file("/overflow", b"")
    disk.remove("/dir/0")
    disk.mkdir("/reused")
    assert disk.usage().entries == 512


def test_sysfs_mount_works_when_db_quota_is_full(tmp_path):
    disk = SQLiteFileSystem(tmp_path / "full.sqlite3", limits=Limits(1, 1))
    vfs = VirtualFileSystem()
    vfs.mount("/", disk)
    vfs.mount("/sys", SystemFileSystem(vfs.mounts))
    assert [e.name for e in vfs.listdir("/")] == ["sys"]
    assert info(vfs)["entries"] == 1
    assert disk.listdir("/") == []


def test_metadata_queries_do_not_return_blob_content(tmp_path, monkeypatch):
    disk = SQLiteFileSystem(tmp_path / "metadata.sqlite3")
    disk.write_file("/big", b"x" * 1048576)
    original = sqlite3.Row

    def metadata_row(cursor, values):
        row = original(cursor, values)
        column_names = row.keys()
        assert "data" not in column_names, "Metadata query materialized a file BLOB"
        return row

    monkeypatch.setattr(sqlite_backend.sqlite3, "Row", metadata_row)
    assert disk.stat("/big").size == 1048576
    assert disk.listdir("/")[0].size == 1048576
    assert disk.usage().file_bytes == 1048576


def test_concurrent_default_volume_entry_boundary(tmp_path):
    path = tmp_path / "concurrent-default.sqlite3"
    disk = SQLiteFileSystem(path)
    for i in range(505):
        disk.write_file(f"/seed-{i}", b"")

    def create(i):
        try:
            SQLiteFileSystem(path).write_file(f"/racer-{i}", b"")
        except VFSError as exc:
            assert exc.code == "entry_limit"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(20)))
    assert sum(results) == 6
    assert disk.usage().entries == 512
