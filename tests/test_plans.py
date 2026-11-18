from __future__ import annotations

from pathlib import Path

import pytest

from benchling_agent.config import ConfigStore
from benchling_agent.errors import ConflictError, PartialCompletionError, ValidationError
from benchling_agent.models import AuthMethod, Environment, PlanFamily
from benchling_agent.plans import ChangePlanner, PlanRepository


class FakeAPI:
    def __init__(self) -> None:
        self.environment = Environment(
            name="sandbox",
            tenant_url="https://tenant.benchling.com",
            auth_method=AuthMethod.CLIENT_CREDENTIALS,
        )
        self.requests: list[tuple[str, str, int, dict | None]] = []
        self.entry_version = "v1"
        self.patch_count = 0
        self.fail_second_patch = False

    async def request(self, method, path, *, version=3, params=None, json=None, early_access=None):
        self.requests.append((method, path, version, json))
        if method == "GET" and path.startswith("/entry/"):
            return {"id": "etr_one", "versionMetadata": {"versionId": self.entry_version}}
        if method == "POST" and path == "/assay-results:bulk-create":
            return {"taskId": "task_1"}
        if method == "PATCH":
            self.patch_count += 1
            self.entry_version = f"v{self.patch_count + 1}"
            if self.fail_second_patch and self.patch_count == 2:
                raise ConflictError("simulated conflict")
            return {"id": "updated"}
        raise AssertionError((method, path, version))


class FakeService:
    def __init__(self) -> None:
        self.table_version = "v1"
        self.field_value = 20
        self.table = {
            "id": "docpart_table",
            "versionId": "v1",
            "content": {
                "__typename": "UnstructuredTableDocumentContent",
                "name": "Measurements",
                "columnDefinitions": [{"name": "Sample"}, {"name": "Value"}],
                "rows": [
                    {
                        "cells": [
                            {"inputValue": {"type": "text_value", "value": "A"}},
                            {
                                "inputValue": {"type": "text_value", "value": "1"},
                                "evaluatedValue": {"type": "text_value", "value": "1"},
                                "format": {"backgroundColor": "#fff000"},
                                "selectorId": "selector_1",
                            },
                        ]
                    },
                    {
                        "cells": [
                            {
                                "inputValue": {"type": "text_value", "value": "B"},
                                "mergeRange": {"mergeRowSpan": 1, "mergeColSpan": 2},
                            },
                            {
                                "inputValue": {"type": "text_value", "value": ""},
                                "mergeRangeParent": {"row": 1, "col": 0},
                            },
                        ]
                    },
                    {
                        "cells": [
                            {"inputValue": {"type": "text_value", "value": "C"}},
                            {
                                "inputValue": {
                                    "type": "text_value",
                                    "value": "=SUM(A1:A2)",
                                }
                            },
                        ]
                    },
                ],
            },
        }

    async def resolve_entry(self, reference):
        return {"entry_id": "etr_one"}

    async def get_table(self, part_id):
        copy = __import__("copy").deepcopy(self.table)
        copy["versionId"] = self.table_version
        return copy

    async def get_table_for_entry(self, reference, part_id):
        return {
            "resolved": {"entry_id": "etr_one"},
            "part": {
                "id": part_id,
                "content": {"__typename": "UnstructuredTableDocumentContentSummary"},
            },
            "table": await self.get_table(part_id),
        }

    async def get_entry(self, reference, *, full=False):
        return {
            "version_id": "v1",
            "parts": [
                {
                    "id": "docpart_text",
                    "content": {
                        "__typename": "TextDocumentContent",
                        "text": "before",
                        "formats": [],
                        "links": [],
                        "mentions": [],
                        "indentation": 0,
                    },
                }
            ],
            "entry": {"id": "etr_one"},
            "resolved": {"entry_id": "etr_one"},
            "sections": [],
            "tables": {},
            "results": [],
            "issues": [],
            "completeness": "complete",
        }

    async def get_entry_fields(self, reference):
        return {
            "entry_id": "etr_one",
            "schema": {"id": "entsch_one"},
            "fields": {"Temperature": {"value": self.field_value}},
            "fields_by_system_name": {
                "temperature": {"value": self.field_value}
            },
            "field_definitions": [
                {
                    "id": "tsf_temperature",
                    "__typename": "IntegerFieldDefinition",
                    "name": "Temperature",
                    "systemName": "temperature",
                    "isRequired": True,
                    "derivationType": "DIRECT",
                }
            ],
            "modified_at": "2026-01-01T00:00:00Z",
        }

    async def get_result_table(self, entry_id, table_id):
        return {
            "entry_id": entry_id,
            "table_id": table_id,
            "name": "Results",
            "schema_id": "assaysch_one",
            "columns": [],
        }

    async def get_schema(self, schema_id):
        return {
            "schema": {"id": schema_id},
            "field_definitions": [
                {
                    "id": "assayfield_ct",
                    "__typename": "IntegerFieldDefinition",
                    "name": "Ct",
                    "systemName": "ct",
                    "isRequired": True,
                    "derivationType": "DIRECT",
                }
            ],
        }

    async def get_project(self, project_id):
        return {"id": project_id, "name": "Sandbox"}

    async def get_task(self, task_id):
        return {"status": "SUCCEEDED", "response": {}}


