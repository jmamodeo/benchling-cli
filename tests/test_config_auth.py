from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from benchling_agent.auth import CLIENT_SECRET, TOKEN_BUNDLE, AuthManager
from benchling_agent.config import ConfigStore
from benchling_agent.credentials import MemoryCredentialStore
from benchling_agent.errors import AuthenticationError, ValidationError
from benchling_agent.login import LoginCoordinator
from benchling_agent.models import AuthMethod, Profile


def test_profile_selection_is_explicit_when_multiple(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    for name in ("sandbox", "production"):
        config.upsert(
            Profile(
                name=name,
                tenant_url=f"https://{name}.benchling.com",
                auth_method=AuthMethod.API_KEY,
            )
        )
    with pytest.raises(ValidationError) as caught:
        config.resolve(None)
    assert caught.value.details["available_profiles"] == ["production", "sandbox"]
    assert config.resolve("sandbox").name == "sandbox"


def test_delegated_refresh_rotates_token_atomically(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    profile = Profile(
        name="sandbox",
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.DELEGATED,
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
    )
    config.upsert(profile)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        assert b"grant_type=refresh_token" in request.content
        assert b"refresh_token=old-refresh" in request.content
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 900,
                "refresh_token_expires_in": 2592000,
            },
        )

    auth = AuthManager(
        config,
        credentials,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    credentials.set("sandbox", CLIENT_SECRET, "secret")
    credentials.set(
        "sandbox",
        TOKEN_BUNDLE,
        '{"access_token":"old","refresh_token":"old-refresh","expires_at":"2020-01-01T00:00:00Z","refresh_expires_at":"2099-01-01T00:00:00Z","token_type":"Bearer"}',
    )
    assert auth.headers(profile) == {"Authorization": "Bearer new-access"}
    stored = credentials.get("sandbox", TOKEN_BUNDLE)
    assert stored and "new-refresh" in stored and "old-refresh" not in stored


def test_invalid_oauth_state_is_rejected(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    coordinator = LoginCoordinator(config, credentials)
    with pytest.raises(ValidationError):
        coordinator.callback_code("code", "unknown", None)


def test_login_form_requires_csrf(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    coordinator = LoginCoordinator(config, credentials)
    with pytest.raises(ValidationError):
        coordinator.configure({"csrf": ["wrong"]})


def test_delegated_callback_success_stores_tokens(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=authorization_code" in request.content
        assert b"code=authorization-code" in request.content
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 900,
                "refresh_token_expires_in": 2592000,
            },
        )

    auth = AuthManager(
        config,
        credentials,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    coordinator = LoginCoordinator(config, credentials, auth=auth)
    authorization = coordinator.configure(
        {
            "csrf": [coordinator.csrf],
            "profile_name": ["sandbox"],
            "tenant_url": ["https://tenant.benchling.com"],
            "auth_method": ["delegated"],
            "api_key": [""],
            "client_id": ["client"],
            "client_secret": ["secret"],
        }
    )
    assert authorization and authorization.startswith(
        "https://tenant.benchling.com/oauth/authorize?"
    )
    state = next(iter(coordinator.states))
    assert coordinator.callback_code("authorization-code", state, None) is None
    assert coordinator.complete is True
    assert credentials.get("sandbox", TOKEN_BUNDLE) is not None


def test_delegated_callback_cancellation_is_reported(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    coordinator = LoginCoordinator(config, credentials)
    with pytest.raises(AuthenticationError):
        coordinator.callback_code(None, "unused", "access_denied")


def test_concurrent_refresh_uses_one_rotating_token_exchange(
    config: ConfigStore, credentials: MemoryCredentialStore
) -> None:
    profile = Profile(
        name="sandbox",
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.DELEGATED,
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
    )
    config.upsert(profile)
    count = 0
    count_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        with count_lock:
            count += 1
        time.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 900,
                "refresh_token_expires_in": 2592000,
            },
        )

    auth = AuthManager(
        config,
        credentials,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    credentials.set("sandbox", CLIENT_SECRET, "secret")
    credentials.set(
        "sandbox",
        TOKEN_BUNDLE,
        '{"access_token":"old","refresh_token":"old-refresh","expires_at":"2020-01-01T00:00:00Z","refresh_expires_at":"2099-01-01T00:00:00Z","token_type":"Bearer"}',
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        headers = list(pool.map(lambda _: auth.headers(profile), range(2)))
    assert headers == [
        {"Authorization": "Bearer new-access"},
        {"Authorization": "Bearer new-access"},
    ]
    assert count == 1
