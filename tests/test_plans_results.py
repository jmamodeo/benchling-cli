from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID

from benchling_toolkit.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)

TABLE = "strtbl_example"


def record(**overrides):
    base = {"schemaId": "assaysch_abc123", "fields": {"ct": {"value": 12}}}
    return {**base, **overrides}


async def plan(runtime, records, table_id=TABLE):
    return await runtime.planner.plan(
        ENTRY_ID, "results", {"table_id": table_id, "assay_results": records}
    )


@pytest.mark.parametrize(
    ("table_id", "records", "expected"),
    [
        ("tbl_wrong", [record()], "explicit V2 result table ID"),
        (None, [record()], "explicit V2 result table ID"),
        (TABLE, [], "non-empty assay_results"),
        (TABLE, "records", "non-empty assay_results"),
        (TABLE, [record()] * 501, "at most 500"),
        (TABLE, ["bad"], "schemaId and fields"),
        (TABLE, [{"schemaId": "assaysch_abc123"}], "schemaId and fields"),
        (TABLE, [{"fields": {"ct": {"value": 1}}}], "schemaId and fields"),
        (TABLE, [record(fields={})], "required schema field"),
        (TABLE, [record(fields={"note": {"value": "only"}})], "required schema field"),
        (TABLE, [record(projectId="")], "non-empty string"),
        (TABLE, [record(projectId=5)], "non-empty string"),
        (TABLE, [record(schemaId="assaysch_other")], "does not match the selected table"),
        (TABLE, [record(fields={"ct": {"value": "high"}})], "wrong type"),
    ],
)
async def test_invalid_result_plans_are_rejected(
    runtime, fake, table_id, records, expected
) -> None:
    with pytest.raises(ValidationError, match=expected):
        await runtime.planner.plan(
            ENTRY_ID, "results", {"table_id": table_id, "assay_results": records}
        )
    assert fake.writes() == []


async def test_client_supplied_result_ids_are_unsupported(runtime) -> None:
    with pytest.raises(UnsupportedError, match="server-assigned"):
        await plan(runtime, [record(id="result_mine")])


async def test_table_must_be_on_the_selected_entry(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(runtime, [record()], table_id="strtbl_elsewhere")


async def test_table_without_schema_is_unsupported(runtime, fake) -> None:
    fake.result_table_schema = None
    with pytest.raises(UnsupportedError, match="result schema"):
        await plan(runtime, [record()])


async def test_unknown_project_is_rejected(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(runtime, [record(projectId="src_missing")])


async def test_plan_resolves_project_and_warns_when_unscoped(runtime) -> None:
    scoped = await plan(runtime, [record(projectId="src_abc123")])
    assert scoped.after["projects"] == {
        "src_abc123": {"id": "src_abc123", "name": "Synthetic project"}
    }
    assert not any("creator" in warning for warning in scoped.warnings)
    unscoped = await plan(runtime, [record()])
    assert any("creator" in warning for warning in unscoped.warnings)
    assert len(unscoped.after["client_record_ids"]) == 1
    assert unscoped.after["state"] == "pending_integration"


async def test_apply_posts_normalized_records_once(runtime, fake) -> None:
    planned = await plan(runtime, [record(fields={"assayfield_ct": {"value": 7}}), record()])
    applied = await runtime.planner.apply(planned.id)
    (payload,) = fake.bulk_payloads
    assert payload["tableId"] == TABLE
    assert [r["fields"] for r in payload["assayResults"]] == [
        {"ct": {"value": 7}},
        {"ct": {"value": 12}},
    ]
    assert applied["journal"]["result"]["http_status"] == 202
    assert applied["journal"]["result"]["task_id"] == "task_abc123"
    assert applied["journal"]["result"]["state"] == "submission_pending"
    assert applied["journal"]["result"]["record_ids"] == []
    assert applied["journal"]["result"]["client_record_ids"] == planned.after["client_record_ids"]
    assert all("id" not in record for record in payload["assayResults"])


async def test_status_persists_server_assigned_ids_not_local_correlations(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.task = {
        "status": "SUCCEEDED",
        "response": {"assayResults": [{"id": "server-uuid", "v3Id": "result_server"}]},
    }
    status = await runtime.planner.status(planned.id)
    assert status["record_ids"] == ["server-uuid"]
    stored = runtime.repository.load_journal(planned.id).result
    assert stored["record_v3_ids"] == ["result_server"]
    assert stored["client_record_ids"] == planned.after["client_record_ids"]
    assert stored["state"] == "pending_integration"
    assert len(fake.bulk_payloads) == 1


async def test_apply_detects_result_schema_drift(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.field_defs["assaysch_abc123"][1]["isRequired"] = True
    with pytest.raises(ConflictError, match="Result schema fields"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_table_reassigned_to_another_schema(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.field_defs["assaysch_other"] = fake.field_defs["assaysch_abc123"]
    fake.result_table_schema = "assaysch_other"
    with pytest.raises(ConflictError, match="Result table schema"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_accepted_upload_without_task_id_reports_unknown_completion(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    fake.bulk_response = {}
    with pytest.raises(PartialCompletionError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["client_record_ids"] == planned.after["client_record_ids"]
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "unknown"


@pytest.mark.parametrize(
    ("remote", "state"),
    [
        ({"status": "RUNNING"}, "submission_running"),
        ({"status": "FAILED"}, "submission_failed"),
        ({"status": "SUCCEEDED", "response": {"assayResults": []}}, "pending_integration"),
        ({"status": "SOMETHING_NEW"}, "unknown"),
    ],
)
async def test_status_maps_remote_task_state(runtime, fake, remote, state) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.task = remote
    status = await runtime.planner.status(planned.id)
    assert status["result_state"] == state
    assert status["journal"]["result"]["state"] == state


async def test_status_surfaces_task_lookup_errors_without_losing_the_journal(runtime, fake) -> None:
    planned = await plan(runtime, [record()])
    await runtime.planner.apply(planned.id)
    fake.fail("GET", "/tasks/", 403)
    status = await runtime.planner.status(planned.id)
    assert status["remote_task_error"]["code"] == "access_denied"
    assert status["journal"]["status"] == "succeeded"
    assert status["journal"]["result"]["state"] == "submission_pending"
