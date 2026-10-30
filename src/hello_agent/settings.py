import json
import os
import tempfile
from pathlib import Path

from platformdirs import user_config_path

from .errors import AppError, file_errors
from .locking import file_lock, locked

CONFIG_DIR = user_config_path("hello-agent", appauthor=False)
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULT_COMPACT_THRESHOLD = 128_000
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def transaction():
    return file_lock(SETTINGS_FILE.with_suffix(".lock"))


@locked(lambda: SETTINGS_FILE.with_suffix(".lock"))
def load_settings() -> dict:
    with file_errors(SETTINGS_FILE):
        if not SETTINGS_FILE.exists():
            return {}

        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise AppError(f"설정 파일은 JSON 객체여야 합니다: {SETTINGS_FILE}")
        return data


@locked(lambda: SETTINGS_FILE.with_suffix(".lock"))
def save_settings(settings: dict) -> None:
    with file_errors(SETTINGS_FILE):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR)

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp, SETTINGS_FILE)
        finally:
            Path(tmp).unlink(missing_ok=True)


def get_model() -> str | None:
    model = load_settings().get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise AppError(f"설정 파일의 model 값이 잘못되었습니다: {SETTINGS_FILE}")
    return model


@locked(lambda: SETTINGS_FILE.with_suffix(".lock"))
def set_model(model: str, reasoning_effort: str | None = None) -> None:
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"Invalid reasoning effort: {reasoning_effort}")
    settings = load_settings()
    settings["model"] = model
    if reasoning_effort is None:
        settings.pop("reasoning_effort", None)
    else:
        settings["reasoning_effort"] = reasoning_effort
    save_settings(settings)


def get_reasoning_effort() -> str | None:
    value = load_settings().get("reasoning_effort")
    if value is not None and value not in REASONING_EFFORTS:
        raise AppError(
            f"설정 파일의 reasoning_effort 값이 잘못되었습니다: {SETTINGS_FILE}"
        )
    return value


def get_compact_threshold() -> int:
    value = load_settings().get("compact_threshold", DEFAULT_COMPACT_THRESHOLD)
    if type(value) is not int or value <= 0:
        raise AppError(
            f"설정 파일의 compact_threshold는 양의 정수여야 합니다: {SETTINGS_FILE}"
        )
    return value


@locked(lambda: SETTINGS_FILE.with_suffix(".lock"))
def set_compact_threshold(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("compact_threshold must be a positive integer")
    settings = load_settings()
    settings["compact_threshold"] = value
    save_settings(settings)
