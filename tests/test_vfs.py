import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from hello_agent.errors import ToolError
from hello_agent.tools import create_registry
from hello_agent.vfs import (
    Limits,
    SQLiteFileSystem,
    VFSError,
    VirtualFileSystem,
    create_vfs,
)
from hello_agent.vfs.core import normalize_path


@pytest.fixture
def disk(tmp_path):
    return SQLiteFileSystem(
        tmp_path / "disk.sqlite3", limits=Limits(max_file_bytes=8, max_entries=5)
    )


@pytest.fixture
def fs(disk):
    vfs = VirtualFileSystem()
    vfs.mount("/", disk)
    return vfs


def test_files_directories_and_reopening(disk):
    assert disk.listdir("/") == []
    disk.mkdir("/notes")
    disk.write_file("/notes/a", b"hello")
    disk.write_file("/binary", b"\x00\xff")
    assert asdict(disk.stat("/notes/a")) == {"name": "a", "type": "file", "size": 5}
    assert [e.name for e in disk.listdir("/")] == ["binary", "notes"]
    assert [e.name for e in disk.listdir("/notes")] == ["a"]
    restored = SQLiteFileSystem(disk.database)
    assert restored.read_file("/notes/a") == b"hello"
    assert restored.read_file("/binary") == b"\x00\xff"
    assert restored.usage() == disk.usage()
    assert restored.usage().entries == 4
    assert restored.usage().file_bytes == 7


def test_size_boundary_and_failed_write_is_atomic(disk):
    disk.write_file("/a", b"12345678")
    assert disk.read_file("/a") == b"12345678"
    for path in ("/a", "/new"):
        with pytest.raises(VFSError, match="file_too_large"):
            disk.write_file(path, b"123456789")
    assert disk.read_file("/a") == b"12345678"
    assert [e.name for e in disk.listdir("/")] == ["a"]
    assert disk.usage().file_bytes == 8


def test_combined_entry_quota_includes_root_and_allows_overwrite(disk):
    disk.mkdir("/d")
    for path in ("/a", "/b", "/d/c"):
        disk.write_file(path, b"")
    assert disk.usage().entries == 5
    for operation in (
        lambda: disk.mkdir("/new"),
        lambda: disk.write_file("/new", b"x"),
    ):
        with pytest.raises(VFSError, match="entry_limit"):
            operation()
    disk.write_file("/a", b"updated")
    assert disk.read_file("/a") == b"updated"
    assert disk.usage().entries == 5
    disk.remove("/d/c")
    disk.mkdir("/new")
    assert disk.usage().entries == 5


def test_root_only_volume(tmp_path):
    disk = SQLiteFileSystem(tmp_path / "root.sqlite3", limits=Limits(1, 1))
    assert disk.stat("/").type == "directory"
    with pytest.raises(VFSError, match="entry_limit"):
        disk.write_file("/a", b"")


@pytest.mark.parametrize(
    "operation, code",
    [
        (lambda d: d.read_file("/"), "is_directory"),
        (lambda d: d.write_file("/", b""), "is_directory"),
        (lambda d: d.mkdir("/"), "already_exists"),
        (lambda d: d.remove("/"), "busy"),
        (lambda d: d.stat("/missing"), "not_found"),
        (lambda d: d.read_file("/missing"), "not_found"),
        (lambda d: d.mkdir("/missing/child"), "not_found"),
        (lambda d: d.write_file("/missing/child", b""), "not_found"),
        (lambda d: d.remove("/missing"), "not_found"),
    ],
)
def test_expected_filesystem_errors(disk, operation, code):
    with pytest.raises(VFSError) as exc:
        operation(disk)
    assert exc.value.code == code


def test_file_as_parent_and_nonempty_directory(disk):
    disk.write_file("/file", b"x")
    for operation in (disk.listdir, disk.mkdir):
        with pytest.raises(VFSError, match="not_directory|already_exists"):
            operation("/file")
    with pytest.raises(VFSError, match="not_directory"):
        disk.write_file("/file/child", b"x")
    disk.mkdir("/d")
    disk.write_file("/d/child", b"yes")
    with pytest.raises(VFSError, match="not_empty"):
        disk.remove("/d")
    assert disk.read_file("/d/child") == b"yes"
    disk.remove("/d/child")
    disk.remove("/d")
    assert [x.name for x in disk.listdir("/")] == ["file"]


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "",
        None,
        42,
        [],
        "/bad\0name",
        "/..",
        "/a/../../b",
        "/" + "x" * 256,
        "/" + "x/" * 2048,
        "/\ud800",
    ],
)
def test_invalid_paths(disk, path):
    with pytest.raises(VFSError, match="invalid_path"):
        disk.write_file(path, b"x")
    assert disk.listdir("/") == []


