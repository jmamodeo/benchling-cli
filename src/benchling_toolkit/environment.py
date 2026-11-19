"""Named environments stored in environments.json.

The file holds the tenant URL and client ID only. Client secrets live in the
OS keychain. ``BENCHLING_<ENV>_*`` variables are not read.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError as ModelValidationError

from .config import ConfigStore
from .errors import ValidationError
from .models import AuthMethod, Environment

ENV_NAME = re.compile(r"^[a-z][a-z0-9]{0,31}$")
FILE_VERSION = 1


def write_private(path: Path, text: str) -> None:
    """Atomically replace a file and force mode 0600, ignoring chmod failures."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class EnvironmentConfigStore(ConfigStore):
    """Resolve environments from environments.json under the state directory.

    The inherited directories hold write plans and journals, not secrets.
    Files saved by previous versions are never read.
    """

    @property
    def environments_path(self) -> Path:
        """Path of the non-secret environment file."""
        return self.root / "environments.json"

    def configured_names(self) -> list[str]:
        """Sorted environment names saved in environments.json."""
        return sorted(self._records())

    def resolve(self, name: str | None) -> Environment:
        """Build the Environment for a name.

        Raises ValidationError if the name is missing, unknown, or the saved
        record is invalid. The client secret is not loaded here.
        """
        names = self.configured_names()
        if not name:
            raise ValidationError(
                "An environment is required; pass --env NAME",
                details={"available_envs": names},
            )
        records = self._records()
        if not ENV_NAME.match(name) or name not in records:
            raise ValidationError(
                "Unknown environment; run `benchling auth login --env NAME`",
                details={
                    "env": name if ENV_NAME.match(name) else None,
                    "available_envs": names,
                },
            )
        return self._environment(name, records[name])

    def metadata(self) -> dict[str, Any]:
        """Non-secret JSON description of every configured environment, keyed by name."""
        return {
            env: self.resolve(env).model_dump(mode="json", exclude_none=True)
            for env in self.configured_names()
        }

    def upsert(self, environment: Environment) -> None:
        """Insert or replace one environment's non-secret record."""
        if not environment.client_id:
            raise ValidationError("Client ID is required", details={"env": environment.name})
        records = self._records() if self.environments_path.exists() else {}
        records[environment.name] = {
            "tenant_url": str(environment.tenant_url),
            "client_id": environment.client_id,
        }
        self._write(records)

    def remove(self, name: str) -> bool:
        """Remove one environment. Returns True when a record was deleted."""
        if not self.environments_path.exists():
            return False
        records = self._records()
        if name not in records:
            return False
        del records[name]
        self._write(records)
        return True

    def _records(self) -> dict[str, Any]:
        path = self.environments_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "Could not read environments.json",
                details={"path": str(path)},
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != FILE_VERSION:
            raise ValidationError("environments.json has an unsupported version")
        environments = raw.get("environments")
        if not isinstance(environments, dict):
            raise ValidationError("environments.json is invalid")
        for name in environments:
            if not isinstance(name, str) or not ENV_NAME.match(name):
                raise ValidationError("environments.json contains an invalid environment name")
            if not isinstance(environments[name], dict):
                raise ValidationError(
                    "environments.json is invalid",
                    details={"env": name},
                )
        return environments

    def _environment(self, name: str, record: dict[str, Any]) -> Environment:
        try:
            environment = Environment.model_validate(
                {
                    "name": name,
                    "tenant_url": record.get("tenant_url"),
                    "client_id": record.get("client_id"),
                    "auth_method": AuthMethod.CLIENT_CREDENTIALS,
                }
            )
        except ModelValidationError as exc:
            # Pydantic errors include input values; never expose them.
            raise ValidationError("Invalid endpoint or client ID", details={"env": name}) from exc
        if not environment.client_id:
            raise ValidationError("Invalid endpoint or client ID", details={"env": name})
        return environment

    def _write(self, records: dict[str, Any]) -> None:
        self.ensure()
        payload = {"version": FILE_VERSION, "environments": records}
        write_private(self.environments_path, json.dumps(payload, indent=2) + "\n")
