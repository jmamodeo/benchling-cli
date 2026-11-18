from __future__ import annotations

import httpx
import pytest
from conftest import make_profile

from benchling_agent.auth import AuthManager
from benchling_agent.config import ConfigStore
from benchling_agent.errors import AuthenticationError, UpstreamError


def auth_for(handler, secrets=None) -> AuthManager:
    return AuthManager(
        {"sandbox": "secret"} if secrets is None else secrets,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_config_store_owns_only_plan_journal_and_lock_dirs(tmp_path) -> None:
    store = ConfigStore(tmp_path / "app")
    store.ensure()
    assert sorted(path.name for path in store.root.iterdir()) == ["journals", "locks", "plans"]


def test_missing_secret_is_an_authentication_error() -> None:
    auth = auth_for(lambda request: pytest.fail("no token request expected"), secrets={})
    with pytest.raises(AuthenticationError):
        auth.headers(make_profile())
    assert auth.status(make_profile())["configured"] is False


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"access_token": "t", "token_type": "mac", "expires_in": 900}, UpstreamError),
        ({"access_token": "t"}, UpstreamError),
    ],
)
def test_bad_token_responses_are_rejected(payload, error) -> None:
    auth = auth_for(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(error):
        auth.headers(make_profile())
