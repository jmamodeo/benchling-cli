"""OS keychain storage for client secrets.

The service name is ``benchling-toolkit`` and the username is the environment
name. There is no plaintext file fallback.
"""

from __future__ import annotations

from typing import Any, Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .errors import AuthenticationError
from .redact import register_secret

SERVICE_NAME = "benchling-toolkit"
NO_KEYRING_MESSAGE = (
    "No usable OS keychain is available. Client secrets are stored in the "
    "system keychain and are never written to plaintext files. On Linux, "
    "install and unlock a Secret Service provider such as gnome-keyring or "
    "KWallet, and run this command in a session that can reach it over D-Bus. "
    "macOS and Windows use the system keychain automatically."
)


class _Backend(Protocol):
    priority: float

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _backend_name(backend: object) -> str:
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


def _usable(backend: Any) -> bool:
    """True when the backend is a real OS keychain, not fail/null/plaintext."""
    kind = type(backend)
    module = kind.__module__.lower()
    name = kind.__name__.lower()
    if module.endswith(".fail") or module.endswith(".null"):
        return False
    if "plaintext" in module or "plaintext" in name:
        return False
    try:
        priority = float(backend.priority)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False
    return priority >= 1


class Keychain:
    """Read and write one environment's client secret."""

    def __init__(self, backend: _Backend | None = None) -> None:
        self._backend = backend

    def backend(self) -> Any:
        """Return the injected backend, or the process-wide keyring backend."""
        if self._backend is not None:
            return self._backend
        return keyring.get_keyring()

    def require(self) -> None:
        """Raise AuthenticationError when no OS keychain can store a secret."""
        backend = self.backend()
        if _usable(backend):
            return
        raise AuthenticationError(
            NO_KEYRING_MESSAGE,
            details={"backend": _backend_name(backend)},
        )

    def get(self, env: str) -> str | None:
        """Return the stored secret, or None when this environment has none."""
        self.require()
        try:
            value = self.backend().get_password(SERVICE_NAME, env)
        except KeyringError as exc:
            raise _unavailable(exc) from exc
        if isinstance(value, str) and value:
            register_secret(value)
            return value
        return None

    def set(self, env: str, secret: str) -> None:
        """Store a client secret. The value is not logged."""
        self.require()
        register_secret(secret)
        try:
            self.backend().set_password(SERVICE_NAME, env, secret)
        except KeyringError as exc:
            raise _unavailable(exc) from exc

    def delete(self, env: str) -> None:
        """Remove a client secret. Missing entries are not an error."""
        self.require()
        try:
            self.backend().delete_password(SERVICE_NAME, env)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise _unavailable(exc) from exc


def _unavailable(exc: KeyringError) -> AuthenticationError:
    return AuthenticationError(
        NO_KEYRING_MESSAGE,
        details={"backend": type(exc).__name__},
    )