def test_normalized_paths(fs):
    fs.mkdir("/d")
    fs.write_file("//d/./a/../b", b"x")
    assert fs.read_file("/d/b/") == b"x"
    assert normalize_path("/d/../") == "/"


def test_mount_longest_prefix_isolation_and_protected_ancestors(fs, disk, tmp_path):
    fs.mkdir("/mnt")
    fs.mkdir("/mnt/nested")
    disk.write_file("/mnt/hidden", b"parent")
    other = SQLiteFileSystem(tmp_path / "other.sqlite3")
    other.mkdir("/nested")
    fs.mount("/mnt", other)
    nested = SQLiteFileSystem(tmp_path / "nested.sqlite3")
    fs.mount("/mnt/nested", nested)
    fs.write_file("/mnt/a", b"other")
    fs.write_file("/mnt/nested/b", b"nested")
    assert other.read_file("/a") == b"other"
    assert nested.read_file("/b") == b"nested"
    assert disk.read_file("/mnt/hidden") == b"parent"
    assert [e.name for e in fs.listdir("/mnt")] == ["a", "nested"]
    assert fs.stat("/mnt").name == "mnt"
    assert fs.stat("/mnt/nested").type == "directory"
    assert fs.usage("/mnt/nested/b")[0] == "/mnt/nested"
    with pytest.raises(VFSError, match="not_found"):
        fs.read_file("/mnt/hidden")
    # '/mnt2' must not accidentally route to '/mnt'.
    with pytest.raises(VFSError, match="not_found"):
        fs.read_file("/mnt2/a")
    for path in ("/", "/mnt", "/mnt/nested"):
        with pytest.raises(VFSError, match="busy"):
            fs.remove(path)


def test_mount_validation(fs, tmp_path):
    other = SQLiteFileSystem(tmp_path / "other.sqlite3")
    with pytest.raises(VFSError, match="already_mounted"):
        fs.mount("//", other)
    with pytest.raises(VFSError, match="not_found"):
        fs.mount("/missing/child", other)
    fs.write_file("/file", b"")
    with pytest.raises(VFSError, match="not_directory"):
        fs.mount("/file", other)
    fs.mkdir("/a")
    fs.mkdir("/a/b")
    fs.mount("/a/b", other)
    with pytest.raises(VFSError, match="busy"):
        fs.mount("/a", other)


def test_unmounted_paths():
    fs = VirtualFileSystem()
    with pytest.raises(VFSError, match="not_mounted"):
        fs.listdir("/")


def test_alternative_backend_mount_uses_backend_relative_paths(fs):
    class ReadOnlyBackend:
        backend_type = "test"
        read_only = True

        def stat(self, path):
            from hello_agent.vfs import Entry

            return Entry("/", "directory")

        def read_file(self, path):
            assert path == "/message"
            return b"alternative"

    fs.mkdir("/custom")
    fs.mount("/custom", ReadOnlyBackend())
    assert fs.read_file("/custom/message") == b"alternative"


def test_concurrent_creations_cannot_exceed_quota(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    disk = SQLiteFileSystem(path, limits=Limits(8, 10))
    disk.usage()

    def create(i):
        try:
            SQLiteFileSystem(path).write_file(f"/{i}", b"x")
        except VFSError as exc:
            assert exc.code == "entry_limit"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(24)))
    assert sum(results) == 9
    assert disk.usage().entries == 10
    assert disk.usage().file_bytes == 9


def test_persisted_limits_cannot_silently_change(disk):
    disk.usage()
    with pytest.raises(VFSError, match="limits_mismatch"):
        SQLiteFileSystem(disk.database, limits=Limits(9, 10)).write_file("/bad", b"x")
    assert disk.listdir("/") == []


