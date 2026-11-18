from __future__ import annotations

import httpx
import pytest

from benchling_agent.api import BenchlingAPI
from benchling_agent.auth import AuthManager
from benchling_agent.models import AuthMethod, Profile


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "app"))


def make_profile(name: str = "sandbox") -> Profile:
    return Profile(
        name=name,
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.CLIENT_CREDENTIALS,
        client_id="client",
    )


def make_api(handler, *, profile: Profile | None = None) -> BenchlingAPI:
    profile = profile or make_profile()

    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 900}
        )

    auth = AuthManager(
        {profile.name: "secret"}, http=httpx.Client(transport=httpx.MockTransport(token))
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BenchlingAPI(profile, auth, http=client, max_read_attempts=1)
