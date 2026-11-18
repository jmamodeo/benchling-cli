from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ValidationError
from .models import Profile


def application_home() -> Path:
    override = os.getenv("BENCHLING_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys_platform() == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "benchling-agent"


def sys_platform() -> str:
    import sys

    return sys.platform


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    profiles: dict[str, Profile] = Field(default_factory=dict)


class ConfigStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or application_home()
        self.path = self.root / "profiles.json"

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

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            return AppConfig.model_validate_json(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "Profile configuration is invalid",
                details={"path": str(self.path), "reason": str(exc)},
            ) from exc

    def save(self, config: AppConfig) -> None:
        self.ensure()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            config.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def upsert(self, profile: Profile) -> None:
        config = self.load()
        config.profiles[profile.name] = profile
        self.save(config)

    def remove(self, name: str) -> Profile:
        config = self.load()
        try:
            profile = config.profiles.pop(name)
        except KeyError as exc:
            raise ValidationError(
                f"Unknown profile: {name}", details={"profile": name}
            ) from exc
        self.save(config)
        return profile

    def resolve(self, name: str | None) -> Profile:
        profiles = self.load().profiles
        if name:
            try:
                return profiles[name]
            except KeyError as exc:
                raise ValidationError(
                    f"Unknown profile: {name}",
                    details={"available_profiles": sorted(profiles)},
                ) from exc
        if len(profiles) == 1:
            return next(iter(profiles.values()))
        if not profiles:
            raise ValidationError("No profiles configured; run `benchling login`")
        raise ValidationError(
            "A profile is required when multiple profiles exist",
            details={"available_profiles": sorted(profiles)},
        )

    def metadata(self) -> dict[str, Any]:
        config = self.load()
        return {
            name: profile.model_dump(mode="json", exclude_none=True)
            for name, profile in sorted(config.profiles.items())
        }

