from __future__ import annotations

import html
import secrets
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError as PydanticValidationError

from .auth import AuthManager
from .config import ConfigStore
from .credentials import CredentialStore
from .errors import AuthenticationError, BenchlingAgentError, ValidationError
from .models import AuthMethod, Profile


class LoginCoordinator:
    """State machine behind the temporary loopback setup server."""

    def __init__(
        self,
        config: ConfigStore,
        credentials: CredentialStore,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        auth: AuthManager | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValidationError("Login server must bind to loopback")
        self.config = config
        self.credentials = credentials
        self.host = host
        self.port = port
        self.origin = f"http://{host}:{port}"
        self.callback = f"{self.origin}/oauth/callback"
        self.auth = auth or AuthManager(config, credentials)
        self.csrf = secrets.token_urlsafe(32)
        self.states: dict[str, Profile] = {}
        self.pending: list[Profile] = []
        self.complete = False
        self.failure: BenchlingAgentError | None = None
        self.result: dict[str, Any] = {"profiles": [], "delegated_profiles": []}

    def configure(self, form: dict[str, list[str]]) -> str | None:
        if form.get("csrf", [None])[0] != self.csrf:
            raise ValidationError("Invalid setup form token")
        names = form.get("profile_name", [])
        urls = form.get("tenant_url", [])
        methods = form.get("auth_method", [])
        keys = form.get("api_key", [])
        clients = form.get("client_id", [])
        secrets_ = form.get("client_secret", [])
        count = len(names)
        if not count or not all(len(values) == count for values in (urls, methods, keys, clients, secrets_)):
            raise ValidationError("Every profile row must be complete")
        profiles: list[Profile] = []
        for index in range(count):
            method = AuthMethod(methods[index])
            try:
                profile = Profile(
                    name=names[index].strip(),
                    tenant_url=urls[index].strip(),
                    auth_method=method,
                    client_id=clients[index].strip() or None,
                    redirect_uri=self.callback if method == AuthMethod.DELEGATED else None,
                )
            except (PydanticValidationError, ValueError) as exc:
                raise ValidationError(
                    "Profile configuration is invalid",
                    details={"row": index + 1, "reason": str(exc)},
                ) from exc
            if method == AuthMethod.API_KEY:
                if not keys[index].strip():
                    raise ValidationError(
                        "API-key profiles require an API key", details={"profile": profile.name}
                    )
            else:
                if not profile.client_id or not secrets_[index].strip():
                    raise ValidationError(
                        "Delegated profiles require a client ID and client secret",
                        details={"profile": profile.name},
                    )
            profiles.append(profile)

        for index, profile in enumerate(profiles):
            self.config.upsert(profile)
            if profile.auth_method == AuthMethod.API_KEY:
                self.auth.save_api_key(profile, keys[index])
            else:
                self.auth.save_client_secret(profile, secrets_[index])
                self.pending.append(profile)
            self.result["profiles"].append(profile.name)
        return self.next_authorization()

    def next_authorization(self) -> str | None:
        if not self.pending:
            self.complete = True
            return None
        profile = self.pending.pop(0)
        url, state = self.auth.authorization_url(profile)
        self.states[state] = profile
        return url

    def callback_code(self, code: str | None, state: str | None, error: str | None) -> str | None:
        if error:
            raise AuthenticationError(
                "Benchling authorization was cancelled or rejected", details={"error": error}
            )
        if not code or not state or state not in self.states:
            raise ValidationError("OAuth callback is missing a valid code or state")
        profile = self.states.pop(state)
        self.auth.exchange_code(profile, code)
        self.result["delegated_profiles"].append(profile.name)
        return self.next_authorization()


def _page(csrf: str, callback: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Benchling CLI login</title>
<style>body{{font:16px system-ui;max-width:920px;margin:3rem auto;padding:0 1rem;color:#17212b}}fieldset{{border:1px solid #ccd4dc;border-radius:8px;margin:1rem 0;padding:1rem}}label{{display:block;margin:.6rem 0}}input,select{{box-sizing:border-box;width:100%;padding:.55rem}}button{{padding:.65rem 1rem;margin:.5rem .5rem 0 0}}.hint{{color:#56616d}}.remove{{float:right}}</style></head>
<body><h1>Connect the Benchling CLI</h1>
<p class="hint">Add one row per Benchling tenant. Delegated login opens Benchling or your organization's SSO page. This form never accepts a Benchling account password.</p>
<form method="post" action="/configure"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<div id="profiles"><fieldset class="profile"><button type="button" class="remove" onclick="removeRow(this)">Remove</button>
<label>Profile name <input name="profile_name" value="production" required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}"></label>
<label>Tenant URL <input name="tenant_url" type="url" placeholder="https://example.benchling.com" required></label>
<label>Login method <select name="auth_method" onchange="toggle(this)"><option value="delegated">Browser / SSO (delegated app)</option><option value="api_key">Personal API key</option></select></label>
<div class="delegated"><label>App client ID <input name="client_id"></label><label>App client secret <input name="client_secret" type="password"></label><p class="hint">Register this exact callback URL in Benchling: <code>{html.escape(callback)}</code></p></div>
<div class="api" hidden><label>Personal API key <input name="api_key" type="password"></label></div>
</fieldset></div>
<button type="button" onclick="addRow()">Add another tenant</button><button type="submit">Continue</button></form>
<script>function toggle(s){{let r=s.closest('.profile');let api=r.querySelector('.api'),del=r.querySelector('.delegated');api.hidden=s.value!='api_key';del.hidden=s.value!='delegated';}}
function addRow(){{let n=document.querySelector('.profile').cloneNode(true);n.querySelectorAll('input').forEach(x=>{{if(x.name!='profile_name')x.value='';}});n.querySelector('[name=profile_name]').value='sandbox';document.querySelector('#profiles').appendChild(n);toggle(n.querySelector('select'));}}
function removeRow(b){{if(document.querySelectorAll('.profile').length>1)b.closest('.profile').remove();}}</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    coordinator: LoginCoordinator

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self._check_host()
            if parsed.path == "/":
                self._html(
                    HTTPStatus.OK,
                    _page(self.coordinator.csrf, self.coordinator.callback),
                )
                return
            if parsed.path == "/oauth/callback":
                query = parse_qs(parsed.query)
                next_url = self.coordinator.callback_code(
                    query.get("code", [None])[0],
                    query.get("state", [None])[0],
                    query.get("error", [None])[0],
                )
                if next_url:
                    self._redirect(next_url)
                else:
                    self._html(HTTPStatus.OK, "<h1>Benchling CLI login complete</h1><p>You may close this tab.</p>")
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            if parsed.path == "/oauth/callback":
                self.coordinator.complete = True
                self.coordinator.failure = (
                    exc
                    if isinstance(exc, BenchlingAgentError)
                    else AuthenticationError(
                        "Delegated login failed",
                        details={"reason": str(exc)},
                    )
                )
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_host()
            if self.path != "/configure":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            origin = self.headers.get("Origin")
            if origin and origin != self.coordinator.origin:
                raise ValidationError("Invalid form origin")
            length = int(self.headers.get("Content-Length", "0"))
            if length > 128_000:
                raise ValidationError("Setup form is too large")
            form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            next_url = self.coordinator.configure(form)
            if next_url:
                self._redirect(next_url)
            else:
                self._html(HTTPStatus.OK, "<h1>Benchling CLI login complete</h1><p>You may close this tab.</p>")
        except Exception as exc:
            self._error(exc)

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        allowed = {f"{self.coordinator.host}:{self.coordinator.port}", f"localhost:{self.coordinator.port}"}
        if host not in allowed:
            raise ValidationError("Invalid Host header")

    def _redirect(self, url: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _html(self, status: HTTPStatus, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        message = html.escape(getattr(exc, "message", str(exc)))
        self._html(HTTPStatus.BAD_REQUEST, f"<h1>Login failed</h1><p>{message}</p><p><a href='/'>Return to setup</a></p>")


def run_login_server(
    config: ConfigStore,
    credentials: CredentialStore,
    *,
    port: int = 8765,
    timeout: int = 300,
    open_browser: bool = True,
) -> dict[str, Any]:
    coordinator = LoginCoordinator(config, credentials, port=port)
    handler = type("LoginHandler", (_Handler,), {"coordinator": coordinator})
    server = ThreadingHTTPServer((coordinator.host, port), handler)
    server.timeout = 0.5
    url = coordinator.origin + "/"
    if open_browser:
        webbrowser.open(url)
    started = time.monotonic()
    try:
        while not coordinator.complete and time.monotonic() - started < timeout:
            server.handle_request()
    finally:
        server.server_close()
        coordinator.auth.close()
    if coordinator.failure:
        raise coordinator.failure
    if not coordinator.complete:
        raise AuthenticationError(
            "Login setup timed out", details={"url": url, "timeout_seconds": timeout}
        )
    return {**coordinator.result, "url": url}
