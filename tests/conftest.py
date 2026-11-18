from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from benchling_agent.api import BenchlingAPI
from benchling_agent.auth import AuthManager
from benchling_agent.config import ConfigStore
from benchling_agent.credentials import MemoryCredentialStore
from benchling_agent.models import AuthMethod, Profile


@pytest.fixture
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "app")


@pytest.fixture
def credentials() -> MemoryCredentialStore:
    return MemoryCredentialStore()


def make_profile(name: str = "sandbox") -> Profile:
    return Profile(
        name=name,
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.API_KEY,
    )


def make_api(
    config: ConfigStore,
    credentials: MemoryCredentialStore,
    handler,
    *,
    profile: Profile | None = None,
) -> BenchlingAPI:
    profile = profile or make_profile()
    config.upsert(profile)
    auth = AuthManager(config, credentials)
    auth.save_api_key(profile, "sk_test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BenchlingAPI(profile, auth, http=client, max_read_attempts=1)

