import json
from hashlib import sha256

import pytest

from hello_agent.errors import ToolError
from hello_agent.tools import create_registry
from hello_agent.vfs import SQLiteFileSystem, VFSError, VirtualFileSystem


@pytest.fixture
def fs(tmp_path):
    fs = VirtualFileSystem()
    fs.mount("/", SQLiteFileSystem(tmp_path / "text.sqlite3"))
    return fs


@pytest.fixture
def execute(fs):
    registry = create_registry(fs)

    def execute(name, **kwargs):
        return json.loads(registry.execute(name, json.dumps(kwargs)))

    return execute


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"", []),
        (b"\n", [""]),
        (b"a\n", ["a"]),
        (b"a\n\n", ["a", ""]),
        (b"a\r\nb\n\r\nend\r", ["a", "b", "", "end\r"]),
        ("  한글\t\n\t공백  \n".encode(), ["  한글\t", "\t공백  "]),
        ("a\v\fb\u2028c\rX".encode(), ["a\v\fb\u2028c\rX"]),
        (b"x" * 100_000, ["x" * 100_000]),
    ],
)
def test_read_line_boundaries_and_snapshot_metadata(fs, execute, data, expected):
    fs.write_file("/text", data)
    result = execute("read_file", path="//./text")
    assert result == {
        "status": "success",
        "path": "/text",
        "file_size_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "total_lines": len(expected),
        "start_line": 1,
        "end_line": len(expected) or None,
        "returned_lines": len(expected),
        "has_more": False,
        "next_start_line": None,
        "lines": [{"line_number": n, "text": t} for n, t in enumerate(expected, 1)],
    }


def test_read_pagination_defaults_eof_and_file_changes(fs, execute, monkeypatch):
    fs.write_file("/text", ("x\n" * 205).encode())
    original = fs.read_file
    reads = []

    def read_once(path):
        reads.append(path)
        data = original(path)
        fs.write_file(path, b"changed")
        return data

    monkeypatch.setattr(fs, "read_file", read_once)
    result = execute("read_file", path="/text")
    assert reads == ["/text"]
    assert result["returned_lines"] == 200
    assert result["total_lines"] == 205
    assert result["has_more"] and result["next_start_line"] == 201
    monkeypatch.setattr(fs, "read_file", original)
    changed = execute("read_file", path="/text")
    assert changed["sha256"] != result["sha256"]
    fs.write_file("/text", b"a\nb\nc\n")
    result = execute("read_file", path="/text", start_line=2, max_lines=1)
    assert result["lines"] == [{"line_number": 2, "text": "b"}]
    assert result["end_line"] == 2 and result["next_start_line"] == 3
    for start in (4, 100):
        result = execute("read_file", path="/text", start_line=start)
        assert result["lines"] == []
        assert result["end_line"] is None and result["next_start_line"] is None
        assert not result["has_more"]


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", None, [], {}])
@pytest.mark.parametrize("field", ["start_line", "max_lines"])
def test_read_invalid_range(execute, field, value):
    with pytest.raises(VFSError, match="invalid_arguments"):
        execute("read_file", path="/missing", **{field: value})


@pytest.mark.parametrize(
    "path, code",
    [("/missing", "not_found"), ("/", "is_directory"), ("/binary", "not_text")],
)
def test_read_expected_errors(fs, execute, path, code):
    fs.write_file("/binary", b"\xff")
    with pytest.raises(VFSError, match=code):
        execute("read_file", path=path)


def test_search_literal_or_order_dedup_and_metadata(fs, execute):
    fs.mkdir("/dir")
    fs.mkdir("/dir/sub")
    fs.write_file("/dir/sub/z", b"DEBUG DEBUG TIMEOUT\nnope\nDebug\n.* [x]\n")
    fs.write_file("/dir/a", "한글 DEBUG\nTIMEOUT\r\n".encode())
    fs.write_file("/outside", b"DEBUG")
    result = execute(
        "search_files",
        paths=["/dir/sub/z", "/dir/", "/dir/sub", "/dir"],
        queries=["DEBUG", "TIMEOUT", ".*", "[x]", "DEBUG"],
    )
    assert result["queries"] == ["DEBUG", "TIMEOUT", ".*", "[x]"]
    assert result["matches"] == [
        {
            "path": "/dir/a",
            "line_number": 1,
            "text": "한글 DEBUG",
            "matched_strings": ["DEBUG"],
        },
        {
            "path": "/dir/a",
            "line_number": 2,
            "text": "TIMEOUT",
            "matched_strings": ["TIMEOUT"],
        },
        {
            "path": "/dir/sub/z",
            "line_number": 1,
            "text": "DEBUG DEBUG TIMEOUT",
            "matched_strings": ["DEBUG", "TIMEOUT"],
        },
        {
            "path": "/dir/sub/z",
            "line_number": 4,
            "text": ".* [x]",
            "matched_strings": [".*", "[x]"],
        },
    ]
    assert result["files_scanned"] == result["files_matched"] == 2
    assert result["total_matches"] == result["returned_matches"] == 4
    assert not result["has_more"] and result["next_offset"] is None
    assert result["skipped_files"] == []


