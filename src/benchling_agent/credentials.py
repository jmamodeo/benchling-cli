from __future__ import annotations

import json
from typing import Protocol

from .errors import AuthenticationError

SERVICE_NAME = "benchling-agent"


class CredentialStore(Protocol):
    def get(self, profile: str, key: str) -> str | None: ...

    def set(self, profile: str, key: str, value: str) -> None: ...

    def delete(self, profile: str, key: str) -> None: ...


class KeyringCredentialStore:
    def _username(self, profile: str, key: str) -> str:
        return f"{profile}:{key}"

    def _keyring(self):
        try:
            import keyring
        except ImportError as exc:
            raise AuthenticationError(
                "The keyring package is unavailable; reinstall benchling-agent"
            ) from exc
        return keyring

    def get(self, profile: str, key: str) -> str | None:
        try:
            return self._keyring().get_password(
                SERVICE_NAME, self._username(profile, key)
            )
        except Exception as exc:
            raise AuthenticationError(
                "The operating-system credential store is unavailable",
                details={"profile": profile, "reason": str(exc)},
            ) from exc

    def set(self, profile: str, key: str, value: str) -> None:
        try:
            self._keyring().set_password(
                SERVICE_NAME, self._username(profile, key), value
            )
        except Exception as exc:
            raise AuthenticationError(
                "Could not save credentials in the operating-system credential store",
                details={"profile": profile, "reason": str(exc)},
            ) from exc

    def delete(self, profile: str, key: str) -> None:
        try:
            self._keyring().delete_password(
                SERVICE_NAME, self._username(profile, key)
            )
        except Exception as exc:
            # Keyring backends disagree on missing-item exception types.
            if "not found" not in str(exc).lower():
                raise AuthenticationError(
                    "Could not clear credentials from the operating-system credential store",
                    details={"profile": profile, "reason": str(exc)},
                ) from exc


class MemoryCredentialStore:
    """Test-only credential store."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, profile: str, key: str) -> str | None:
        return self.values.get((profile, key))

    def set(self, profile: str, key: str, value: str) -> None:
        self.values[(profile, key)] = value

    def delete(self, profile: str, key: str) -> None:
        self.values.pop((profile, key), None)

    def dump(self) -> str:
        return json.dumps({f"{p}:{k}": v for (p, k), v in self.values.items()})
