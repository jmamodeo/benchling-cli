from __future__ import annotations

import os

import httpx
import pytest
import pytest_asyncio
from fake_benchling import TENANT, FakeBenchling

from benchling_agent.api import BenchlingAPI
from benchling_agent.auth import AuthManager
from benchling_agent.models import AuthMethod, Profile
from benchling_agent.runtime import Runtime, RuntimePool


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


def token_client() -> httpx.Client:
    def token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 900}
        )

    return httpx.Client(transport=httpx.MockTransport(token))


@pytest.fixture
def benchling_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCHLING_") and name != "BENCHLING_AGENT_HOME":
            monkeypatch.delenv(name)
    monkeypatch.setenv("BENCHLING_SANDBOX_API_ENDPOINT", TENANT)
    monkeypatch.setenv("BENCHLING_SANDBOX_CLIENT_ID", "client")
    monkeypatch.setenv("BENCHLING_SANDBOX_CLIENT_SECRET", "secret")


@pytest.fixture
def fake() -> FakeBenchling:
    return FakeBenchling()


def runtime_for(fake: FakeBenchling, env: str = "sandbox") -> Runtime:
    return Runtime(
        env,
        auth_http=token_client(),
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )


@pytest_asyncio.fixture
async def runtime(benchling_env, fake):
    active = runtime_for(fake)
    yield active
    await active.close()


@pytest_asyncio.fixture
async def pool(benchling_env, fake):
    runtimes = RuntimePool(runtimes={"sandbox": runtime_for(fake)})
    yield runtimes
    await runtimes.close()
