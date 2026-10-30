import json
import os
import secrets
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import keyring
import requests
from authlib.integrations.requests_client import OAuth2Session

from .errors import AppError, file_errors
from .locking import locked

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"

SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"

APP_NAME = "hello-agent"
KEYRING_SERVICE = "hello-agent"
CONFIG_DIR = Path.home() / ".config" / "hello-agent"
HOST_FILE = CONFIG_DIR / "host-id"


@locked(lambda: HOST_FILE.with_suffix(".lock"))
def get_host_id():
    with file_errors(HOST_FILE):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.chmod(0o700)

        if HOST_FILE.exists():
            return HOST_FILE.read_text().strip()

        host_id = f"urn:uuid:{uuid.uuid4()}"
        fd, temporary = tempfile.mkstemp(dir=CONFIG_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(host_id)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, HOST_FILE)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return host_id


@locked(lambda: CONFIG_DIR / "oauth.lock")
def load_credentials():
    raw = keyring.get_password(KEYRING_SERVICE, "oauth")
    if not raw:
        return None
    try:
        credentials = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            "저장된 인증정보가 손상되었습니다. 인증정보 저장소를 확인하세요."
        ) from exc
    required = ("client_id", "subject", "id_token", "access_token", "refresh_token")
    if (
        not isinstance(credentials, dict)
        or any(not isinstance(credentials.get(key), str) for key in required)
        or not isinstance(credentials.get("expires_at"), int | float)
        or not isinstance(credentials.get("scopes"), list)
    ):
        raise AppError(
            "저장된 인증정보 형식이 잘못되었습니다. 인증정보 저장소를 확인하세요."
        )
    return credentials


@locked(lambda: CONFIG_DIR / "oauth.lock")
def save_credentials(credentials):
    keyring.set_password(
        KEYRING_SERVICE,
        "oauth",
        json.dumps(credentials),
    )


def create_callback_server():
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)

            if parsed.path != "/auth/callback":
                self.send_error(404)
                return

            result.update(
                {
                    key: values[0]
                    for key, values in parse_qs(
                        parsed.query,
                        keep_blank_values=True,
                    ).items()
                }
            )

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Login callback received. Return to terminal.")

        def log_message(self, format, *args):
            # Callback URL may contain an authorization code.
            pass

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        raise AppError(
            "로그인 콜백 서버를 열 수 없습니다. 로컬 네트워크 권한을 확인하세요."
        ) from exc
    server.timeout = 1

    return server, result


@locked(lambda: CONFIG_DIR / "oauth.lock")
def login():
    existing = load_credentials()
    host_id = get_host_id()

    client_id = existing["client_id"] if existing else "dynamic_agent_client"

    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)

    server, callback = create_callback_server()
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"

    oauth = OAuth2Session(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        code_challenge_method="S256",
        token_endpoint_auth_method="none",
    )

    extra = {
        "resource": RESOURCE,
        "ext_agent_host_id": host_id,
        "nonce": nonce,
    }

    if existing:
        extra["id_token_hint"] = existing["id_token"]
        if existing.get("email"):
            extra["login_hint"] = existing["email"]
    else:
        extra["agent_name_hint"] = APP_NAME

    authorization_url, state = oauth.create_authorization_url(
        AUTHORIZE_URL,
        code_verifier=verifier,
        **extra,
    )

    print("Opening ChatGPT login...")
    webbrowser.open(authorization_url)

    try:
        deadline = time.monotonic() + 180

        while not callback and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not callback:
        raise AppError("Login timed out")

    if not secrets.compare_digest(
        callback.get("state", ""),
        state,
    ):
        raise AppError("OAuth state mismatch")

    if "error" in callback:
        raise AppError(f"Authorization failed: {callback['error']}")

    code = callback.get("code")
    if not code:
        raise AppError("Missing authorization code")

    returned_id = callback.get("client_id")

    if existing:
        if returned_id and returned_id != client_id:
            raise AppError("Client ID mismatch")
    else:
        if not returned_id:
            raise AppError("No issued client ID")
        client_id = returned_id

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
        timeout=30,
    )
    response.raise_for_status()
    tokens = response.json()

    claims = validate_id_token(
        tokens["id_token"],
        client_id,
        nonce,
    )

    if existing and claims["sub"] != existing["subject"]:
        raise AppError("Different account selected")

    scopes = tokens.get("scope", "").split()

    if "chatgpt.tokens.use.direct" not in scopes:
        raise AppError("ChatGPT plan usage permission was not granted")

    credentials = {
        "client_id": client_id,
        "subject": claims["sub"],
        "email": claims.get("email"),
        "id_token": tokens["id_token"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "scopes": scopes,
        "expires_at": time.time() + tokens["expires_in"],
    }

    save_credentials(credentials)
    print("Signed in:", credentials["email"] or claims["sub"])


def validate_id_token(id_token, client_id, nonce):
    discovery = requests.get(
        f"{ISSUER}/.well-known/openid-configuration",
        timeout=15,
    )
    discovery.raise_for_status()
    metadata = discovery.json()

    if metadata["issuer"] != ISSUER:
        raise AppError("Unexpected token issuer")

    jwks = jwt.PyJWKClient(metadata["jwks_uri"])
    signing_key = jwks.get_signing_key_from_jwt(id_token)

    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=ISSUER,
        options={
            "require": ["sub", "iss", "aud", "exp", "nonce"],
        },
    )

    if not secrets.compare_digest(
        str(claims["nonce"]),
        nonce,
    ):
        raise AppError("Invalid ID token nonce")

    return claims


@locked(lambda: CONFIG_DIR / "oauth.lock")
def get_access_token():
    credentials = load_credentials()

    if credentials is None:
        raise AppError("Not signed in. Run: hello-agent login")

    # Return an existing token if still valid.
    if time.time() < credentials["expires_at"] - 60:
        return credentials["access_token"]

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": credentials["client_id"],
            "refresh_token": credentials["refresh_token"],
            "resource": RESOURCE,
        },
        timeout=30,
    )
    response.raise_for_status()
    tokens = response.json()

    new_scopes = tokens.get(
        "scope",
        " ".join(credentials["scopes"]),
    ).split()

    if "chatgpt.tokens.use.direct" not in new_scopes:
        raise AppError("Plan usage permission was lost")

    credentials.update(
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", credentials["refresh_token"]),
            "scopes": new_scopes,
            "expires_at": time.time() + tokens["expires_in"],
        }
    )

    save_credentials(credentials)

    return credentials["access_token"]
