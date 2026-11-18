from __future__ import annotations

import os
import sys
from pathlib import Path


def application_home() -> Path:
    override = os.getenv("BENCHLING_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "benchling-agent"


class ConfigStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or application_home()

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def journals_dir(self) -> Path:
        return self.root / "journals"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.plans_dir,
            self.journals_dir,
            self.locks_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
