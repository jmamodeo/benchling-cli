from __future__ import annotations

import os
import re
from typing import Any

from pydantic import ValidationError as ModelValidationError

from .config import ConfigStore
from .errors import AuthenticationError, ValidationError
from .models import AuthMethod, Profile

SUFFIXES = ("API_ENDPOINT", "CLIENT_ID", "CLIENT_SECRET")
ENV_NAME = re.compile(r"^[a-z][a-z0-9]{0,31}$")
_VARIABLE = re.compile(rf"^BENCHLING_([A-Z][A-Z0-9]{{0,31}})_({'|'.join(SUFFIXES)})$")


def variables(env: str) -> tuple[str, str, str]:
    prefix = f"BENCHLING_{env.upper()}_"
    endpoint, client_id, secret = (prefix + suffix for suffix in SUFFIXES)
    return endpoint, client_id, secret


def _value(key: str) -> str:
    return os.environ.get(key, "").strip()


def configured_envs() -> list[str]:
    """Environments with at least one BENCHLING_<ENV>_* variable set."""
    names = {
        match.group(1).lower()
        for key in os.environ
        if (match := _VARIABLE.match(key)) and _value(key)
    }
    return sorted(names)


def available_envs() -> list[str]:
    """Environments with all three variables set."""
    return [env for env in configured_envs() if all(_value(key) for key in variables(env))]


class EnvironmentConfigStore(ConfigStore):
    """Resolve environments from BENCHLING_<ENV>_* variables.

    The inherited directories hold write plans and journals, not credentials.
    Profiles saved by previous versions are never read.
    """

    def resolve(self, name: str | None) -> Profile:
        if not name:
            raise ValidationError(
                "An environment is required; pass --env NAME",
                details={"available_envs": available_envs()},
            )
        if not ENV_NAME.match(name) or name not in configured_envs():
            raise ValidationError(
                "Unknown environment; set BENCHLING_<ENV>_API_ENDPOINT, _CLIENT_ID, and _CLIENT_SECRET",
                details={"env": name if ENV_NAME.match(name) else None, "available_envs": available_envs()},
            )
        endpoint, client_id, secret = variables(name)
        missing = [key for key in (endpoint, client_id, secret) if not _value(key)]
        if missing:
            raise AuthenticationError(
                f"Set all three BENCHLING_{name.upper()}_* variables",
                details={"env": name, "missing_variables": missing},
            )
        try:
            return Profile(
                name=name,
                tenant_url=_value(endpoint),
                client_id=_value(client_id),
                auth_method=AuthMethod.CLIENT_CREDENTIALS,
            )
        except ModelValidationError as exc:
            # Pydantic errors include input values; never expose them.
            raise ValidationError("Invalid endpoint or client ID", details={"env": name}) from exc

    def metadata(self) -> dict[str, Any]:
        return {
            env: self.resolve(env).model_dump(mode="json", exclude_none=True)
            for env in configured_envs()
        }


def environment_secrets() -> dict[str, str]:
    """Take a process-local snapshot; never write secrets or tokens to disk."""
    return {
        env: secret for env in configured_envs() if (secret := _value(variables(env)[2]))
    }
