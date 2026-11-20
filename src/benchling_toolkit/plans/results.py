"""Result-record submission planning."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from ..errors import (
    BenchlingAgentError,
    ConflictError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from .base import PlanParts, write_event_details
from .result_update import ResultUpdatePlanning, request_is_result_update

PENDING_RESULTS_WARNING = (
    "Created results stay pending until a person accepts them in the entry's results table "
    "in Benchling (the plug icon in the table toolbar)"
)


def pending_results_next_step(
    plan: ChangePlan, table_ids: set[str] | None = None
) -> str | None:
    """Tell the user how to accept pending results, or None when the plan created none."""
    if plan.family.value != "results" or plan.after.get("state") != "pending_integration":
        return None
    tables = (plan.before or {}).get("tables")
    if isinstance(tables, list):
        names = [
            str((item.get("table") or {}).get("name") or item.get("table_id"))
            for item in tables
            if isinstance(item, dict)
            and (table_ids is None or item.get("table_id") in table_ids)
        ]
    else:
        table = (plan.before or {}).get("table") or {}
        table_id = plan.after.get("table_id")
        names = (
            [str(table.get("name") or table_id)]
            if table_ids is None or table_id in table_ids
            else []
        )
    if not names:
        return None
    name = ", ".join(names)
    noun = "tables" if len(names) > 1 else "table"
    return (
        f'Results are pending. Open entry {plan.entry_id}, go to the result {noun} "{name}", '
        "and click the plug (integrate) icon in each table's toolbar to accept the pending results. "
        "Until you do, they do not appear in the table and cannot be updated with this "
        "toolkit, because an update needs a result that is on the entry. Refresh the page "
        "if the entry is already open. To check, re-read with `results list --entry` or "
        "`results rows`."
    )


class ResultsPlanning(ResultUpdatePlanning):
    """Planning of new result records and updates to existing ones."""

    async def _plan_results(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        if request_is_result_update(request):
            if "table_id" in request:
                raise ValidationError("A result update does not use table_id or assay_results")
            if any(key in request for key in ("assay_results", "batches")):
                raise ValidationError("A result update does not use table or upload fields")
            return await self._plan_result_updates(entry_id, request)
        if "batches" in request:
            if "table_id" in request or "assay_results" in request:
                raise ValidationError("Pass batches or table_id and assay_results, not both")
            batches = request["batches"]
        else:
            batches = [
                {"table_id": request.get("table_id"), "assay_results": request.get("assay_results")}
            ]
        if not isinstance(batches, list) or not batches:
            raise ValidationError("Result plan requires non-empty batches")
        planned_batches: list[dict[str, Any]] = []
        seen_tables: set[str] = set()
        plan_row_offset = 0
        for batch in batches:
            if not isinstance(batch, dict):
                raise ValidationError("Each result batch requires table_id and assay_results")
            if set(batch) != {"table_id", "assay_results"}:
                raise ValidationError("Each result batch only accepts table_id and assay_results")
            table_id = batch.get("table_id")
            records = batch.get("assay_results")
            if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
                raise ValidationError("Each result batch requires an explicit V2 result table ID")
            if table_id in seen_tables:
                raise ValidationError("A result plan cannot include the same table more than once")
            seen_tables.add(table_id)
            if not isinstance(records, list) or not records:
                raise ValidationError("Each result batch requires non-empty assay_results")
            if len(records) > 500:
                raise ValidationError(
                    "Benchling accepts at most 500 results per notebook table upload"
                )
            table = await self.service.get_result_table(entry_id, table_id)
            schema_id = table.get("schema_id")
            if not isinstance(schema_id, str):
                raise UnsupportedError(
                    "The selected result table does not expose a result schema",
                    details={"table_id": table_id},
                )
            schema = await self.service.get_schema(schema_id)
            definitions = schema.get("field_definitions") or []
            planned, client_record_ids, projects = await self._prepare_result_records(
                records, table_id, schema_id, definitions
            )
            planned_batches.append(
                {
                    "table_id": table_id,
                    "schema_id": schema_id,
                    "table": table,
                    "records": planned,
                    "client_record_ids": client_record_ids,
                    "projects": projects,
                    "row_count": len(planned),
                    "plan_row_offset": plan_row_offset,
                    "schema_fields": self._field_definition_fingerprints(definitions),
                }
            )
            plan_row_offset += len(planned)
        all_records = [record for batch in planned_batches for record in batch["records"]]
        all_projects = {
            project_id: project
            for batch in planned_batches
            for project_id, project in batch["projects"].items()
        }
        before = {
            "tables": [
                {
                    "table_id": batch["table_id"],
                    "schema_id": batch["schema_id"],
                    "row_count": batch["row_count"],
                    "table": batch["table"],
                }
                for batch in planned_batches
            ],
            "records": [],
        }
        after = {
            "batches": planned_batches,
            "projects": all_projects,
            "state": "pending_integration",
        }
        preconditions: dict[str, Any] = {
            "entry_id": entry_id,
            "batches": [
                {
                    "table_id": batch["table_id"],
                    "schema_id": batch["schema_id"],
                    "schema_fields": batch["schema_fields"],
                }
                for batch in planned_batches
            ],
        }
        if len(planned_batches) == 1:
            only = planned_batches[0]
            before["table"] = only["table"]
            after.update(
                {
                    "table_id": only["table_id"],
                    "schema_id": only["schema_id"],
                    "records": only["records"],
                    "client_record_ids": only["client_record_ids"],
                }
            )
            preconditions.update(
                {
                    "table_id": only["table_id"],
                    "result_schema_id": only["schema_id"],
                    "result_schema_fields": only["schema_fields"],
                }
            )
        warnings = self._result_plan_warnings(all_records)
        return (
            before,
            after,
            preconditions,
            [
                "verified entry-to-table and table-to-schema relationships",
                "local correlation UUIDs and persisted task IDs for reconciliation",
                "no automatic write retry",
            ],
            warnings,
        )

    async def _prepare_result_records(
        self, records: list[Any], table_id: str, schema_id: str, definitions: list[Any]
    ) -> tuple[list[Any], list[str], dict[str, dict[str, Any]]]:
        """Validate and normalize records; returns them with correlation IDs and referenced projects."""
        planned = deepcopy(records)
        client_record_ids: list[str] = []
        projects: dict[str, dict[str, Any]] = {}
        for record in planned:
            if (
                not isinstance(record, dict)
                or not record.get("schemaId")
                or not isinstance(record.get("fields"), dict)
            ):
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
        return planned, client_record_ids, projects

    def _result_plan_warnings(self, planned: list[Any]) -> list[str]:
        """Warnings shown in the preview for submitted results."""
        warnings = [
            PENDING_RESULTS_WARNING,
            "Result IDs are assigned by Benchling; local correlation UUIDs are not idempotency keys",
        ]
        if any(record.get("projectId") is None for record in planned):
            warnings.append(
                "Results without projectId are readable only by their creator until access is changed"
            )
        return warnings

    async def _apply_results(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        if plan.preconditions.get("operation") == "update":
            return await self._apply_result_updates(plan, journal)
        batches = plan.after.get("batches")
        if not isinstance(batches, list):
            batches = [
                {
                    "table_id": plan.after["table_id"],
                    "schema_id": plan.preconditions["result_schema_id"],
                    "records": plan.after["records"],
                    "client_record_ids": plan.after.get("client_record_ids", []),
                    "schema_fields": plan.preconditions.get("result_schema_fields"),
                }
            ]
        expected = {
            batch["table_id"]: batch
            for batch in plan.preconditions.get("batches", [])
            if isinstance(batch, dict)
        }
        if not expected:
            expected = {
                plan.preconditions["table_id"]: {
                    "schema_id": plan.preconditions["result_schema_id"],
                    "schema_fields": plan.preconditions.get("result_schema_fields"),
                }
            }
        submitted: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        uncertain = False
        first_error: BenchlingAgentError | None = None
        for batch_index, batch in enumerate(batches):
            table_id = batch["table_id"]
            try:
                if any("id" in record for record in batch["records"]):
                    raise ConflictError(
                        "This result plan uses an unsupported bulk-table contract; "
                        "create a new preview"
                    )
                table = await self.service.get_result_table(plan.entry_id, table_id)
                self._expect(
                    table.get("schema_id"),
                    expected[table_id]["schema_id"],
                    "result table schema",
                )
                schema = await self.service.get_schema(expected[table_id]["schema_id"])
                self._expect(
                    self._field_definition_fingerprints(schema.get("field_definitions") or []),
                    expected[table_id].get("schema_fields"),
                    "result schema fields",
                )
                response = await self.api.request(
                    "POST",
                    "/assay-results:bulk-create",
                    version=2,
                    json={"tableId": table_id, "assayResults": batch["records"]},
                )
            except PartialCompletionError as exc:
                uncertain = True
                first_error = first_error or exc
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "unknown",
                    "error": exc.message,
                    "details": exc.details,
                }
                failures.append(failure)
                self._event(journal, "submit_results", "unknown", failure)
                continue
            except BenchlingAgentError as exc:
                first_error = first_error or exc
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "failed",
                    "error": exc.message,
                    "details": exc.details,
                }
                failures.append(failure)
                self._event(journal, "submit_results", "failed", failure)
                continue
            task_id = response.get("taskId") or response.get("id")
            if not task_id:
                uncertain = True
                first_error = first_error or PartialCompletionError(
                    "Benchling accepted the result upload but did not return a task ID"
                )
                failure = {
                    "batch_index": batch_index,
                    "table_id": table_id,
                    "state": "unknown",
                    "response": dict(response),
                    "client_record_ids": batch.get("client_record_ids", []),
                }
                failures.append(failure)
                self._event(
                    journal,
                    "submit_results",
                    "unknown",
                    {**write_event_details(response), **failure},
                )
                continue
            result = {
                "batch_index": batch_index,
                "table_id": table_id,
                "task_id": task_id,
                "response": dict(response),
                "record_ids": [],
                "client_record_ids": batch.get("client_record_ids", []),
                "row_count": len(batch["records"]),
                "plan_row_offset": batch.get("plan_row_offset", 0),
                "state": "submission_pending",
            }
            recorded = write_event_details(
                response, **{key: value for key, value in result.items() if key != "response"}
            )
            if "http_status" in recorded:
                result["http_status"] = recorded["http_status"]
            self._event(journal, "submit_results", "succeeded", recorded)
            submitted.append(result)
        if failures:
            details = {
                "submitted": submitted,
                "failures": failures,
                "client_record_ids": [
                    record_id
                    for batch in batches
                    for record_id in batch.get("client_record_ids", [])
                ],
            }
            if uncertain:
                raise PartialCompletionError(
                    "One or more result table uploads have an unconfirmed outcome",
                    details=details,
                ) from first_error
            assert first_error is not None
            first_error.details = {**first_error.details, **details}
            raise first_error
        result = {
            "batches": submitted,
            "state": "submission_pending",
        }
        if len(submitted) == 1:
            result.update(
                {
                    "task_id": submitted[0]["task_id"],
                    "response": submitted[0]["response"],
                    "record_ids": [],
                    "client_record_ids": submitted[0]["client_record_ids"],
                    "http_status": submitted[0]["http_status"],
                }
            )
        return result