@pytest.fixture
def planner(tmp_path: Path):
    api = FakeAPI()
    service = FakeService()
    repository = PlanRepository(ConfigStore(tmp_path / "app"))
    return ChangePlanner(service, api, repository), service, api


@pytest.mark.asyncio
async def test_cell_preview_preserves_unrelated_data_and_writes_nothing(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        PlanFamily.TABLE,
        {
            "operation": "set_cells",
            "table_part_id": "docpart_table",
            "updates": [{"row": 0, "column": 1, "value": "3"}],
        },
    )
    updated = plan.after["content"]["rows"][0]["cells"][1]
    assert updated["inputValue"]["value"] == "3"
    assert updated["format"] == {"backgroundColor": "#fff000"}
    assert updated["selectorId"] == "selector_1"
    assert "evaluatedValue" not in updated
    assert plan.after["content"]["rows"][1] == service.table["content"]["rows"][1]
    assert plan.after["content"]["rows"][2] == service.table["content"]["rows"][2]
    assert api.requests == []


@pytest.mark.asyncio
async def test_stale_table_plan_fails_without_patch(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        "table",
        {
            "operation": "set_cells",
            "table_part_id": "docpart_table",
            "updates": [{"row": 0, "column": 1, "value": "3"}],
        },
    )
    service.table_version = "v2"
    with pytest.raises(ConflictError):
        await engine.apply(plan.id)
    assert not [request for request in api.requests if request[0] == "PATCH"]
    assert (await engine.status(plan.id))["journal"]["status"] == "failed"


