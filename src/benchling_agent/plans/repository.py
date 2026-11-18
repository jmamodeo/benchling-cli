from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..config import ConfigStore
from ..errors import (
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)

SAFE_ID = re.compile(r"^[0-9a-f]{32}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum_for(plan: dict[str, Any]) -> str:
    clean = dict(plan)
    clean.pop("checksum", None)
    return hashlib.sha256(canonical_json(clean).encode()).hexdigest()


def json_diff(before: Any, after: Any) -> dict[str, Any]:
    return {"before": before, "after": after, "changed": before != after}


class PlanRepository:
    def __init__(self, config: ConfigStore) -> None:
        self.config = config

    def save_plan(self, plan: ChangePlan) -> None:
        self.config.ensure()
        path = self._plan_path(plan.id)
        if path.exists():
            raise ValidationError("Plan ID already exists")
        self._atomic_write(path, plan.model_dump_json(indent=2, exclude_none=True))

    def load_plan(self, plan_id: str) -> ChangePlan:
        path = self._plan_path(plan_id)
        if not path.exists():
            raise ValidationError("Unknown change plan", details={"plan_id": plan_id})
        plan = ChangePlan.model_validate_json(path.read_text("utf-8"))
        if checksum_for(plan.model_dump(mode="json")) != plan.checksum:
            raise ValidationError(
                "Change plan checksum does not match; the plan cannot be applied",
                details={"plan_id": plan_id},
            )
        return plan

    def load_journal(self, plan_id: str) -> ChangeJournal:
        path = self._journal_path(plan_id)
        if not path.exists():
            return ChangeJournal(plan_id=plan_id)
        return ChangeJournal.model_validate_json(path.read_text("utf-8"))

    def save_journal(self, journal: ChangeJournal) -> None:
        self.config.ensure()
        self._atomic_write(
            self._journal_path(journal.plan_id), journal.model_dump_json(indent=2)
        )

    def _plan_path(self, plan_id: str) -> Path:
        self._validate_id(plan_id)
        return self.config.plans_dir / f"{plan_id}.json"

    def _journal_path(self, plan_id: str) -> Path:
        self._validate_id(plan_id)
        return self.config.journals_dir / f"{plan_id}.json"

    def _validate_id(self, plan_id: str) -> None:
        if not SAFE_ID.fullmatch(plan_id):
            raise ValidationError("Invalid plan ID", details={"plan_id": plan_id})

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, "utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
