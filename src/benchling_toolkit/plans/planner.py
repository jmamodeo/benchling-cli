"""Plan/apply orchestration: builds checksummed change plans and applies them with a journal."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, NoReturn

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from ..errors import (
    BenchlingAgentError,
    ConflictError,
    PartialCompletionError,
    UpstreamError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
    PlanFamily,
    PlanStatus,
)
from .document import DocumentPlanning
from .fields import FieldsPlanning
from .repository import checksum_for, json_diff
from .result_update import request_is_result_update
from .results import ResultsPlanning, pending_results_next_step
from .table import TablePlanning


def _with_http_status(details: dict[str, Any]) -> dict[str, Any]:
    """Copy error details and record the raw HTTP status when the API returned one."""
    recorded = dict(details)
    status = recorded.get("status")
    if isinstance(status, int) and "http_status" not in recorded:
        recorded["http_status"] = status
    return recorded


def _failure_details(exc: BenchlingAgentError) -> dict[str, Any]:
    """Journal details for a failed apply step, including the HTTP status when known."""
    details: dict[str, Any] = {"error": exc.message}
    status = exc.details.get("status")
    if isinstance(status, int):
        details["http_status"] = status
    return details


class ChangePlanner(DocumentPlanning, TablePlanning, FieldsPlanning, ResultsPlanning):
    """Creates change plans and applies them under a per-plan file lock."""

    async def plan(
        self, reference: str, family: PlanFamily | str, request: dict[str, Any]
    ) -> ChangePlan:
        """Build and persist a checksummed plan without writing to the entry.

        Raises ValidationError for an unknown family or invalid request.
        """
        try:
            family = PlanFamily(family)
        except ValueError as exc:
            raise ValidationError(
                "Unknown change family",
                details={"family": str(family), "supported": [item.value for item in PlanFamily]},
            ) from exc
        request = deepcopy(request)
        if family != PlanFamily.RESULTS and request_is_result_update(request):
            raise ValidationError(
                "CSV result updates require the results family",
                details={"family": family.value},
            )
        resolved = await self.service.resolve_entry(reference)
        entry_id = resolved["entry_id"]
        planners = {
            PlanFamily.DOCUMENT: self._plan_document,
            PlanFamily.TABLE: self._plan_table,
            PlanFamily.FIELDS: self._plan_fields,
            PlanFamily.RESULTS: self._plan_results,
        }
        before, after, preconditions, guarantees, warnings = await planners[family](
            entry_id, request
        )
        plan_data: dict[str, Any] = {
            "schema_version": "1.0",
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "env": self.api.environment.name,
            "tenant_url": self.api.environment.base_url,
            "entry_id": entry_id,
            "family": family.value,
            "request": request,
            "before": before,
            "after": after,
            "diff": json_diff(before, after),
            "preconditions": preconditions,
            "guarantees": guarantees,
            "warnings": warnings,
        }
        # Hash the normalized Pydantic representation so datetimes and enums
        # verify identically after the plan is read from disk.
        plan_data["checksum"] = ""
        plan = ChangePlan.model_validate(plan_data)
        plan.checksum = checksum_for(plan.model_dump(mode="json"))
        self.repository.save_plan(plan)
        return plan

    async def apply(self, plan_id: str) -> dict[str, Any]:
        """Apply a stored plan while holding its lock.

        Raises ConflictError if another process holds the lock for 30 seconds.
        """
        self.repository._validate_id(plan_id)
        self.repository.config.ensure()
        lock = FileLock(
            str(self.repository.config.locks_dir / f"plan-{plan_id}.apply.lock"),
            thread_local=False,
        )
        try:
            await asyncio.to_thread(lock.acquire, timeout=30)
        except FileLockTimeout as exc:
            raise ConflictError(
                "Another process is applying this plan",
                details={"plan_id": plan_id},
            ) from exc
        try:
            return await self._apply_locked(plan_id)
        finally:
            lock.release()

    def _check_plan_scope(self, plan: ChangePlan) -> None:
        """Reject plans created for a different environment or tenant."""
        if (
            plan.env != self.api.environment.name
            or plan.tenant_url != self.api.environment.base_url
        ):
            raise ValidationError(
                "Plan belongs to a different environment or tenant",
                details={"plan_env": plan.env, "active_env": self.api.environment.name},
            )

    async def _run_family_apply(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        """Dispatch to the apply routine for the plan's family."""
        appliers = {
            PlanFamily.DOCUMENT: self._apply_document,
            PlanFamily.TABLE: self._apply_table,
            PlanFamily.FIELDS: self._apply_fields,
            PlanFamily.RESULTS: self._apply_results,
        }
        return await appliers[plan.family](plan, journal)

    async def _apply_locked(self, plan_id: str) -> dict[str, Any]:
        plan = self.repository.load_plan(plan_id)
        self._check_plan_scope(plan)
        journal = self.repository.load_journal(plan_id)
        if journal.status != PlanStatus.PENDING:
            raise ConflictError(
                "Plan has already been applied or attempted",
                details={"plan_id": plan_id, "status": journal.status.value},
            )
        self._event(journal, "apply", "started", {"family": plan.family.value})
        journal.status = PlanStatus.APPLYING
        self.repository.save_journal(journal)
        try:
            result = await self._run_family_apply(plan, journal)
            return self._finalize_success(plan, journal, result)
        except PartialCompletionError as exc:
            self._record_unknown(plan_id, journal, exc)
            raise
        except BenchlingAgentError as exc:
            self._record_error(plan_id, journal, exc)
            raise
        except Exception as exc:
            self._raise_local_failure(plan_id, journal, exc)

    def _finalize_success(
        self, plan: ChangePlan, journal: ChangeJournal, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Mark the journal succeeded and build the apply response."""
        journal.status = PlanStatus.SUCCEEDED
        journal.result = result
        self._event(journal, "apply", "succeeded", result)
        self.repository.save_journal(journal)
        response: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "journal": journal.model_dump(mode="json"),
        }
        next_step = pending_results_next_step(plan)
        if next_step:
            response["next_step"] = next_step
        return response

    def _record_unknown(
        self, plan_id: str, journal: ChangeJournal, exc: PartialCompletionError
    ) -> None:
        """Record an unknown outcome and attach plan and journal to the error details."""
        journal.status = PlanStatus.UNKNOWN
        self._event(journal, "apply", "unknown", _with_http_status(exc.details))
        self.repository.save_journal(journal)
        exc.details = {
            **exc.details,
            "plan_id": plan_id,
            "journal": journal.model_dump(mode="json"),
        }

    def _record_error(self, plan_id: str, journal: ChangeJournal, exc: BenchlingAgentError) -> None:
        """Record a failed or partial apply; raises PartialCompletionError if a step had succeeded."""
        if any(event.status == "succeeded" for event in journal.events if event.stage != "apply"):
            journal.status = PlanStatus.PARTIAL
            self._event(journal, "apply", "partial", _failure_details(exc))
            self.repository.save_journal(journal)
            raise PartialCompletionError(
                "Some plan steps completed before a later step failed",
                details={
                    "plan_id": plan_id,
                    "cause": exc.message,
                    "journal": journal.model_dump(mode="json"),
                },
            ) from exc
        journal.status = PlanStatus.FAILED
        self._event(journal, "apply", "failed", _failure_details(exc))
        self.repository.save_journal(journal)

    def _raise_local_failure(
        self, plan_id: str, journal: ChangeJournal, exc: Exception
    ) -> NoReturn:
        """Record an unexpected local failure and raise the wrapped error."""
        has_completed_step = any(
            event.status == "succeeded" for event in journal.events if event.stage != "apply"
        )
        journal.status = PlanStatus.PARTIAL if has_completed_step else PlanStatus.FAILED
        self._event(
            journal,
            "apply",
            journal.status.value,
            {"error": type(exc).__name__},
        )
        self.repository.save_journal(journal)
        wrapped = UpstreamError(
            "An unexpected local failure interrupted plan execution",
            details={
                "plan_id": plan_id,
                "type": type(exc).__name__,
                "journal": journal.model_dump(mode="json"),
            },
        )
        if has_completed_step:
            raise PartialCompletionError(
                "A local failure occurred after at least one write completed",
                details=wrapped.details,
            ) from exc
        raise wrapped from exc

    async def status(self, plan_id: str) -> dict[str, Any]:
        """Return the plan and journal; for submitted results, also reconcile the remote task.

        Remote lookup failures are reported under remote_task_error, not raised.
        """
        plan = self.repository.load_plan(plan_id)
        self._check_plan_scope(plan)
        journal = self.repository.load_journal(plan_id)
        status: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "journal": journal.model_dump(mode="json"),
        }
        task_id = (journal.result or {}).get("task_id")
        if plan.family == PlanFamily.RESULTS and task_id:
            try:
                remote = await self.service.get_task(task_id)
                status["remote_task"] = remote
                remote_status: Any = remote.get("status")
                status["result_state"] = {
                    "RUNNING": "submission_running",
                    "FAILED": "submission_failed",
                    "SUCCEEDED": "pending_integration",
                }.get(remote_status, "unknown")
                records = (remote.get("response") or {}).get("assayResults", [])
                confirmed_ids = [
                    record.get("id")
                    for record in records
                    if isinstance(record, dict) and record.get("id")
                ]
                confirmed_v3_ids = [
                    record.get("v3Id")
                    for record in records
                    if isinstance(record, dict) and record.get("v3Id")
                ]
                status["record_ids"] = confirmed_ids
                status["record_v3_ids"] = confirmed_v3_ids
                # Retain server-assigned IDs for reconciliation after task data
                # expires. Use the same lock as apply to avoid lost updates.
                lock = FileLock(
                    str(self.repository.config.locks_dir / f"plan-{plan_id}.apply.lock"),
                    thread_local=False,
                )
                await asyncio.to_thread(lock.acquire, timeout=30)
                try:
                    latest = self.repository.load_journal(plan_id)
                    if latest.status == PlanStatus.SUCCEEDED and latest.result:
                        latest.result.update(
                            {
                                "record_ids": confirmed_ids,
                                "record_v3_ids": confirmed_v3_ids,
                                "task_status": remote_status,
                                "state": status["result_state"],
                            }
                        )
                        self.repository.save_journal(latest)
                        status["journal"] = latest.model_dump(mode="json")
                finally:
                    lock.release()
            except BenchlingAgentError as exc:
                status["remote_task_error"] = {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
        return status
