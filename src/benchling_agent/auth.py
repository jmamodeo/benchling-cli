from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .errors import AuthenticationError, UpstreamError
from .models import Profile, TokenBundle
from .tls import verified_context


class AuthManager:
    """Client-credentials auth with an in-memory, per-environment token cache."""

    def __init__(
        self,
        secrets: Mapping[str, str],
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=30.0, verify=verified_context())
        self._owns_http = http is None
        self._app_tokens: dict[tuple[str, str, str | None], TokenBundle] = {}
        self._app_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def headers(self, profile: Profile) -> dict[str, str]:
        return self._app_headers(profile)

    def status(self, profile: Profile) -> dict[str, Any]:
        bundle = self._app_tokens.get((profile.name, profile.base_url, profile.client_id))
        return {
            "profile": profile.name,
            "method": profile.auth_method.value,
            "configured": bool(profile.client_id and self.secrets.get(profile.name)),
            "access_token_usable": bundle.usable() if bundle else False,
            "tenant_url": profile.base_url,
            "credential_source": "environment",
            "identity": "app",
        }

    def _app_headers(self, profile: Profile) -> dict[str, str]:
        # Client credentials issue no refresh token. A long-lived MCP process
        # requests a new access token on expiry; each CLI has its own cache.
        with self._app_lock:
            cache_key = (profile.name, profile.base_url, profile.client_id)
            bundle = self._app_tokens.get(cache_key)
            if bundle and bundle.usable():
                return {"Authorization": f"Bearer {bundle.access_token}"}
            secret = self.secrets.get(profile.name)
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
        return TokenBundle(
            access_token=payload["access_token"],
            token_type=payload.get("token_type", "Bearer"),
            expires_at=now + timedelta(seconds=int(payload["expires_in"])),
        )
