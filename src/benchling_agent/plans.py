from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .api import BenchlingAPI
from .config import ConfigStore
from .errors import (
    BenchlingAgentError,
    ConflictError,
    PartialCompletionError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from .models import (
    ChangeJournal,
    ChangePlan,
    JournalEvent,
    PlanFamily,
    PlanStatus,
)
from .services import (
    BenchlingService,
    entry_version,
    field_definition_type,
    part_type,
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


class ChangePlanner:
    def __init__(
        self,
        service: BenchlingService,
        api: BenchlingAPI,
        repository: PlanRepository,
    ) -> None:
        self.service = service
        self.api = api
        self.repository = repository

    async def plan(
        self, reference: str, family: PlanFamily | str, request: dict[str, Any]
    ) -> ChangePlan:
        try:
            family = PlanFamily(family)
        except ValueError as exc:
            raise ValidationError(
                "Unknown change family",
                details={"family": str(family), "supported": [item.value for item in PlanFamily]},
            ) from exc
        request = deepcopy(request)
        resolved = await self.service.resolve_entry(reference)
        entry_id = resolved["entry_id"]
        if family == PlanFamily.DOCUMENT:
            before, after, preconditions, guarantees, warnings = await self._plan_document(
                entry_id, request
            )
        elif family == PlanFamily.TABLE:
            before, after, preconditions, guarantees, warnings = await self._plan_table(
                entry_id, request
            )
        elif family == PlanFamily.FIELDS:
            before, after, preconditions, guarantees, warnings = await self._plan_fields(
                entry_id, request
            )
        else:
            before, after, preconditions, guarantees, warnings = await self._plan_results(
                entry_id, request
            )
        plan_data: dict[str, Any] = {
            "schema_version": "1.0",
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "profile": self.api.profile.name,
            "tenant_url": self.api.profile.base_url,
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

    async def _apply_locked(self, plan_id: str) -> dict[str, Any]:
        plan = self.repository.load_plan(plan_id)
        if plan.profile != self.api.profile.name or plan.tenant_url != self.api.profile.base_url:
            raise ValidationError(
                "Plan belongs to a different profile or tenant",
                details={"plan_profile": plan.profile, "active_profile": self.api.profile.name},
            )
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
            if plan.family == PlanFamily.DOCUMENT:
                result = await self._apply_document(plan, journal)
            elif plan.family == PlanFamily.TABLE:
                result = await self._apply_table(plan, journal)
            elif plan.family == PlanFamily.FIELDS:
                result = await self._apply_fields(plan, journal)
            else:
                result = await self._apply_results(plan, journal)
            journal.status = PlanStatus.SUCCEEDED
            journal.result = result
            self._event(journal, "apply", "succeeded", result)
            self.repository.save_journal(journal)
            return {"plan": plan.model_dump(mode="json"), "journal": journal.model_dump(mode="json")}
        except PartialCompletionError as exc:
            journal.status = PlanStatus.UNKNOWN
            self._event(journal, "apply", "unknown", exc.details)
            self.repository.save_journal(journal)
            exc.details = {
                **exc.details,
                "plan_id": plan_id,
                "journal": journal.model_dump(mode="json"),
            }
            raise
        except BenchlingAgentError as exc:
            if any(event.status == "succeeded" for event in journal.events if event.stage != "apply"):
                journal.status = PlanStatus.PARTIAL
                self._event(journal, "apply", "partial", {"error": exc.message})
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
            self._event(journal, "apply", "failed", {"error": exc.message})
            self.repository.save_journal(journal)
            raise
        except Exception as exc:
            has_completed_step = any(
                event.status == "succeeded"
                for event in journal.events
                if event.stage != "apply"
            )
            journal.status = (
                PlanStatus.PARTIAL if has_completed_step else PlanStatus.FAILED
            )
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
        plan = self.repository.load_plan(plan_id)
        if plan.profile != self.api.profile.name or plan.tenant_url != self.api.profile.base_url:
            raise ValidationError(
                "Plan belongs to a different profile or tenant",
                details={"plan_profile": plan.profile, "active_profile": self.api.profile.name},
            )
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
                remote_status = remote.get("status")
                status["result_state"] = {
                    "RUNNING": "submission_running",
                    "FAILED": "submission_failed",
                    "SUCCEEDED": "pending_integration",
                }.get(remote_status, "unknown")
                records = (remote.get("response") or {}).get("assayResults", [])
                confirmed_ids = [record.get("id") for record in records if isinstance(record, dict) and record.get("id")]
                confirmed_v3_ids = [record.get("v3Id") for record in records if isinstance(record, dict) and record.get("v3Id")]
                status["record_ids"] = confirmed_ids
                status["record_v3_ids"] = confirmed_v3_ids
                # Retain server-assigned IDs for reconciliation after task data
                # expires. Use the same lock as apply to avoid lost updates.
                lock = FileLock(str(self.repository.config.locks_dir / f"plan-{plan_id}.apply.lock"), thread_local=False)
                await asyncio.to_thread(lock.acquire, timeout=30)
                try:
                    latest = self.repository.load_journal(plan_id)
                    if latest.status == PlanStatus.SUCCEEDED and latest.result:
                        latest.result.update({
                            "record_ids": confirmed_ids,
                            "record_v3_ids": confirmed_v3_ids,
                            "task_status": remote_status,
                            "state": status["result_state"],
                        })
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

    async def _plan_document(self, entry_id: str, request: dict[str, Any]):
        snapshot = await self.service.get_entry(entry_id, full=True)
        operation = request.get("operation")
        parts = snapshot["parts"]
        by_id = {part.get("id"): part for part in parts}
        if operation == "insert":
            new_parts = request.get("parts")
            position = request.get("position", "end_of_document")
            if not isinstance(new_parts, list) or not new_parts:
                raise ValidationError("Document insert requires a non-empty parts list")
            if any(
                not isinstance(part, dict) or not isinstance(part.get("contentType"), str)
                for part in new_parts
            ):
                raise ValidationError(
                    "Every inserted document part requires a contentType"
                )
            if position not in {"beginning_of_document", "end_of_document"} and position not in by_id:
                raise ValidationError("Insert position is not a document part")
            before = {"version_id": snapshot["version_id"], "position": position, "parts": []}
            after = {"version_id": "changes_on_apply", "position": position, "parts": new_parts}
        elif operation == "delete":
            first = request.get("from_part_id")
            last = request.get("to_part_id", first)
            selected = self._part_range(parts, first, last)
            before = {"version_id": snapshot["version_id"], "parts": selected}
            after = {"version_id": "changes_on_apply", "parts": []}
        elif operation == "move":
            first = request.get("from_part_id")
            last = request.get("to_part_id", first)
            selected = self._part_range(parts, first, last)
            destination = request.get("position")
            if destination not in {"beginning_of_document", "end_of_document"} and destination not in by_id:
                raise ValidationError("Move destination is not a document part")
            if destination in {part.get("id") for part in selected}:
                raise ValidationError("Move destination cannot be inside the source range")
            before = {"version_id": snapshot["version_id"], "parts": selected}
            after = {"version_id": "changes_on_apply", "position": destination, "parts": selected}
        elif operation == "replace_text":
            part_id = request.get("part_id")
            original = by_id.get(part_id)
            if not original:
                raise ValidationError("Text replacement target was not found")
            if part_type(original).split(".")[-1] != "TextDocumentContent":
                raise UnsupportedError("replace_text only supports text document parts")
            if not isinstance(request.get("text"), str):
                raise ValidationError("replace_text requires text")
            before = {"version_id": snapshot["version_id"], "part": original}
            content = original.get("content") or {}
            replacement = {
                "contentType": "text",
                "text": request["text"],
                "formats": request.get("formats", content.get("formats", [])),
                "links": request.get("links", content.get("links", [])),
                "mentions": request.get("mentions", content.get("mentions", [])),
                "indentation": request.get("indentation", content.get("indentation", 0)),
            }
            after = {"version_id": "changes_on_apply", "part": replacement}
        else:
            raise ValidationError(
                "Unknown document operation",
                details={"supported": ["insert", "delete", "move", "replace_text"]},
            )
        return (
            before,
            after,
            {
                "entry_version": snapshot["version_id"],
                "part_ids": [part.get("id") for part in parts if part.get("id")],
            },
            ["server-enforced document version check"],
            (["Text replacement requires two versioned requests and can partially complete"] if operation == "replace_text" else []),
        )

    async def _plan_table(self, entry_id: str, request: dict[str, Any]):
        part_id = request.get("table_part_id")
        if not isinstance(part_id, str):
            raise ValidationError("Table plan requires table_part_id")
        target = await self.service.get_table_for_entry(entry_id, part_id)
        table = target["table"]
        content = deepcopy(table.get("content") or {})
        if "UnstructuredTable" not in str(content.get("__typename", "")):
            raise UnsupportedError("Only unstructured tables can be edited")
        before = {"table_part_id": part_id, "version_id": table.get("versionId"), "content": content}
        after_content = deepcopy(content)
        operation = request.get("operation")
        if operation == "set_cells":
            updates = request.get("updates")
            if not isinstance(updates, list) or not updates:
                raise ValidationError("set_cells requires non-empty updates")
            rows = after_content.get("rows") or []
            seen_coordinates: set[tuple[int, int]] = set()
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or "value" not in update
                    or isinstance(update.get("row"), bool)
                    or not isinstance(update.get("row"), int)
                    or isinstance(update.get("column"), bool)
                    or not isinstance(update.get("column"), int)
                    or update["row"] < 0
                    or update["column"] < 0
                ):
                    raise ValidationError(
                        "Each cell update requires nonnegative integer row and column coordinates and a value",
                        details={"update": update},
                    )
                coordinate = (update["row"], update["column"])
                if coordinate in seen_coordinates:
                    raise ValidationError(
                        "A table plan cannot update the same cell twice",
                        details={"row": coordinate[0], "column": coordinate[1]},
                    )
                seen_coordinates.add(coordinate)
                try:
                    row = update["row"]
                    column = update["column"]
                    cell = rows[row]["cells"][column]
                except (KeyError, TypeError, IndexError) as exc:
                    raise ValidationError(
                        "Cell coordinate is outside the table",
                        details={"update": update},
                    ) from exc
                if cell.get("mergeRangeParent"):
                    raise UnsupportedError("Cannot write directly to a merged child cell")
                cell["inputValue"] = {
                    "type": "text_value",
                    "value": str(update.get("value", "")),
                }
                cell.pop("evaluatedValue", None)
        elif operation == "append_rows":
            rows = request.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValidationError("append_rows requires non-empty rows")
            expected = len(after_content.get("columnDefinitions") or [])
            normalized = [self._normalize_row(row, expected) for row in rows]
            after_content.setdefault("rows", []).extend(normalized)
        elif operation == "replace":
            replacement = request.get("table")
            if not isinstance(replacement, dict):
                raise ValidationError("replace requires a table object")
            after_content = deepcopy(content)
            after_content.update(deepcopy(replacement))
            if "rows" in replacement:
                self._preserve_table_cell_state(
                    content.get("rows") or [], after_content.get("rows") or []
                )
            expected = len(after_content.get("columnDefinitions") or [])
            after_content["rows"] = [
                self._normalize_row(row, expected) for row in after_content.get("rows", [])
            ]
        else:
            raise ValidationError(
                "Unknown table operation",
                details={"supported": ["set_cells", "append_rows", "replace"]},
            )
        expected = len(after_content.get("columnDefinitions") or [])
        after_content["rows"] = [
            self._normalize_row(row, expected) for row in after_content.get("rows", [])
        ]
        for row in after_content["rows"]:
            for cell in row["cells"]:
                cell.pop("evaluatedValue", None)
        after_content["isEvaluated"] = False
        self._validate_table_content(after_content)
        after = {"table_part_id": part_id, "version_id": "changes_on_apply", "content": after_content}
        return (
            before,
            after,
            {"table_version": table.get("versionId"), "entry_id": entry_id},
            ["server-enforced document version check", "complete table replacement"],
            ["API table edits clear evaluated values until Benchling evaluates the table in the browser"],
        )

    async def _plan_fields(self, entry_id: str, request: dict[str, Any]):
        updates = request.get("schema_fields")
        if not isinstance(updates, dict) or not updates:
            raise ValidationError("Field plan requires non-empty schema_fields")
        current = await self.service.get_entry_fields(entry_id)
        if not current.get("schema"):
            raise UnsupportedError("Entry has no assigned schema")
        definitions = current.get("field_definitions") or []
        normalized, targets = self._normalize_schema_field_updates(updates, definitions)
        request["schema_fields"] = normalized
        fields = current.get("fields_by_system_name") or {}
        before_values = {key: fields.get(key) for key in normalized}
        after_values = {**before_values, **normalized}
        schema_id = (current.get("schema") or {}).get("id")
        return (
            {"fields": before_values, "targets": targets},
            {"fields": after_values, "targets": targets},
            {
                "schema_id": schema_id,
                "observed_values": before_values,
                "field_definitions": self._field_definition_fingerprints(
                    definitions, set(normalized)
                ),
                "modified_at": current.get("modified_at"),
            },
            ["immediate pre-write field-value comparison"],
            ["Entry field updates do not expose an atomic document-version precondition"],
        )

    async def _plan_results(self, entry_id: str, request: dict[str, Any]):
        table_id = request.get("table_id")
        records = request.get("assay_results")
        if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
            raise ValidationError("Result plan requires an explicit V2 result table ID")
        if not isinstance(records, list) or not records:
            raise ValidationError("Result plan requires non-empty assay_results")
        if len(records) > 500:
            raise ValidationError("Benchling accepts at most 500 results in a notebook table upload")
        table = await self.service.get_result_table(entry_id, table_id)
        schema_id = table.get("schema_id")
        if not isinstance(schema_id, str):
            raise UnsupportedError(
                "The selected result table does not expose a result schema",
                details={"table_id": table_id},
            )
        schema = await self.service.get_schema(schema_id)
        definitions = schema.get("field_definitions") or []
        planned = deepcopy(records)
        client_record_ids: list[str] = []
        projects: dict[str, dict[str, Any]] = {}
        for record in planned:
            if not isinstance(record, dict) or not record.get("schemaId") or not isinstance(record.get("fields"), dict):
                raise ValidationError("Each result requires schemaId and fields")
            if record["schemaId"] != schema_id:
                raise ValidationError(
                    "A result record's schema does not match the selected table",
                    details={
                        "table_id": table_id,
                        "expected_schema_id": schema_id,
                        "actual_schema_id": record["schemaId"],
                    },
                )
            normalized, _ = self._normalize_schema_field_updates(
                record["fields"], definitions, require_all_required=True
            )
            record["fields"] = normalized
            if "id" in record:
                raise UnsupportedError(
                    "Notebook table uploads use server-assigned result IDs; omit id"
                )
            # Local correlation IDs are journal metadata, not remote IDs or
            # idempotency keys. This tenant rejects id in bulk-table requests.
            client_record_ids.append(str(uuid.uuid4()))
            project_id = record.get("projectId")
            if project_id is not None:
                if not isinstance(project_id, str) or not project_id:
                    raise ValidationError("projectId must be a non-empty string or null")
                if project_id not in projects:
                    project = await self.service.get_project(project_id)
                    projects[project_id] = {
                        "id": project.get("id", project_id),
                        "name": project.get("name"),
                    }
        before = {"table": table, "records": []}
        after = {
            "table_id": table_id,
            "schema_id": schema_id,
            "projects": projects,
            "records": planned,
            "client_record_ids": client_record_ids,
            "state": "pending_integration",
        }
        warnings = [
            "Created results remain pending until a person integrates them in Benchling",
            "Result IDs are assigned by Benchling; local correlation UUIDs are not idempotency keys",
        ]
        if any(record.get("projectId") is None for record in planned):
            warnings.append(
                "Results without projectId are readable only by their creator until access is changed"
            )
        return (
            before,
            after,
            {
                "entry_id": entry_id,
                "table_id": table_id,
                "result_schema_id": schema_id,
                "result_schema_fields": self._field_definition_fingerprints(
                    definitions
                ),
            },
            [
                "verified entry-to-table and table-to-schema relationships",
                "local correlation UUIDs and persisted task IDs for reconciliation",
                "no automatic write retry",
            ],
            warnings,
        )

    async def _apply_document(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        current = await self.api.request("GET", f"/entry/{plan.entry_id}")
        version = entry_version(current)
        self._expect(version, plan.preconditions["entry_version"], "entry version")
        request = plan.request
        operation = request["operation"]
        if operation == "replace_text":
            target = request["part_id"]
            insertion = {
                "operationType": "insert",
                "version": version,
                "coordinates": {"position": target},
                "parts": [plan.after["part"]],
            }
            first = await self.api.request(
                "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": insertion}
            )
            self._event(journal, "insert_replacement", "succeeded", {"response": first})
            self.repository.save_journal(journal)
            updated = await self.api.request("GET", f"/entry/{plan.entry_id}")
            next_version = entry_version(updated)
            deletion = {
                "operationType": "delete",
                "version": next_version,
                "coordinates": {"fromDocumentPartId": target, "toDocumentPartId": target},
            }
            second = await self.api.request(
                "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": deletion}
            )
            self._event(journal, "delete_original", "succeeded", {"response": second})
            discovered = await self._discover_new_part_ids(
                plan.entry_id, plan.preconditions.get("part_ids", [])
            )
            return {
                "operations": [first, second],
                "changed_part_ids": [target, *discovered["ids"]],
                "part_id_discovery": discovered,
            }

        if operation == "insert":
            mutation = {
                "operationType": "insert",
                "version": version,
                "coordinates": {"position": request.get("position", "end_of_document")},
                "parts": request["parts"],
            }
        elif operation == "delete":
            mutation = {
                "operationType": "delete",
                "version": version,
                "coordinates": {
                    "fromDocumentPartId": request["from_part_id"],
                    "toDocumentPartId": request.get("to_part_id", request["from_part_id"]),
                },
            }
        else:
            mutation = {
                "operationType": "move",
                "version": version,
                "sourceCoordinates": {
                    "fromDocumentPartId": request["from_part_id"],
                    "toDocumentPartId": request.get("to_part_id", request["from_part_id"]),
                },
                "destinationCoordinates": {"position": request["position"]},
            }
        response = await self.api.request(
            "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": mutation}
        )
        self._event(journal, operation, "succeeded", {"response": response})
        if operation == "insert":
            discovered = await self._discover_new_part_ids(
                plan.entry_id, plan.preconditions.get("part_ids", [])
            )
            changed_part_ids = discovered["ids"]
        else:
            discovered = {"complete": True, "ids": []}
            changed_part_ids = [
                part.get("id") for part in plan.before.get("parts", []) if part.get("id")
            ]
        return {
            "operation": operation,
            "response": response,
            "changed_part_ids": changed_part_ids,
            "part_id_discovery": discovered,
        }

    async def _apply_table(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        part_id = plan.after["table_part_id"]
        current = await self.service.get_table(part_id)
        self._expect(current.get("versionId"), plan.preconditions["table_version"], "table version")
        payload = self._table_write_payload(plan.after["content"])
        payload["versionId"] = plan.preconditions["table_version"]
        response = await self.api.request(
            "PATCH", f"/document-part-unstructured-table/{part_id}", json=payload
        )
        self._event(journal, "replace_table", "succeeded", {"response": response})
        return {"table_part_id": part_id, "response": response, "evaluated_values_cleared": True}

    async def _apply_fields(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        current = await self.service.get_entry_fields(plan.entry_id)
        current_schema_id = (current.get("schema") or {}).get("id")
        self._expect(
            current_schema_id,
            plan.preconditions.get("schema_id"),
            "entry schema",
        )
        self._expect(
            self._field_definition_fingerprints(
                current.get("field_definitions") or [],
                set(plan.preconditions["observed_values"]),
            ),
            plan.preconditions.get("field_definitions"),
            "entry field definitions",
        )
        observed = {
            key: (current.get("fields_by_system_name") or {}).get(key)
            for key in plan.preconditions["observed_values"]
        }
        self._expect(observed, plan.preconditions["observed_values"], "entry field values")
        response = await self.api.request(
            "PATCH",
            f"/entry/{plan.entry_id}",
            json={"schemaFields": plan.request["schema_fields"]},
        )
        self._event(journal, "update_fields", "succeeded", {"response": response})
        return {"response": response, "concurrency": "best_effort_value_comparison"}

    async def _apply_results(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        if any("id" in record for record in plan.after["records"]):
            raise ConflictError("This result plan uses an unsupported bulk-table contract; create a new preview")
        table = await self.service.get_result_table(
            plan.entry_id, plan.preconditions["table_id"]
        )
        self._expect(
            table.get("schema_id"),
            plan.preconditions["result_schema_id"],
            "result table schema",
        )
        schema = await self.service.get_schema(plan.preconditions["result_schema_id"])
        self._expect(
            self._field_definition_fingerprints(
                schema.get("field_definitions") or []
            ),
            plan.preconditions.get("result_schema_fields"),
            "result schema fields",
        )
        payload = {
            "tableId": plan.after["table_id"],
            "assayResults": plan.after["records"],
        }
        response = await self.api.request(
            "POST", "/assay-results:bulk-create", version=2, json=payload
        )
        task_id = response.get("taskId") or response.get("id")
        if not task_id:
            raise PartialCompletionError(
                "Benchling accepted the result upload but did not return a task ID",
                details={
                    "response": response,
                    "client_record_ids": plan.after.get("client_record_ids", []),
                },
            )
        result = {
            "response": response,
            "task_id": task_id,
            "record_ids": [],
            "client_record_ids": plan.after.get("client_record_ids", []),
            "state": "submission_pending",
        }
        self._event(journal, "submit_results", "succeeded", result)
        return result

    def _part_range(self, parts: list[dict[str, Any]], first: Any, last: Any) -> list[dict[str, Any]]:
        ids = [part.get("id") for part in parts]
        try:
            start, end = ids.index(first), ids.index(last)
        except ValueError as exc:
            raise ValidationError("Document range contains an unknown part ID") from exc
        if end < start:
            raise ValidationError("Document range is reversed")
        return parts[start : end + 1]

    def _normalize_row(self, row: Any, expected_columns: int) -> dict[str, Any]:
        if isinstance(row, list):
            cells = [
                {"inputValue": {"type": "text_value", "value": str(value)}}
                for value in row
            ]
            row = {"cells": cells}
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
            raise ValidationError("Each table row must contain cells")
        if expected_columns and len(row["cells"]) != expected_columns:
            raise ValidationError(
                "Table row has the wrong number of cells",
                details={"expected": expected_columns, "actual": len(row["cells"])},
            )
        normalized = deepcopy(row)
        for index, cell in enumerate(normalized["cells"]):
            if not isinstance(cell, dict):
                normalized["cells"][index] = {
                    "inputValue": {"type": "text_value", "value": str(cell)}
                }
            elif "inputValue" not in cell:
                cell["inputValue"] = {"type": "text_value", "value": ""}
        return normalized

    def _preserve_table_cell_state(
        self, previous_rows: list[dict[str, Any]], replacement_rows: list[Any]
    ) -> None:
        preserved_keys = {
            "inputValue",
            "format",
            "mergeRange",
            "mergeRangeParent",
            "selectorId",
        }
        for row_index, row in enumerate(replacement_rows):
            if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
                continue
            if row_index >= len(previous_rows):
                continue
            previous_cells = previous_rows[row_index].get("cells") or []
            for column_index, cell in enumerate(row["cells"]):
                if not isinstance(cell, dict) or column_index >= len(previous_cells):
                    continue
                previous_cell = previous_cells[column_index]
                if not isinstance(previous_cell, dict):
                    continue
                for key in preserved_keys:
                    if key not in cell and key in previous_cell:
                        cell[key] = deepcopy(previous_cell[key])

    def _validate_table_content(self, content: dict[str, Any]) -> None:
        columns = content.get("columnDefinitions") or []
        rows = content.get("rows") or []
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValidationError("Table columns and rows must be arrays")
        if any(not isinstance(column, dict) for column in columns):
            raise ValidationError("Every table column definition must be an object")
        width = len(columns)
        for row_index, row in enumerate(rows):
            cells = row.get("cells") if isinstance(row, dict) else None
            if not isinstance(cells, list) or len(cells) != width:
                raise ValidationError(
                    "Every table row must match the number of columns",
                    details={
                        "row": row_index,
                        "expected_columns": width,
                        "actual_columns": len(cells) if isinstance(cells, list) else None,
                    },
                )
            for column_index, cell in enumerate(cells):
                if not isinstance(cell, dict):
                    raise ValidationError("Every table cell must be an object")
                input_value = cell.get("inputValue")
                if (
                    not isinstance(input_value, dict)
                    or input_value.get("type") != "text_value"
                    or not isinstance(input_value.get("value"), str)
                ):
                    raise ValidationError(
                        "Every unstructured-table cell requires a text inputValue",
                        details={"row": row_index, "column": column_index},
                    )
                merge = cell.get("mergeRange")
                if merge is not None:
                    if (
                        not isinstance(merge, dict)
                        or not isinstance(merge.get("mergeRowSpan"), int)
                        or not isinstance(merge.get("mergeColSpan"), int)
                        or merge["mergeRowSpan"] < 1
                        or merge["mergeColSpan"] < 1
                        or row_index + merge["mergeRowSpan"] > len(rows)
                        or column_index + merge["mergeColSpan"] > width
                    ):
                        raise ValidationError(
                            "A table merge range is invalid",
                            details={"row": row_index, "column": column_index},
                        )
                parent = cell.get("mergeRangeParent")
                if parent is not None:
                    if not isinstance(parent, dict):
                        raise ValidationError("A mergeRangeParent must be an object")
                    parent_row, parent_column = parent.get("row"), parent.get("col")
                    if (
                        not isinstance(parent_row, int)
                        or not isinstance(parent_column, int)
                        or parent_row < 0
                        or parent_column < 0
                        or parent_row >= len(rows)
                        or parent_column >= width
                    ):
                        raise ValidationError(
                            "A merged child points outside the table",
                            details={"row": row_index, "column": column_index},
                        )
                    anchor = rows[parent_row]["cells"][parent_column]
                    anchor_range = anchor.get("mergeRange") or {}
                    if not (
                        parent_row <= row_index < parent_row + anchor_range.get("mergeRowSpan", 0)
                        and parent_column
                        <= column_index
                        < parent_column + anchor_range.get("mergeColSpan", 0)
                    ):
                        raise ValidationError(
                            "A merged child does not point to a covering merge anchor",
                            details={"row": row_index, "column": column_index},
                        )
                selector_id = cell.get("selectorId")
                if selector_id is not None and not isinstance(selector_id, str):
                    raise ValidationError("selectorId must be a string or null")

    def _table_write_payload(self, content: dict[str, Any]) -> dict[str, Any]:
        self._validate_table_content(content)
        payload: dict[str, Any] = {}
        if "name" in content:
            payload["name"] = content["name"]
        if "caption" in content:
            caption = content["caption"]
            payload["caption"] = (
                self._select(caption, {"text", "formats", "links", "mentions"})
                if isinstance(caption, dict)
                else caption
            )
        payload["columnDefinitions"] = [
            self._select(column, {"name", "width", "isLocked"})
            for column in content.get("columnDefinitions", [])
        ]
        payload["rows"] = []
        for row in content.get("rows", []):
            output_row = {"cells": []}
            for cell in row["cells"]:
                output_cell: dict[str, Any] = {
                    "inputValue": self._select(cell["inputValue"], {"type", "value"})
                }
                if "format" in cell:
                    value = cell["format"]
                    output_cell["format"] = (
                        self._select(
                            value,
                            {
                                "alignment",
                                "backgroundColor",
                                "foregroundColor",
                                "link",
                                "properties",
                            },
                        )
                        if isinstance(value, dict)
                        else value
                    )
                for key, allowed in (
                    ("mergeRange", {"mergeRowSpan", "mergeColSpan"}),
                    ("mergeRangeParent", {"row", "col"}),
                ):
                    if key in cell:
                        value = cell[key]
                        output_cell[key] = (
                            self._select(value, allowed)
                            if isinstance(value, dict)
                            else value
                        )
                if "selectorId" in cell:
                    output_cell["selectorId"] = cell["selectorId"]
                output_row["cells"].append(output_cell)
            payload["rows"].append(output_row)
        return payload

    def _select(self, value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        return {key: deepcopy(item) for key, item in value.items() if key in allowed}

    def _normalize_schema_field_updates(
        self,
        updates: dict[str, Any],
        definitions: list[dict[str, Any]],
        *,
        require_all_required: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        by_system = {
            str(item["systemName"]): item
            for item in definitions
            if item.get("systemName")
        }
        by_id = {
            str(item["id"]): item for item in definitions if item.get("id")
        }
        if not by_system:
            raise UnsupportedError(
                "The assigned schema did not expose addressable field definitions"
            )
        normalized: dict[str, Any] = {}
        targets: list[dict[str, Any]] = []
        unknown: list[str] = []
        for supplied_key, field_input in updates.items():
            definition = by_system.get(supplied_key) or by_id.get(supplied_key)
            if definition is None:
                unknown.append(supplied_key)
                continue
            system_name = str(definition["systemName"])
            if system_name in normalized:
                raise ValidationError(
                    "The same schema field was supplied more than once",
                    details={"system_name": system_name},
                )
            if not isinstance(field_input, dict) or set(field_input) != {"value"}:
                raise ValidationError(
                    "Each schema field input must contain exactly one value property",
                    details={"field": supplied_key},
                )
            if definition.get("archived"):
                raise UnsupportedError(
                    "Archived schema fields cannot be updated",
                    details={"field": system_name},
                )
            if definition.get("derivationType") not in {None, "DIRECT"}:
                raise UnsupportedError(
                    "Computed or snapshot schema fields cannot be updated directly",
                    details={
                        "field": system_name,
                        "derivation_type": definition.get("derivationType"),
                    },
                )
            self._validate_schema_field_value(definition, field_input["value"])
            normalized[system_name] = deepcopy(field_input)
            targets.append(
                {
                    "field_definition_id": definition.get("id"),
                    "system_name": system_name,
                    "name": definition.get("name"),
                    "type": field_definition_type(definition),
                }
            )
        if unknown:
            raise ValidationError(
                "Schema fields must be addressed by system name or field-definition ID",
                details={
                    "unknown_fields": unknown,
                    "available_system_names": sorted(by_system),
                    "available_field_ids": sorted(by_id),
                },
            )
        if require_all_required:
            missing_required = sorted(
                str(item["systemName"])
                for item in definitions
                if item.get("systemName")
                and item.get("isRequired")
                and not item.get("archived")
                and item.get("derivationType") in {None, "DIRECT"}
                and str(item["systemName"]) not in normalized
            )
            if missing_required:
                raise ValidationError(
                    "Result records must provide every required schema field",
                    details={"missing_required_fields": missing_required},
                )
        return normalized, targets

    def _field_definition_fingerprints(
        self,
        definitions: list[dict[str, Any]],
        system_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        fingerprints = []
        for definition in definitions:
            system_name = definition.get("systemName")
            if not system_name or (
                system_names is not None and system_name not in system_names
            ):
                continue
            fingerprints.append(
                {
                    "id": definition.get("id"),
                    "system_name": system_name,
                    "type": field_definition_type(definition),
                    "is_multi": bool(definition.get("isMulti")),
                    "is_required": bool(definition.get("isRequired")),
                    "archived": bool(definition.get("archived")),
                    "derivation_type": definition.get("derivationType"),
                    "numeric_min": definition.get("numericMin"),
                    "numeric_max": definition.get("numericMax"),
                }
            )
        return sorted(fingerprints, key=lambda item: str(item["system_name"]))

    def _validate_schema_field_value(
        self, definition: dict[str, Any], value: Any
    ) -> None:
        field = str(definition.get("systemName") or definition.get("id"))
        if value is None:
            if definition.get("isRequired"):
                raise ValidationError(
                    "A required schema field cannot be cleared", details={"field": field}
                )
            return
        field_type = field_definition_type(definition)
        is_multi = bool(definition.get("isMulti"))
        values = value if is_multi else [value]
        if is_multi and not isinstance(value, list):
            raise ValidationError(
                "A multi-value schema field requires an array", details={"field": field}
            )
        if field_type in {"TEXT", "LONG_TEXT"}:
            valid = all(isinstance(item, str) for item in values)
        elif field_type == "INTEGER":
            valid = all(isinstance(item, int) and not isinstance(item, bool) for item in values)
        elif field_type == "FLOAT":
            valid = all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in values
            )
        elif field_type == "BOOLEAN":
            valid = all(isinstance(item, bool) for item in values)
        elif field_type == "DATE":
            valid = all(self._is_date(item) for item in values)
        elif field_type == "DATETIME":
            valid = all(self._is_datetime(item) for item in values)
        elif field_type == "JSON":
            valid = True
        elif field_type in {"LINK", "DROPDOWN"}:
            valid = all(isinstance(item, str) and bool(item) for item in values)
        else:
            raise UnsupportedError(
                "This schema field type cannot be safely validated",
                details={"field": field, "type": field_type},
            )
        if not valid:
            raise ValidationError(
                "Schema field value has the wrong type",
                details={"field": field, "type": field_type, "is_multi": is_multi},
            )
        if field_type in {"INTEGER", "FLOAT"}:
            minimum = definition.get("numericMin")
            maximum = definition.get("numericMax")
            if minimum is not None and any(item < minimum for item in values):
                raise ValidationError(
                    "Schema field value is below its minimum",
                    details={"field": field, "minimum": minimum},
                )
            if maximum is not None and any(item > maximum for item in values):
                raise ValidationError(
                    "Schema field value is above its maximum",
                    details={"field": field, "maximum": maximum},
                )

    def _is_date(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def _is_datetime(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    async def _discover_new_part_ids(
        self, entry_id: str, previous_ids: list[str]
    ) -> dict[str, Any]:
        try:
            current = [
                item.get("id")
                async for item in self.api.paginate(
                    f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
                )
                if item.get("id")
            ]
            previous = set(previous_ids)
            return {"complete": True, "ids": [item for item in current if item not in previous]}
        except BenchlingAgentError as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": "local_discovery_error", "type": type(exc).__name__},
            }

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
