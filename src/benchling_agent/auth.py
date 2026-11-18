from __future__ import annotations

import base64
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import ConfigStore
from .credentials import CredentialStore
from .errors import AuthenticationError, UpstreamError, ValidationError
from .models import AuthMethod, Profile, TokenBundle
from .tls import verified_context

API_KEY = "api_key"
CLIENT_SECRET = "client_secret"
TOKEN_BUNDLE = "token_bundle"


class AuthManager:
    def __init__(
        self,
        config: ConfigStore,
        credentials: CredentialStore,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.http = http or httpx.Client(timeout=30.0, verify=verified_context())
        self._owns_http = http is None
        self._app_tokens: dict[tuple[str, str, str | None], TokenBundle] = {}
        self._app_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def save_api_key(self, profile: Profile, api_key: str) -> None:
        if profile.auth_method != AuthMethod.API_KEY:
            raise ValidationError("Profile is not configured for API-key login")
        if not api_key.strip():
            raise ValidationError("API key cannot be empty")
        self.credentials.set(profile.name, API_KEY, api_key.strip())

    def save_client_secret(self, profile: Profile, client_secret: str) -> None:
        if profile.auth_method != AuthMethod.DELEGATED:
            raise ValidationError("Profile is not configured for delegated login")
        if not client_secret.strip():
            raise ValidationError("Client secret cannot be empty")
        self.credentials.set(profile.name, CLIENT_SECRET, client_secret.strip())

    def authorization_url(self, profile: Profile, state: str | None = None) -> tuple[str, str]:
        if profile.auth_method != AuthMethod.DELEGATED:
            raise ValidationError("Delegated login is not configured for this profile")
        if not profile.client_id or not profile.redirect_uri:
            raise ValidationError(
                "Delegated login requires client_id and redirect_uri",
                details={"profile": profile.name},
            )
        state = state or secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": profile.client_id,
                "redirect_uri": str(profile.redirect_uri),
                "response_type": "code",
                "state": state,
            }
        )
        return f"{profile.base_url}/oauth/authorize?{query}", state

    def exchange_code(self, profile: Profile, code: str) -> TokenBundle:
        secret = self.credentials.get(profile.name, CLIENT_SECRET)
        if not secret:
            raise AuthenticationError(
                "Delegated client secret is missing; run `benchling login`",
                details={"profile": profile.name},
            )
        if not profile.client_id or not profile.redirect_uri:
            raise AuthenticationError("Delegated profile is incomplete")
        response = self._token_request(
            profile,
            {
                "grant_type": "authorization_code",
                "client_id": profile.client_id,
                "client_secret": secret,
                "redirect_uri": str(profile.redirect_uri),
                "code": code,
            },
        )
        bundle = self._bundle(response)
        self._save_bundle(profile.name, bundle)
        return bundle

    def headers(self, profile: Profile) -> dict[str, str]:
        if profile.auth_method == AuthMethod.CLIENT_CREDENTIALS:
            return self._app_headers(profile)
        if profile.auth_method == AuthMethod.API_KEY:
            key = self.credentials.get(profile.name, API_KEY)
            if not key:
                raise AuthenticationError(
                    "API key is missing; run `benchling login`",
                    details={"profile": profile.name},
                )
            encoded = base64.b64encode(f"{key}:".encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        bundle = self._load_bundle(profile.name)
        if bundle and bundle.usable():
            return {"Authorization": f"{bundle.token_type} {bundle.access_token}"}

        self.config.ensure()
        lock = FileLock(str(self.config.locks_dir / f"{profile.name}.refresh.lock"), timeout=30)
        try:
            with lock:
                # Another process may have refreshed while this process waited.
                bundle = self._load_bundle(profile.name)
                if bundle and bundle.usable():
                    return {"Authorization": f"{bundle.token_type} {bundle.access_token}"}
                if not bundle or not bundle.refresh_token:
                    raise AuthenticationError(
                        "Delegated login has expired or is missing; run `benchling login`",
                        details={"profile": profile.name},
                    )
                if bundle.refresh_expires_at:
                    expires = bundle.refresh_expires_at
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if expires <= datetime.now(UTC):
                        raise AuthenticationError(
                            "Delegated refresh token has expired; run `benchling login`",
                            details={"profile": profile.name},
                        )
                refreshed = self._refresh(profile, bundle.refresh_token)
                self._save_bundle(profile.name, refreshed)
                return {"Authorization": f"{refreshed.token_type} {refreshed.access_token}"}
        except FileLockTimeout as exc:
            raise AuthenticationError(
                "Timed out waiting for another process to refresh authentication",
                details={"profile": profile.name},
                retryable=True,
            ) from exc

    def status(self, profile: Profile) -> dict[str, Any]:
        if profile.auth_method == AuthMethod.CLIENT_CREDENTIALS:
            bundle = self._app_tokens.get((profile.name, profile.base_url, profile.client_id))
            return {
                "profile": profile.name,
                "method": profile.auth_method.value,
                "configured": bool(profile.client_id and self.credentials.get(profile.name, CLIENT_SECRET)),
                "access_token_usable": bundle.usable() if bundle else False,
                "tenant_url": profile.base_url,
                "credential_source": "environment",
                "identity": "app",
            }
        if profile.auth_method == AuthMethod.API_KEY:
            configured = bool(self.credentials.get(profile.name, API_KEY))
            return {
                "profile": profile.name,
                "method": profile.auth_method.value,
                "configured": configured,
                "tenant_url": profile.base_url,
            }
        bundle = self._load_bundle(profile.name)
        return {
            "profile": profile.name,
            "method": profile.auth_method.value,
            "configured": bundle is not None,
            "access_token_usable": bundle.usable() if bundle else False,
            "expires_at": bundle.expires_at.isoformat() if bundle else None,
            "refresh_expires_at": (
                bundle.refresh_expires_at.isoformat()
                if bundle and bundle.refresh_expires_at
                else None
            ),
            "tenant_url": profile.base_url,
        }

    def clear(self, profile: Profile) -> None:
        self._app_tokens.pop((profile.name, profile.base_url, profile.client_id), None)
        for key in (API_KEY, CLIENT_SECRET, TOKEN_BUNDLE):
            self.credentials.delete(profile.name, key)

    def _app_headers(self, profile: Profile) -> dict[str, str]:
        # Client credentials issue no refresh token. A long-lived MCP process
        # requests a new access token on expiry; each CLI has its own cache.
        with self._app_lock:
            cache_key = (profile.name, profile.base_url, profile.client_id)
            bundle = self._app_tokens.get(cache_key)
            if bundle and bundle.usable():
                return {"Authorization": f"Bearer {bundle.access_token}"}
            secret = self.credentials.get(profile.name, CLIENT_SECRET)
            if not secret or not profile.client_id:
                raise AuthenticationError("App client credentials are missing")
            payload = self._token_request(profile, {
                "grant_type": "client_credentials",
                "client_id": profile.client_id,
                "client_secret": secret,
            })
            bundle = self._bundle(payload)
            if bundle.token_type.lower() != "bearer":
                raise UpstreamError("Benchling returned an unsupported token type")
            self._app_tokens[cache_key] = bundle
            return {"Authorization": f"Bearer {bundle.access_token}"}

    def _refresh(self, profile: Profile, refresh_token: str) -> TokenBundle:
        secret = self.credentials.get(profile.name, CLIENT_SECRET)
        if not secret or not profile.client_id:
            raise AuthenticationError(
                "Delegated app credentials are incomplete; run `benchling login`"
            )
        response = self._token_request(
            profile,
            {
                "grant_type": "refresh_token",
                "client_id": profile.client_id,
                "client_secret": secret,
                "refresh_token": refresh_token,
            },
        )
        return self._bundle(response)

    def _token_request(self, profile: Profile, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.http.post(
                f"{profile.base_url}/oauth/token",
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach the Benchling token endpoint",
                details={"profile": profile.name, "reason": str(exc)},
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise AuthenticationError(
                "Benchling rejected authentication",
                details={"status": response.status_code},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError("Benchling returned an invalid token response") from exc

    def _bundle(self, payload: dict[str, Any]) -> TokenBundle:
        if not payload.get("access_token") or not payload.get("expires_in"):
            raise UpstreamError(
                "Benchling returned an incomplete token response",
                details={"keys": sorted(payload)},
            )
        now = datetime.now(UTC)
        refresh_seconds = payload.get("refresh_token_expires_in")
        return TokenBundle(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_at=now + timedelta(seconds=int(payload["expires_in"])),
            refresh_expires_at=(
                now + timedelta(seconds=int(refresh_seconds))
                if refresh_seconds is not None
                else None
            ),
        )

    def _load_bundle(self, profile: str) -> TokenBundle | None:
        raw = self.credentials.get(profile, TOKEN_BUNDLE)
        if not raw:
            return None
        try:
            return TokenBundle.model_validate_json(raw)
        except ValueError as exc:
            raise AuthenticationError(
                "Stored delegated token data is invalid; run `benchling login`",
                details={"profile": profile},
            ) from exc

    def _save_bundle(self, profile: str, bundle: TokenBundle) -> None:
        self.credentials.set(profile, TOKEN_BUNDLE, bundle.model_dump_json())


def encode_pending_login(profile: Profile, state: str) -> str:
    """Small helper used by login tests and the loopback flow."""
    return json.dumps({"profile": profile.name, "state": state})