def test_search_recursive_nested_mounts_and_shadowed_files(fs, execute, tmp_path):
    fs.mkdir("/mount")
    fs.write_file("/mount/hidden", b"needle")
    fs.mount("/mount", SQLiteFileSystem(tmp_path / "mount.sqlite3"))
    fs.write_file("/mount/a", b"needle")
    fs.mount("/mount/nested", SQLiteFileSystem(tmp_path / "nested.sqlite3"))
    fs.write_file("/mount/nested/b", b"needle")
    result = execute(
        "search_files", paths=["/", "/mount/", "/mount/nested/b"], queries=["needle"]
    )
    assert [m["path"] for m in result["matches"]] == ["/mount/a", "/mount/nested/b"]
    assert result["files_scanned"] == 2


def test_search_pagination_defaults_and_no_truncation(fs, execute):
    long_line = "x" * 100_000
    fs.write_file("/a", ("needle\n" * 105 + long_line).encode())
    fs.write_file("/b", b"no matches")
    kwargs = {"paths": ["/"], "queries": ["needle", long_line]}
    first = execute("search_files", **kwargs)
    assert first["total_matches"] == 106 and first["returned_matches"] == 100
    assert first["has_more"] and first["next_offset"] == 100
    assert first["files_scanned"] == 2 and first["files_matched"] == 1
    second = execute("search_files", **kwargs, offset=100)
    assert second["returned_matches"] == 6
    assert second["matches"][-1]["text"] == long_line
    assert second["next_offset"] is None and not second["has_more"]
    for offset in (106, 1000):
        empty = execute("search_files", **kwargs, offset=offset, max_results=1)
        assert empty["matches"] == [] and empty["next_offset"] is None
    empty = execute("search_files", paths=["/"], queries=["absent"])
    assert empty["total_matches"] == 0 and empty["files_matched"] == 0
    assert empty["next_offset"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"paths": []},
        {"paths": "/"},
        {"paths": [1]},
        {"paths": ["relative"]},
        {"queries": []},
        {"queries": "x"},
        {"queries": [""]},
        {"queries": [1]},
        {"queries": ["x\ny"]},
        {"queries": ["x\ry"]},
        *(
            {field: value}
            for field in ("max_results", "offset")
            for value in (-1, True, 1.5, "1", None)
        ),
        {"max_results": 0},
    ],
)
def test_search_invalid_arguments(execute, kwargs):
    with pytest.raises(VFSError):
        execute("search_files", **{"paths": ["/"], "queries": ["x"], **kwargs})


def test_explicit_scopes_are_validated_before_search(fs, execute, monkeypatch):
    fs.write_file("/file", b"x")

    def unexpected(path):
        pytest.fail("Read before validating scopes")

    monkeypatch.setattr(fs, "read_file", unexpected)
    with pytest.raises(VFSError, match="not_found"):
        execute("search_files", paths=["/file", "/missing"], queries=["x"])
    with pytest.raises(VFSError, match="not_directory"):
        execute("search_files", paths=["/file/"], queries=["x"])


def test_search_skips_binary_disappeared_and_changed_paths(fs, execute, monkeypatch):
    for path in ("/gone", "/changed"):
        fs.write_file(path, b"x")
    fs.write_file("/binary", b"\xff")
    fs.mkdir("/directory")
    fs.mkdir("/lost-directory")
    original_list = fs.listdir

    def changing_list(path):
        entries = original_list(path)
        if path == "/":
            fs.remove("/gone")
            fs.remove("/changed")
            fs.mkdir("/changed")
            fs.remove("/directory")
            fs.write_file("/directory", b"x")
            fs.remove("/lost-directory")
        return entries

    monkeypatch.setattr(fs, "listdir", changing_list)
    result = execute("search_files", paths=["/"], queries=["x"])
    assert result["matches"] == []
    assert result["skipped_files"] == [
        {"path": "/binary", "reason": "not_text"},
        {"path": "/changed", "reason": "is_directory"},
        {"path": "/directory", "reason": "not_directory"},
        {"path": "/gone", "reason": "not_found"},
        {"path": "/lost-directory", "reason": "not_found"},
    ]


@pytest.mark.parametrize("operation", ["listdir", "read_file"])
@pytest.mark.parametrize(
    "error", [VFSError("storage_error", "disk failure"), RuntimeError("bug")]
)
def test_search_storage_errors_and_bugs_propagate(
    fs, execute, monkeypatch, operation, error
):
    fs.write_file("/file", b"x")

    def fail(path):
        raise error

    monkeypatch.setattr(fs, operation, fail)
    with pytest.raises(type(error), match=str(error)):
        execute("search_files", paths=["/"], queries=["x"])


def test_cat_removed_and_schemas_have_defaults_without_sys_hints(fs):
    registry = create_registry(fs)
    with pytest.raises(ToolError, match="Unknown tool: cat"):
        registry.execute("cat", '{"path":"/"}')
    specs = {s["name"]: s for s in registry.specifications()}
    for name in ("read_file", "search_files"):
        assert "/sys" not in specs[name]["description"]
    read = specs["read_file"]["parameters"]
    assert read["required"] == ["path"]
    assert read["properties"]["start_line"]["default"] == 1
    assert read["properties"]["max_lines"]["default"] == 200
    search = specs["search_files"]["parameters"]
    assert list(search["properties"]) == ["paths", "queries", "max_results", "offset"]
    assert search["required"] == ["paths", "queries"]
