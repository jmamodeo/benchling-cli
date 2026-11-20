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
        plan = self.repository.load_plan(plan_id)
        submitted_tables: set[str] = set()
        for event in journal.events:
            table_id = event.details.get("table_id")
            if (
                event.stage == "submit_results"
                and event.status in {"succeeded", "unknown"}
                and isinstance(table_id, str)
            ):
                submitted_tables.add(table_id)
        next_step = pending_results_next_step(plan, submitted_tables or None)
        exc.details = {
            **exc.details,
            "plan_id": plan_id,
            "journal": journal.model_dump(mode="json"),
            **({"next_step": next_step} if next_step else {}),
        }

    def _record_error(self, plan_id: str, journal: ChangeJournal, exc: BenchlingAgentError) -> None:
        """Record a failed or partial apply; raises PartialCompletionError if a step had succeeded."""
        if any(event.status == "succeeded" for event in journal.events if event.stage != "apply"):
            journal.status = PlanStatus.PARTIAL
            self._event(journal, "apply", "partial", _failure_details(exc))
            self.repository.save_journal(journal)
            plan = self.repository.load_plan(plan_id)
            submitted_tables: set[str] = set()
            for event in journal.events:
                table_id = event.details.get("table_id")
                if (
                    event.stage == "submit_results"
                    and event.status == "succeeded"
                    and isinstance(table_id, str)
                ):
                    submitted_tables.add(table_id)
            next_step = pending_results_next_step(plan, submitted_tables or None)
            raise PartialCompletionError(
                "Some plan steps completed before a later step failed",
                details={
                    "plan_id": plan_id,
                    "cause": exc.message,
                    "journal": journal.model_dump(mode="json"),
                    **({"next_step": next_step} if next_step else {}),
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

    async def status(
        self, plan_id: str, *, wait: bool = False, timeout: float = 300.0
    ) -> dict[str, Any]:
        """Return the plan and journal; for submitted results, also reconcile the remote task.

        Remote lookup failures are reported under remote_task_error, not raised.
        """
        if not isinstance(wait, bool):
            raise ValidationError("wait must be a boolean")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0.1
            or timeout > 3600
        ):
            raise ValidationError("timeout must be greater than 0 and at most 3600 seconds")
        plan = self.repository.load_plan(plan_id)
        self._check_plan_scope(plan)
        journal = self.repository.load_journal(plan_id)
        status: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "journal": journal.model_dump(mode="json"),
        }
        tasks = self._result_tasks(journal)
        if plan.family == PlanFamily.RESULTS and tasks:
            deadline = asyncio.get_running_loop().time() + timeout
            results = []
            updates = []
            for task in tasks:
                task_result = await self._poll_result_task(
                    task, wait=wait, deadline=deadline
                )
                results.append(task_result)
                if "remote_task" in task_result:
                    updates.append(task_result)
            status["tasks"] = results
            if len(results) == 1:
                status["remote_task"] = results[0].get("remote_task")
                status["result_state"] = results[0].get("result_state", "unknown")
                status["record_ids"] = results[0].get("record_ids", [])
                status["record_v3_ids"] = results[0].get("record_v3_ids", [])
                if "remote_task_error" in results[0]:
                    status["remote_task_error"] = results[0]["remote_task_error"]
            status.update(self._row_errors_by_table(results))
            if updates:
                lock = FileLock(
                    str(self.repository.config.locks_dir / f"plan-{plan_id}.apply.lock"),
                    thread_local=False,
                )
                await asyncio.to_thread(lock.acquire, timeout=30)
                try:
                    latest = self.repository.load_journal(plan_id)
                    if latest.result is None:
                        latest.result = {}
                    known = latest.result.get("batches")
                    if not isinstance(known, list):
                        known = []
                    by_task = {
                        item.get("task_id"): item for item in known if isinstance(item, dict)
                    }
                    for update in updates:
                        task_id = update["task_id"]
                        old = by_task.get(task_id, {})
                        by_task[task_id] = {
                            **old,
                            "batch_index": update["batch_index"],
                            "table_id": update["table_id"],
                            "task_id": task_id,
                            "plan_row_offset": update.get("plan_row_offset", 0),
                            "record_ids": update.get("record_ids", []),
                            "record_v3_ids": update.get("record_v3_ids", []),
                            "task_status": update.get("task_status"),
                            "state": update.get("result_state", "unknown"),
                            "row_errors": update.get("row_errors", []),
                            "unmatched_errors": update.get("unmatched_errors", []),
                        }
                    latest.result["batches"] = list(by_task.values())
                    if len(tasks) == 1 and latest.status == PlanStatus.SUCCEEDED:
                        only = by_task[tasks[0]["task_id"]]
                        latest.result.update(
                            {
                                "record_ids": only.get("record_ids", []),
                                "record_v3_ids": only.get("record_v3_ids", []),
                                "task_status": only.get("task_status"),
                                "state": only.get("state"),
                            }
                        )
                    self.repository.save_journal(latest)
                    status["journal"] = latest.model_dump(mode="json")
                finally:
                    lock.release()
        return status

    def _result_tasks(self, journal: ChangeJournal) -> list[dict[str, Any]]:
        """Find task submissions in append-only events, with support for older journals."""
        tasks = []
        prior_result = journal.result or {}
        for index, event in enumerate(journal.events):
            if (
                event.stage != "submit_results"
                or event.status != "succeeded"
                or not event.details.get("task_id")
            ):
                continue
            tasks.append(
                {
                    **event.details,
                    "batch_index": event.details.get("batch_index", index),
                    "table_id": event.details.get("table_id", prior_result.get("table_id")),
                    "client_record_ids": event.details.get(
                        "client_record_ids", prior_result.get("client_record_ids", [])
                    ),
                    "plan_row_offset": event.details.get("plan_row_offset", 0),
                }
            )
        if tasks:
            return tasks
        task_id = (journal.result or {}).get("task_id")
        if task_id:
            return [
                {
                    "task_id": task_id,
                    "plan_row_offset": (journal.result or {}).get("plan_row_offset", 0),
                    "table_id": (journal.result or {}).get("table_id"),
                    "batch_index": 0,
                    "client_record_ids": (journal.result or {}).get("client_record_ids", []),
                    "row_count": len((journal.result or {}).get("client_record_ids", [])),
                }
            ]
        batches = (journal.result or {}).get("batches")
        if isinstance(batches, list):
            return [batch for batch in batches if isinstance(batch, dict) and batch.get("task_id")]
        return []

    async def _poll_result_task(
        self, task: dict[str, Any], *, wait: bool, deadline: float
    ) -> dict[str, Any]:
        """Read a task until it is terminal or the caller's wait deadline expires."""
        task_id = task["task_id"]
        result: dict[str, Any] = {
            "batch_index": task.get("batch_index", 0),
            "table_id": task.get("table_id"),
            "task_id": task_id,
        }
        delay = 0.25
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if wait and remaining <= 0:
                    result["timed_out"] = True
                    result["result_state"] = "submission_running"
                    break
                try:
                    request = self.service.get_task(task_id)
                    remote = (
                        await asyncio.wait_for(request, timeout=remaining)
                        if wait
                        else await request
                    )
                except TimeoutError:
                    result["timed_out"] = True
                    result["result_state"] = "submission_running"
                    break
                remote_status: Any = remote.get("status")
                result.update(
                    {
                        "remote_task": remote,
                        "task_status": remote_status,
                        "result_state": {
                            "RUNNING": "submission_running",
                            "FAILED": "submission_failed",
                            "SUCCEEDED": "pending_integration",
                        }.get(remote_status, "unknown"),
                    }
                )
                if not wait or remote_status != "RUNNING":
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result["timed_out"] = True
                    break
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 5.0)
        except BenchlingAgentError as exc:
            result["remote_task_error"] = {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
            }
            return result
        records = (result.get("remote_task", {}).get("response") or {}).get("assayResults", [])
        result["record_ids"] = [
            record.get("id")
            for record in records
            if isinstance(record, dict) and record.get("id")
        ] if isinstance(records, list) else []
        result["record_v3_ids"] = [
            record.get("v3Id")
            for record in records
            if isinstance(record, dict) and record.get("v3Id")
        ] if isinstance(records, list) else []
        if result.get("task_status") == "FAILED":
            row_errors, unmatched_errors = self._map_result_errors(
                result["remote_task"].get("errors"),
                task.get("client_record_ids", []),
                task.get("plan_row_offset", 0),
            )
            result["row_errors"] = row_errors
            result["unmatched_errors"] = unmatched_errors
        return result

    def _map_result_errors(
        self, errors: Any, client_record_ids: Any, plan_row_offset: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Map only the documented bulk-task ``index`` field; preserve other raw errors."""
        ids = client_record_ids if isinstance(client_record_ids, list) else []
        if isinstance(errors, list):
            entries = errors
        elif isinstance(errors, dict):
            entries = [errors]
        else:
            entries = [] if errors is None else [errors]
        mapped = []
        unmatched = []
        for error in entries:
            index = error.get("index") if isinstance(error, dict) else None
            if (
                isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(ids)
            ):
                mapped.append(
                    {
                        "table_id": None,
                        "row_index": index,
                        "plan_row_index": plan_row_offset + index
                        if isinstance(plan_row_offset, int) and not isinstance(plan_row_offset, bool)
                        else index,
                        "client_record_id": ids[index],
                        "error": error,
                    }
                )
            else:
                unmatched.append(
                    {
                        "error": error,
                        "reason": "Task error has no usable input row index",
                    }
                )
        return mapped, unmatched

    def _row_errors_by_table(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Add each table ID to row-level errors and report unmatched task errors."""
        rows: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        for result in results:
            table_id = result.get("table_id")
            rows.extend(
                {**error, "table_id": table_id}
                for error in result.get("row_errors", [])
            )
            unmatched.extend(
                {**error, "table_id": table_id}
                for error in result.get("unmatched_errors", [])
            )
        return {"row_errors": rows, "unmatched_row_errors": unmatched}
