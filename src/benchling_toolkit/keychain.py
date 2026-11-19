"""OS keychain storage for client secrets.

The service name is ``benchling-toolkit`` and the username is the environment
name. There is no plaintext file fallback.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Protocol

import keyring
from keyring.errors import KeyringError, KeyringLocked, PasswordDeleteError

from .errors import AuthenticationError
from .redact import register_secret

SERVICE_NAME = "benchling-toolkit"
_SECRET_SERVICE_NAME = "org.freedesktop.secrets"
_NO_PLAINTEXT = (
    "Client secrets are stored in the system keychain and are never written to plaintext files."
)
_UNLOCK = "gnome-keyring-daemon --unlock --components=secrets"
_SESSION = (
    'dbus-run-session -- sh -c \'printf "%s\\n" "$KEYRING_PASSWORD" | '
    "gnome-keyring-daemon --unlock --components=secrets'"
)
_NO_SUBSTITUTION = (
    "Do not capture gnome-keyring-daemon --unlock with $(...) or backticks. "
    "The daemon does not exit, so the substitution hangs. Run the unlock and "
    "benchling inside one dbus-run-session shell."
)
_SHARE_BUS = (
    "Later shells and the MCP server must use the same DBUS_SESSION_BUS_ADDRESS, "
    "or the keychain is not visible to them."
)
_MCP_ENV = (
    "The standard MCP SDK launches stdio servers with only HOME, LOGNAME, PATH, "
    "SHELL, TERM and USER in the environment, so DBUS_SESSION_BUS_ADDRESS is missing. "
    "Set DBUS_SESSION_BUS_ADDRESS in the MCP client config env block."
)
NO_KEYRING_MESSAGE = (
    f"No usable OS keychain is available. {_NO_PLAINTEXT} On Linux, install and unlock "
    "a Secret Service provider such as gnome-keyring or KWallet. "
    f"{_SESSION}. {_NO_SUBSTITUTION} {_SHARE_BUS} {_MCP_ENV} "
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


def provider_installed() -> bool:
    """True when a Linux Secret Service provider binary is on PATH."""
    return any(
        shutil.which(name)
        for name in ("gnome-keyring-daemon", "kwalletd6", "kwalletd5", "kwalletd")
    )


def secret_service_reachable() -> bool:
    """True when this session bus owns org.freedesktop.secrets."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        connection = _open_session_bus()
    except Exception:
        return False
    try:
        return _secret_service_owned(connection)
    except Exception:
        return False
    finally:
        _close_bus(connection)


def _open_session_bus() -> Any:
    from jeepney.io.blocking import open_dbus_connection  # type: ignore[import-untyped]

    return open_dbus_connection(bus="SESSION", auth_timeout=1.0)


def _secret_service_owned(connection: Any) -> bool:
    from jeepney.bus_messages import message_bus  # type: ignore[import-untyped]
    from jeepney.io.blocking import Proxy

    owned: Any = Proxy(message_bus, connection).NameHasOwner(_SECRET_SERVICE_NAME)
    value = owned[0] if isinstance(owned, tuple) else owned
    return bool(value)


def _close_bus(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return


def _is_locked(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, KeyringLocked):
        return True
    return "lock" in type(exc).__name__.lower()


def classify_keychain_failure(backend: object | None, exc: BaseException | None = None) -> str:
    """Return no_backend, dbus_unreachable, or locked."""
    if _is_locked(exc):
        return "locked"
    if sys.platform.startswith("linux"):
        if not provider_installed():
            return "no_backend"
        if not secret_service_reachable():
            return "dbus_unreachable"
        if exc is not None:
            return "locked"
    return "no_backend"


def keychain_message(cause: str) -> str:
    """Actionable text for one keychain failure cause. Never includes secret values."""
    if cause == "locked":
        return (
            "No usable OS keychain is available because the Secret Service collection is locked. "
            f"{_NO_PLAINTEXT} Unlock it in this session by piping the keyring password on stdin, "
            f"not as an argument: printf '%s\\n' \"$KEYRING_PASSWORD\" | {_UNLOCK}. "
            f"If there is no session bus, run that inside dbus-run-session. {_NO_SUBSTITUTION} "
            f"{_SHARE_BUS} {_MCP_ENV}"
        )
    if cause == "dbus_unreachable":
        return (
            "No usable OS keychain is available because Secret Service is not reachable on D-Bus. "
            f"{_NO_PLAINTEXT} DBUS_SESSION_BUS_ADDRESS is unset, or this bus has no "
            f"{_SECRET_SERVICE_NAME} name. On headless Linux, start an unlocked keyring and run "
            f"benchling in that same session: {_SESSION}. {_NO_SUBSTITUTION} {_SHARE_BUS} {_MCP_ENV} "
            "macOS and Windows use the system keychain automatically."
        )
    return (
        "No usable OS keychain is available because no Secret Service backend is installed. "
        f"{_NO_PLAINTEXT} On Linux, install gnome-keyring and dbus "
        "(for example `sudo apt-get install -y gnome-keyring dbus`), then unlock a session: "
        f"{_SESSION}. {_NO_SUBSTITUTION} {_SHARE_BUS} {_MCP_ENV} "
        "macOS and Windows use the system keychain automatically."
    )


def _keychain_error(backend: object | None, exc: BaseException | None) -> AuthenticationError:
    cause = classify_keychain_failure(backend, exc)
    if backend is not None:
        backend_name: str = _backend_name(backend)
    elif exc is not None:
        backend_name = type(exc).__name__
    else:
        backend_name = "unknown"
    return AuthenticationError(
        keychain_message(cause),
        details={"backend": backend_name, "cause": cause},
    )


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
        raise _keychain_error(backend, None)

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
    return _keychain_error(None, exc)
