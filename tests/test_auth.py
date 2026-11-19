from __future__ import annotations

import httpx
import pytest
from conftest import make_environment

from benchling_toolkit.auth import AuthManager
from benchling_toolkit.errors import AuthenticationError, UpstreamError


def auth_for(handler, secrets=None) -> AuthManager:
    return AuthManager(
        {"sandbox": "secret"} if secrets is None else secrets,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_missing_secret_is_an_authentication_error() -> None:
    auth = auth_for(lambda request: pytest.fail("no token request expected"), secrets={})
    with pytest.raises(AuthenticationError):
        auth.headers(make_environment())
    assert auth.status(make_environment())["configured"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "t", "token_type": "mac", "expires_in": 900},
        {"access_token": "t"},
    ],
)
def test_bad_token_responses_are_rejected(payload) -> None:
    auth = auth_for(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(UpstreamError):
        auth.headers(make_environment())