def test_storage_corruption_is_expected(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a database")
    with pytest.raises(VFSError, match="storage_error"):
        SQLiteFileSystem(path).listdir("/")
    assert path.read_bytes() == b"not a database"


def test_sql_bugs_are_not_hidden(disk, monkeypatch):
    def bad_query(db, _):
        db.execute("SELECT * FROM nonexistent_table")

    monkeypatch.setattr(disk, "_row", bad_query)
    with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
        disk.stat("/")


def test_single_storage_file_and_no_host_file_access(fs, disk, tmp_path):
    host = tmp_path / "private.txt"
    host.write_text("host secret")
    with pytest.raises(VFSError, match="not_found"):
        fs.read_file(str(host))
    fs.mkdir("/notes")
    fs.write_file("/notes/virtual.txt", b"virtual")
    assert not (tmp_path / "notes").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["disk.sqlite3", "private.txt"]


def test_tools_write_read_usage_and_new_registry(fs, disk):
    registry = create_registry(fs)
    execute = lambda name, **kwargs: json.loads(
        registry.execute(name, json.dumps(kwargs))
    )
    execute("mkdir", path="/문서")
    assert execute("write_file", path="/문서/글", content="가나")["size"] == 6
    assert execute("read_file", path="/문서/글")["lines"][0]["text"] == "가나"
    with pytest.raises(ToolError, match="file_too_large"):
        execute("write_file", path="/문서/글", content="가나다")
    assert execute("stat", path="/문서/글")["size"] == 6
    assert execute("ls", path="/문서")["entries"][0]["type"] == "file"
    restored = VirtualFileSystem()
    restored.mount("/", SQLiteFileSystem(disk.database))
    assert (
        json.loads(
            create_registry(restored).execute("read_file", '{"path":"/문서/글"}')
        )["lines"][0]["text"]
        == "가나"
    )
    execute("rm", path="/문서/글")
    execute("rm", path="/문서")
    assert execute("ls", path="/")["entries"] == []


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("read_file", {"path": []}),
        ("write_file", {"path": "/a", "content": 3}),
        ("write_file", {"path": "/a", "content": "\ud800"}),
    ],
)
def test_bad_model_arguments_are_tool_errors(fs, name, kwargs):
    with pytest.raises(ToolError):
        create_registry(fs).execute(name, json.dumps(kwargs))


def test_binary_file_read_is_tool_error(fs):
    fs.write_file("/binary", b"\xff")
    with pytest.raises(ToolError, match="not_text"):
        create_registry(fs).execute("read_file", '{"path":"/binary"}')


def test_default_volume_persists_between_registries():
    first = create_registry()
    first.execute("write_file", '{"path":"/saved","content":"hello"}')
    second = create_registry()
    assert (
        json.loads(second.execute("read_file", '{"path":"/saved"}'))["lines"][0]["text"]
        == "hello"
    )
    assert create_vfs().usage()[1].limits == Limits()


def test_initial_limits_survive_rejected_first_write(tmp_path):
    path = tmp_path / "first.sqlite3"
    disk = SQLiteFileSystem(path, limits=Limits(2, 3))
    with pytest.raises(VFSError, match="file_too_large"):
        disk.write_file("/bad", b"too large")
    assert SQLiteFileSystem(path).usage().limits == Limits(2, 3)
    assert SQLiteFileSystem(path).usage().entries == 1


def test_new_default_readme_and_user_changes_persist():
    from hello_agent.vfs import DEFAULT_README

    fs = create_vfs()
    assert fs.read_file("/README.txt") == DEFAULT_README
    fs.write_file("/README.txt", b"user edited")
    assert create_vfs().read_file("/README.txt") == b"user edited"
    fs.remove("/README.txt")
    assert [e.name for e in create_vfs().listdir("/")] == ["home", "sys"]


def test_default_bootstrap_does_not_modify_existing_db():
    from hello_agent.vfs import VFS_DATABASE

    existing = SQLiteFileSystem(VFS_DATABASE)
    existing.write_file("/mine", b"keep")
    fs = create_vfs()
    assert [e.name for e in fs.listdir("/")] == ["home", "mine", "sys"]
    assert fs.read_file("/mine") == b"keep"


@pytest.mark.parametrize(
    "limits, content, code",
    [(Limits(1, 2), b"xx", "file_too_large"), (Limits(8, 1), b"x", "entry_limit")],
)
def test_initial_files_obey_limits(tmp_path, limits, content, code):
    disk = SQLiteFileSystem(
        tmp_path / "initial.sqlite3",
        limits=limits,
        initial_files={"README.txt": content},
    )
    with pytest.raises(VFSError, match=code):
        disk.stat("/")
