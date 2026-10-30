import json
import multiprocessing
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from hello_agent.errors import AppError, ToolError
from hello_agent.shell import ToolShell, read_command
from hello_agent.tools import create_registry
from hello_agent.vfs import (
    Limits,
    SQLiteFileSystem,
    VFSError,
    VirtualFileSystem,
    create_vfs,
)


@pytest.fixture
def fs(tmp_path):
    result = VirtualFileSystem()
    result.mount(
        "/", SQLiteFileSystem(tmp_path / "edit.sqlite3", limits=Limits(128, 3))
    )
    return result


def test_multiple_edits_use_original_and_allow_adjacent_ranges(fs):
    fs.write_file("/main.py", b"DEBUG = False\nTIMEOUT = 10\n")
    registry = create_registry(fs)
    result = json.loads(
        registry.execute(
            "edit_file",
            json.dumps(
                {
                    "path": "/main.py",
                    "edits": [
                        {"old": "TIMEOUT = 10", "new": "TIMEOUT = 30"},
                        {"old": "DEBUG = False", "new": "DEBUG = True"},
                    ],
                }
            ),
        )
    )
    assert fs.read_file("/main.py") == b"DEBUG = True\nTIMEOUT = 30\n"
    assert result == {"status": "success", "size": 26, "edits": 2}
    fs.write_file("/main.py", b"ab")
    fs.edit_file("/main.py", [{"old": "a", "new": "b"}, {"old": "b", "new": "a"}])
    assert fs.read_file("/main.py") == b"ba"


@pytest.mark.parametrize(
    "original, edits, code",
    [
        (
            "abc",
            [{"old": "a", "new": "z"}, {"old": "missing", "new": "x"}],
            "edit_not_found",
        ),
        ("aba", [{"old": "a", "new": "x"}], "edit_ambiguous"),
        ("aaa", [{"old": "aa", "new": "x"}], "edit_ambiguous"),
        (
            "abcdef",
            [{"old": "abc", "new": "x"}, {"old": "cde", "new": "y"}],
            "edit_overlap",
        ),
        (
            "abcdef",
            [{"old": "abc", "new": "x"}, {"old": "abc", "new": "y"}],
            "edit_overlap",
        ),
        (
            "abc",
            [{"old": "a", "new": "new"}, {"old": "new", "new": "z"}],
            "edit_not_found",
        ),
        ("abc", [], "invalid_edits"),
        ("abc", None, "invalid_edits"),
        ("abc", "bad", "invalid_edits"),
        ("abc", [None], "invalid_edits"),
        ("abc", [{"old": "", "new": "x"}], "invalid_edits"),
        ("abc", [{"old": 1, "new": "x"}], "invalid_edits"),
        ("abc", [{"old": "a", "new": 1}], "invalid_edits"),
        ("abc", [{"old": "a"}], "invalid_edits"),
        ("abc", [{"old": "a", "new": "x", "extra": True}], "invalid_edits"),
        ("abc", [{"old": "a", "new": "\ud800"}], "invalid_edits"),
    ],
)
def test_any_failure_leaves_original_and_usage_unchanged(fs, original, edits, code):
    data = original.encode()
    fs.write_file("/file", data)
    before = fs.usage()[1]
    with pytest.raises(VFSError, match=code):
        fs.edit_file("/file", edits)
    assert fs.read_file("/file") == data
    assert fs.usage()[1] == before


def test_unicode_multiline_deletion_and_literal_replacements(fs):
    fs.write_file("/text", "첫째\n둘째\n셋째".encode())
    fs.edit_file(
        "/text", [{"old": "첫째\n둘째\n", "new": ""}, {"old": "셋째", "new": "끝\\n$1"}]
    )
    assert fs.read_file("/text").decode() == "끝\\n$1"


def test_final_byte_size_limit_and_full_volume(fs):
    fs.write_file("/file", b"abc")
    fs.write_file("/other", b"x")  # Volume is full, but editing creates no entries.
    fs.edit_file("/file", [{"old": "abc", "new": "x" * 128}])
    assert fs.stat("/file").size == 128
    with pytest.raises(VFSError, match="file_too_large"):
        fs.edit_file("/file", [{"old": "x" * 128, "new": "가" * 43}])
    assert fs.read_file("/file") == b"x" * 128
    assert fs.usage()[1].entries == 3


def test_missing_directory_binary_and_read_only(fs):
    for path, code in [("/missing", "not_found"), ("/", "is_directory")]:
        with pytest.raises(VFSError, match=code):
            fs.edit_file(path, [{"old": "a", "new": "b"}])
    fs.write_file("/binary", b"\xff")
    with pytest.raises(VFSError, match="not_text"):
        fs.edit_file("/binary", [{"old": "a", "new": "b"}])
    with pytest.raises(ToolError, match="read_only"):
        create_registry().execute(
            "edit_file",
            json.dumps(
                {"path": "/sys/fs/root.json", "edits": [{"old": "x", "new": "y"}]}
            ),
        )


def test_mount_routing_and_persistence(fs, tmp_path):
    path = tmp_path / "nested.sqlite3"
    fs.mount("/nested", SQLiteFileSystem(path))
    fs.write_file("/nested/file", b"old")
    fs.edit_file("/nested/file", [{"old": "old", "new": "new"}])
    assert SQLiteFileSystem(path).read_file("/file") == b"new"


def edit_worker(database, barrier, number):
    fs = SQLiteFileSystem(Path(database))
    barrier.wait(15)
    fs.edit_file("/file", [{"old": f"old-{number}", "new": f"new-{number}"}])


def test_processes_do_not_lose_each_others_edits(tmp_path):
    path = tmp_path / "shared.sqlite3"
    fs = SQLiteFileSystem(path)
    fs.write_file("/file", b"old-0 old-1 old-2 old-3")
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    processes = [
        ctx.Process(target=edit_worker, args=(str(path), barrier, i)) for i in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert fs.read_file("/file") == b"new-0 new-1 new-2 new-3"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_shell_accepts_json_array_argument(monkeypatch):
    fs = create_vfs()
    fs.write_file("/file", b"old text")
    output = StringIO()
    shell = ToolShell(create_registry(fs), Console(file=output))
    monkeypatch.setattr(
        "builtins.input",
        lambda _: """edit_file /file '[{"old":"old text","new":"new text"}]' """,
    )
    shell.execute(read_command())
    assert fs.read_file("/file") == b"new text"
    with pytest.raises(AppError, match="JSON"):
        shell.execute(["edit_file", "/file", "not json"])
    assert fs.read_file("/file") == b"new text"
