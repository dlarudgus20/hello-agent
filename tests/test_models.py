import sys
from types import SimpleNamespace

import pytest

from hello_agent import cli, settings
from hello_agent.history import HistoryState


@pytest.fixture
def model_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    models = [
        {
            "slug": "first",
            "display_name": "First Model",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": value} for value in ("low", "medium", "high", "max")
            ],
        },
        {
            "slug": "second",
            "display_name": "Second Model",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "medium"}],
        },
    ]
    monkeypatch.setattr(cli.api, "list_models", lambda: models)
    return models


@pytest.mark.parametrize("option", ["--reasoning", "--reasoning-effort", "-r"])
def test_model_set_persists_effort(model_settings, monkeypatch, capsys, option):
    settings.set_compact_threshold(64000)
    monkeypatch.setattr(
        sys, "argv", ["hello-agent", "model", "set", "first", option, "high"]
    )
    assert cli.main() == 0
    assert settings.load_settings() == {
        "model": "first",
        "reasoning_effort": "high",
        "compact_threshold": 64000,
    }
    assert settings.get_reasoning_effort() == "high"
    assert "추론: high" in capsys.readouterr().out


@pytest.mark.parametrize("model", ["first", "second"])
def test_omitted_effort_resets_to_model_default(model_settings, monkeypatch, model):
    settings.set_model("first", "high")
    monkeypatch.setattr(sys, "argv", ["hello-agent", "model", "set", model])
    assert cli.main() == 0
    assert settings.get_model() == model
    assert settings.get_reasoning_effort() is None
    assert "reasoning_effort" not in settings.load_settings()


@pytest.mark.parametrize("model, effort", [("second", "high"), ("missing", "high")])
def test_invalid_selection_preserves_settings(
    model_settings, monkeypatch, capsys, model, effort
):
    settings.set_model("first", "low")
    before = settings.SETTINGS_FILE.read_bytes()
    monkeypatch.setattr(
        sys, "argv", ["hello-agent", "model", "set", model, "-r", effort]
    )
    assert cli.main() == 1
    assert settings.SETTINGS_FILE.read_bytes() == before
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err


def test_invalid_effort_rejected_by_parser_without_api(model_settings, monkeypatch):
    def unexpected():
        pytest.fail("No API lookup for invalid CLI input")

    monkeypatch.setattr(cli.api, "list_models", unexpected)
    monkeypatch.setattr(
        sys, "argv", ["hello-agent", "model", "set", "first", "-r", "typo"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "effort, expected", [("high", "추론: high"), (None, "추론: 기본값")]
)
def test_ls_only_annotates_selected_model(
    model_settings, monkeypatch, capsys, effort, expected
):
    settings.set_model("first", effort)
    monkeypatch.setattr(sys, "argv", ["hello-agent", "model", "ls"])
    assert cli.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("* first")
    assert expected in lines[0]
    assert "추론:" not in lines[1]


def test_ls_without_selected_model(model_settings, capsys):
    cli.list_models()
    assert "추론:" not in capsys.readouterr().out


def test_show_displays_effort_without_api(model_settings, monkeypatch, capsys):
    settings.set_model("first", "max")
    monkeypatch.setattr(
        cli.api, "list_models", lambda: pytest.fail("No network for show")
    )
    cli.show_model()
    assert capsys.readouterr().out == "first · 추론: max\n"


def test_create_agent_restores_effort(model_settings, monkeypatch):
    settings.set_model("first", "high")
    client = SimpleNamespace()
    monkeypatch.setattr(cli.api, "create_client", lambda: client)
    monkeypatch.setattr(cli.HistoryStore, "load", lambda _: HistoryState())
    agent = cli.create_agent()
    assert agent.client is client
    assert agent.model == "first"
    assert agent.reasoning_effort == "high"


@pytest.mark.parametrize("value", ["typo", 1, [], {}])
def test_corrupt_effort_is_expected_error(model_settings, monkeypatch, capsys, value):
    settings.save_settings({"model": "first", "reasoning_effort": value})
    monkeypatch.setattr(sys, "argv", ["hello-agent", "model", "show"])
    assert cli.main() == 1
    assert "reasoning_effort" in capsys.readouterr().err
