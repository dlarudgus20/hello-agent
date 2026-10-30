"""Expected operational failures and their terminal presentation."""

import json
from contextlib import contextmanager

import jwt
import keyring.errors
import requests
from authlib.integrations.base_client.errors import OAuthError
from authlib.oauth2 import OAuth2Error
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
from rich.console import Console
from rich.text import Text


class AppError(RuntimeError):
    """An anticipated failure that can be explained without a traceback."""


class ToolError(AppError):
    """Invalid model arguments or an explicitly reported tool failure."""


EXPECTED_ERRORS = (
    AppError,
    APIError,
    requests.RequestException,
    keyring.errors.KeyringError,
    jwt.PyJWTError,
    OAuthError,
    OAuth2Error,
)


@contextmanager
def file_errors(path):
    """Translate external file failures only, leaving programming errors visible."""
    try:
        yield
    except json.JSONDecodeError as exc:
        raise AppError(f"JSON 파일이 손상되었습니다: {path} (줄 {exc.lineno})") from exc
    except UnicodeError as exc:
        raise AppError(f"UTF-8 파일을 읽을 수 없습니다: {path}") from exc
    except OSError as exc:
        raise AppError(
            f"파일 작업에 실패했습니다: {path} ({exc.strerror or exc})"
        ) from exc


def _http_message(status: int) -> str:
    if status in (401, 403):
        return f"현재 인증정보로 이 작업을 수행할 수 없습니다 (HTTP {status}). 로그인 상태와 계정 권한을 확인하세요."
    if status == 429:
        return "요청 또는 사용량 한도에 도달했습니다 (HTTP 429). 잠시 후 한도를 확인하고 다시 시도하세요."
    if status >= 500:
        return f"서버 오류가 발생했습니다 (HTTP {status}). 잠시 후 다시 시도하세요."
    return f"서버가 요청을 거부했습니다 (HTTP {status})."


def error_message(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return str(exc)
    if isinstance(exc, APITimeoutError | requests.Timeout):
        return "서버 응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요."
    if isinstance(exc, APIConnectionError | requests.ConnectionError):
        return "서버에 연결할 수 없습니다. 네트워크 연결을 확인하세요."
    if isinstance(exc, APIStatusError):
        message = _http_message(exc.status_code)
        if isinstance(exc.body, dict) and isinstance(exc.body.get("message"), str):
            message += f"\n서버 메시지: {exc.body['message']}"
        return message
    if isinstance(exc, APIError):
        return f"API 응답을 처리하지 못했습니다: {exc.message}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return _http_message(exc.response.status_code)
    if isinstance(exc, requests.RequestException):
        return "HTTP 요청을 처리하지 못했습니다. 네트워크와 서버 응답을 확인하세요."
    if isinstance(exc, keyring.errors.KeyringError):
        return "시스템 인증정보 저장소에 접근하지 못했습니다. keyring 설정과 잠금 상태를 확인하세요."
    if isinstance(exc, jwt.PyJWTError | OAuthError | OAuth2Error):
        return "로그인 인증을 검증하지 못했습니다. hello-agent login으로 다시 로그인하세요."
    raise TypeError(f"Unhandled error type: {type(exc).__name__}")


def report_error(exc: BaseException) -> None:
    Console(stderr=True).print(Text(f"오류: {error_message(exc)}", style="red"))
