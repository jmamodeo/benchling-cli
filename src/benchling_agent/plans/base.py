"""Shared base class for the per-family planners."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..api import BenchlingAPI
from ..errors import (
    ConflictError,
)
from ..models import (
    ChangeJournal,
    JournalEvent,
)
from ..services import (
    BenchlingService,
)
from .repository import PlanRepository

# before, after, preconditions, guarantees, warnings
PlanParts = tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], list[str]]


class PlannerBase:
    """Holds the service, API and repository that every planner mixin relies on."""

    def __init__(
        self,
        service: BenchlingService,
        api: BenchlingAPI,
        repository: PlanRepository,
    ) -> None:
        self.service = service
        self.api = api
        self.repository = repository

    def _expect(self, actual: Any, expected: Any, label: str) -> None:
        if actual != expected:
            raise ConflictError(
                f"{label.capitalize()} changed after the plan was created",
                details={"expected": expected, "actual": actual},
            )

    def _event(
        self, journal: ChangeJournal, stage: str, status: str, details: dict[str, Any]
    ) -> None:
        journal.events.append(
            JournalEvent(at=datetime.now(UTC), stage=stage, status=status, details=details)
        )
