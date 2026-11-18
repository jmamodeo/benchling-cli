from __future__ import annotations

from collections.abc import Mapping

import httpx

from .api import BenchlingAPI
from .auth import AuthManager
from .environment import EnvironmentConfigStore, environment_secrets
from .plans import ChangePlanner, PlanRepository
from .services import BenchlingService


class Runtime:
    def __init__(
        self,
        env: str | None,
        *,
        config: EnvironmentConfigStore | None = None,
        secrets: Mapping[str, str] | None = None,
        async_http: httpx.AsyncClient | None = None,
        auth_http: httpx.Client | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.secrets = secrets if secrets is not None else environment_secrets()
        self.environment = self.config.resolve(env)
        self.auth = AuthManager(self.secrets, http=auth_http)
        self.api = BenchlingAPI(self.environment, self.auth, http=async_http)
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
        config: EnvironmentConfigStore | None = None,
        secrets: Mapping[str, str] | None = None,
        runtimes: dict[str, Runtime] | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.secrets = secrets if secrets is not None else environment_secrets()
        self._runtimes: dict[str, Runtime] = dict(runtimes or {})

    def available(self) -> list[str]:
        return sorted(set(self._runtimes) | set(self.config.metadata()))

    def get(self, env: str) -> Runtime:
        runtime = self._runtimes.get(env)
        if runtime is None:
            runtime = Runtime(env, config=self.config, secrets=self.secrets)
            self._runtimes[runtime.environment.name] = runtime
        return runtime

    async def close(self) -> None:
        runtimes, self._runtimes = list(self._runtimes.values()), {}
        for runtime in runtimes:
            await runtime.close()
