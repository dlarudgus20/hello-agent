import json
import os
from pathlib import Path

import pytest

from hello_agent.tools import create_registry
from hello_agent.vfs import ReadOnlyDirectoryFileSystem, VFSError, create_vfs


def test_bundled_readme_and_usage():
    fs = create_vfs()
    data = fs.read_file("/home/README.md")
    assert data == (Path(__file__).parents[1] / "README.md").read_bytes()
    assert [entry.name for entry in fs.listdir("/home")] == ["README.md"]
    assert fs.stat("/home/README.md").size == len(data)
    info = json.loads(fs.read_file("/sys/fs/home.json"))
    assert info == {
        "mount": "/home",
        "backend": "directory",
        "read_only": True,
        "entries": 2,
        "file_bytes": len(data),
        "limits": None,
    }
    assert fs.usage("/")[1].entries == 2


def test_external_directory_live_reads_search_and_errors(tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub/a").write_text("한글 needle\n")
    fs = create_vfs()
    fs.mount("/docs", ReadOnlyDirectoryFileSystem(root))
    assert fs.read_file("/docs/sub/a") == "한글 needle\n".encode()
    (root / "sub/a").write_bytes(b"updated needle")
    assert fs.read_file("/docs/sub/a") == b"updated needle"
    with create_registry(fs) as registry:
        found = json.loads(
            registry.execute("search_files", '{"paths":["/docs"],"queries":["needle"]}')
        )
    assert found["matches"][0]["path"] == "/docs/sub/a"
    assert fs.usage("/docs")[1].entries == 3
    for path, code in (
        ("/docs/missing", "not_found"),
        ("/docs/sub/a/no", "not_directory"),
        ("/docs", "is_directory"),
    ):
        with pytest.raises(VFSError, match=code) as error:
            fs.read_file(path)
        assert str(root) not in str(error.value)
    with pytest.raises(VFSError, match="not_directory"):
        fs.listdir("/docs/sub/a")


@pytest.mark.parametrize(
    "operation,args",
    [
        ("write_file", ("/home/README.md", b"bad")),
        ("append_file", ("/home/README.md", b"bad")),
        ("edit_file", ("/home/README.md", [{"old": "hello", "new": "bad"}])),
        ("mkdir", ("/home/new",)),
        ("remove", ("/home/README.md",)),
    ],
)
def test_all_mutations_rejected_in_router_and_backend(operation, args):
    fs = create_vfs()
    backend = next(m.filesystem for m in fs.mounts() if m.path == "/home")
    for target in (fs, backend):
        with pytest.raises(VFSError, match="read_only"):
            getattr(target, operation)(*args)


def test_symlinks_special_files_and_traversal_are_blocked(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    (root / "link").symlink_to(secret)
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / "fifo")
    fs = ReadOnlyDirectoryFileSystem(root)
    assert fs.listdir("/") == []
    assert fs.usage().entries == 1
    for path in ("/link", "/outside/secret", "/fifo"):
        with pytest.raises(VFSError, match="unsupported_entry"):
            fs.read_file(path)
    with pytest.raises(VFSError, match="invalid_path"):
        fs.read_file("/../secret")
    with pytest.raises(VFSError, match="not_found"):
        fs.read_file("/sub/../secret")
