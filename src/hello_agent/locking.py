"""Reentrant native process locks on stable sidecar files."""

import os
import threading
from contextlib import contextmanager
from functools import lru_cache, wraps
from pathlib import Path

from filelock import FileLock, Timeout

from .errors import AppError, file_errors

_factory_guard = threading.Lock()


@lru_cache(maxsize=128)
def _lock(path: str, pid: int) -> FileLock:
    return FileLock(path, mode=0o600, preserve_lock_file=True, fallback_to_soft=False)


@contextmanager
def file_lock(path: Path, *, timeout: float = 600):
    path = Path(path).absolute()
    with file_errors(path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _factory_guard:
            lock = _lock(str(path), os.getpid())
        try:
            lock.acquire(timeout=timeout)
        except Timeout as exc:
            raise AppError(
                f"다른 hello-agent의 작업을 기다리다 시간이 초과되었습니다: {path.name}"
            ) from exc
    try:
        yield
    finally:
        with file_errors(path):
            lock.release()


def locked(path):
    """Resolve the lock path at call time (also supports isolated test stores)."""

    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            with file_lock(path()):
                return function(*args, **kwargs)

        return wrapper

    return decorate
