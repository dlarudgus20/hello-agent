import pytest

from hello_agent import auth, history, settings, vfs


@pytest.fixture(autouse=True)
def isolated_default_vfs(tmp_path, monkeypatch):
    # Agent tests must never create or modify the user's persistent virtual disk.
    monkeypatch.setattr(vfs, "VFS_DATABASE", tmp_path / "default-vfs.sqlite3")


@pytest.fixture(autouse=True)
def isolated_shared_files(tmp_path, monkeypatch):
    for module in (auth, settings):
        monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(auth, "HOST_FILE", tmp_path / "config/host-id")
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "config/settings.json")
    monkeypatch.setattr(history, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "data/history.json")
