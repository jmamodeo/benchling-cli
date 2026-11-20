"""Result-record submission planning."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from ..errors import (
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


def pending_results_next_step(plan: ChangePlan) -> str | None:
    """Tell the user how to accept pending results, or None when the plan created none."""
    if plan.family.value != "results" or plan.after.get("state") != "pending_integration":
        return None
    table = (plan.before or {}).get("table") or {}
    name = table.get("name") or plan.after.get("table_id")
    return (
        f'Results are pending. Open entry {plan.entry_id}, go to the results table "{name}", '
        "and click the plug (integrate) icon in its toolbar to accept the pending results. "
        "Until you do, they do not appear in the table and cannot be updated with this "
        "toolkit, because an update needs a result that is on the entry. Refresh the page "
        "if the entry is already open. To check, re-read with `results list --entry` or "
        "`results rows`."
    )


class ResultsPlanning(ResultUpdatePlanning):
    """Planning of new result records and updates to existing ones."""

    async def _plan_results(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        if request_is_result_update(request):
            if "table_id" in request or "assay_results" in request:
                raise ValidationError("A result update does not use table_id or assay_results")
            return await self._plan_result_updates(entry_id, request)
        table_id = request.get("table_id")
        records = request.get("assay_results")
        if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
            raise ValidationError("Result plan requires an explicit V2 result table ID")
        if not isinstance(records, list) or not records:
            raise ValidationError("Result plan requires non-empty assay_results")
        if len(records) > 500:
            raise ValidationError(
                "Benchling accepts at most 500 results in a notebook table upload"
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
        before = {"table": table, "records": []}
        after = {
            "table_id": table_id,
            "schema_id": schema_id,
            "projects": projects,
            "records": planned,
            "client_record_ids": client_record_ids,
            "state": "pending_integration",
        }
        warnings = self._result_plan_warnings(planned)
        return (
            before,
            after,
            {
                "entry_id": entry_id,
                "table_id": table_id,
                "result_schema_id": schema_id,
                "result_schema_fields": self._field_definition_fingerprints(definitions),
            },
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
        entry_id = self._entry_id(plan)
        if plan.preconditions.get("operation") == "update":
            return await self._apply_result_updates(plan, journal)
        if any("id" in record for record in plan.after["records"]):
            raise ConflictError(
                "This result plan uses an unsupported bulk-table contract; create a new preview"
            )
        table = await self.service.get_result_table(entry_id, plan.preconditions["table_id"])
        self._expect(
            table.get("schema_id"),
            plan.preconditions["result_schema_id"],
            "result table schema",
        )
        schema = await self.service.get_schema(plan.preconditions["result_schema_id"])
        self._expect(
            self._field_definition_fingerprints(schema.get("field_definitions") or []),
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
        recorded = write_event_details(response)
        result: dict[str, Any] = {
            "response": recorded["response"],
            "task_id": task_id,
            "record_ids": [],
            "client_record_ids": plan.after.get("client_record_ids", []),
            "state": "submission_pending",
        }
        if "http_status" in recorded:
            result["http_status"] = recorded["http_status"]
        self._event(journal, "submit_results", "succeeded", result)
        return result
