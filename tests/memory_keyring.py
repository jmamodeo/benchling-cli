"""In-memory keyring backend for tests and the offline demo."""

from __future__ import annotations

import keyring
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    """Process-local keychain. Not selected automatically; tests install it."""

    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.passwords[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(username) from exc