@pytest.mark.asyncio
async def test_result_plan_correlates_records_locally_and_submits_once(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        "results",
        {
            "table_id": "strtbl_one",
            "assay_results": [
                {"schemaId": "assaysch_one", "projectId": "src_one", "fields": {"ct": {"value": 12}}}
            ],
        },
    )
    record_id = plan.after["client_record_ids"][0]
    result = await engine.apply(plan.id)
    assert result["journal"]["result"]["client_record_ids"] == [record_id]
    assert result["journal"]["result"]["record_ids"] == []
    posts = [request for request in api.requests if request[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][3]["tableId"] == "strtbl_one"
    assert "id" not in posts[0][3]["assayResults"][0]
    assert result["journal"]["result"]["state"] == "submission_pending"
    status = await engine.status(plan.id)
    assert status["remote_task"]["status"] == "SUCCEEDED"
    assert status["result_state"] == "pending_integration"


@pytest.mark.asyncio
async def test_same_plan_cannot_be_applied_twice(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        "results",
        {
            "table_id": "strtbl_one",
            "assay_results": [{"schemaId": "assaysch_one", "fields": {"ct": {"value": 12}}}],
        },
    )
    await engine.apply(plan.id)
    with pytest.raises(ConflictError):
        await engine.apply(plan.id)
    assert len([request for request in api.requests if request[0] == "POST"]) == 1


@pytest.mark.asyncio
async def test_status_persists_server_assigned_ids_not_local_correlations(planner):
    engine, service, api = planner
    plan = await engine.plan("etr_one", "results", {
        "table_id": "strtbl_one",
        "assay_results": [{"schemaId": "assaysch_one", "fields": {"ct": {"value": 12}}}],
    })
    await engine.apply(plan.id)
    async def task(task_id):
        return {"status": "SUCCEEDED", "response": {"assayResults": [{"id": "server-uuid", "v3Id": "result_server"}]}}
    service.get_task = task
    status = await engine.status(plan.id)
    assert status["record_ids"] == ["server-uuid"]
    stored = engine.repository.load_journal(plan.id).result
    assert stored["record_v3_ids"] == ["result_server"]
    assert stored["client_record_ids"] == plan.after["client_record_ids"]
    assert stored["state"] == "pending_integration"
    assert len([request for request in api.requests if request[0] == "POST"]) == 1


@pytest.mark.asyncio
async def test_result_schema_must_match_selected_table(planner) -> None:
    engine, service, api = planner
    with pytest.raises(ValidationError):
        await engine.plan(
            "etr_one",
            "results",
            {
                "table_id": "strtbl_one",
                "assay_results": [
                    {
                        "schemaId": "assaysch_other",
                        "fields": {"ct": {"value": 12}},
                    }
                ],
            },
        )
    assert not [request for request in api.requests if request[0] == "POST"]


@pytest.mark.asyncio
async def test_field_plan_normalizes_definition_id_and_validates_type(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        "fields",
        {"schema_fields": {"tsf_temperature": {"value": 21}}},
    )
    assert plan.request["schema_fields"] == {"temperature": {"value": 21}}
    assert plan.after["targets"][0]["field_definition_id"] == "tsf_temperature"

    result = await engine.apply(plan.id)
    assert result["journal"]["status"] == "succeeded"
    patches = [request for request in api.requests if request[0] == "PATCH"]
    assert patches[0][3] == {"schemaFields": {"temperature": {"value": 21}}}

    with pytest.raises(ValidationError):
        await engine.plan(
            "etr_one",
            "fields",
            {"schema_fields": {"temperature": {"value": "twenty one"}}},
        )


@pytest.mark.asyncio
async def test_negative_cell_coordinates_are_rejected(planner) -> None:
    engine, service, api = planner
    with pytest.raises(ValidationError):
        await engine.plan(
            "etr_one",
            "table",
            {
                "operation": "set_cells",
                "table_part_id": "docpart_table",
                "updates": [{"row": -1, "column": 0, "value": "bad"}],
            },
        )


@pytest.mark.asyncio
async def test_field_plan_rejects_observed_value_change(planner) -> None:
    engine, service, api = planner
    plan = await engine.plan(
        "etr_one",
        "fields",
        {"schema_fields": {"temperature": {"value": 21}}},
    )
    service.field_value = 19
    with pytest.raises(ConflictError):
        await engine.apply(plan.id)
    assert not [request for request in api.requests if request[0] == "PATCH"]


@pytest.mark.asyncio
async def test_text_replacement_reports_partial_completion(planner) -> None:
    engine, service, api = planner
    api.fail_second_patch = True
    plan = await engine.plan(
        "etr_one",
        "document",
        {"operation": "replace_text", "part_id": "docpart_text", "text": "after"},
    )
    with pytest.raises(PartialCompletionError) as caught:
        await engine.apply(plan.id)
    assert caught.value.details["journal"]["status"] == "partial"
    status = await engine.status(plan.id)
    assert status["journal"]["status"] == "partial"
    assert [event["stage"] for event in status["journal"]["events"]] == [
        "apply",
        "insert_replacement",
        "apply",
    ]
