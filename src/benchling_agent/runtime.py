from __future__ import annotations

import httpx

from .api import BenchlingAPI
from .auth import AuthManager
from .config import ConfigStore
from .credentials import CredentialStore
from .environment import EnvironmentConfigStore, environment_credentials
from .plans import ChangePlanner, PlanRepository
from .services import BenchlingService


class Runtime:
    def __init__(
        self,
        profile_name: str | None,
        *,
        config: ConfigStore | None = None,
        credentials: CredentialStore | None = None,
        async_http: httpx.AsyncClient | None = None,
        auth_http: httpx.Client | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.credentials = credentials or environment_credentials()
        self.profile = self.config.resolve(profile_name)
        self.auth = AuthManager(self.config, self.credentials, http=auth_http)
        self.api = BenchlingAPI(self.profile, self.auth, http=async_http)
        self.service = BenchlingService(self.api)
        self.repository = PlanRepository(self.config)
        self.planner = ChangePlanner(self.service, self.api, self.repository)

    async def close(self) -> None:
        await self.api.close()
        self.auth.close()


class RuntimePool:
    """Lazily create and cache one Runtime per environment."""

    def __init__(
        self,
        *,
        config: ConfigStore | None = None,
        credentials: CredentialStore | None = None,
        runtimes: dict[str, Runtime] | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.credentials = credentials or environment_credentials()
        self._runtimes: dict[str, Runtime] = dict(runtimes or {})

    def available(self) -> list[str]:
        return sorted(set(self._runtimes) | set(self.config.metadata()))

    def get(self, env: str) -> Runtime:
        runtime = self._runtimes.get(env)
        if runtime is None:
            runtime = Runtime(env, config=self.config, credentials=self.credentials)
            self._runtimes[runtime.profile.name] = runtime
        return runtime

    async def close(self) -> None:
        runtimes, self._runtimes = list(self._runtimes.values()), {}
        for runtime in runtimes:
            await runtime.close()
