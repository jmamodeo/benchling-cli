from __future__ import annotations

import pytest

from benchling_toolkit.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)

REQUEST = {
    "entry_template_id": "etpl_abc123",
    "folder_id": "lib_abc123",
    "name": "Fresh experiment",
}


async def test_entry_plan_reads_and_previews_then_creates_entry_once(runtime, fake) -> None:
    planned = await runtime.planner.plan(
        None,
        "entry",
        {**REQUEST, "schema_fields": {"temperature": {"value": 21}}},
    )

    assert planned.entry_id is None
    assert planned.before["template"]["name"] == "Synthetic template"
    assert planned.before["folder"]["name"] == "Synthetic folder"
    assert planned.after["name"] == "Fresh experiment"
    assert planned.after["schema_fields"][0]["field_definition_id"] == "tsf_temp"
    assert planned.after["request"] == {
        "entryTemplateId": "etpl_abc123",
        "folderId": "lib_abc123",
        "name": "Fresh experiment",
        "schemaFields": {"tsf_temp": {"value": 21}},
    }
    assert fake.writes() == []
    assert {call.path for call in fake.calls if call.method == "GET"} >= {
        "/api/v3/entry-template/etpl_abc123",
        "/api/v3/folder/lib_abc123",
        "/api/v3/entry-schema/entsch_abc123/field-definitions/items",
    }

    result = await runtime.planner.apply(planned.id)

    assert result["journal"]["result"]["entry_id"] == "etr_created1"
    (write,) = fake.writes()
    assert (write.method, write.path, write.body) == (
        "POST",
        "/api/v3/entry",
        planned.after["request"],
    )
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)
    assert len(fake.writes()) == 1

    inspected = await runtime.service.inspect_entry("etr_created1")
    tables = await runtime.service.list_result_tables("etr_created1")
    assert inspected["resolved"]["entry_id"] == "etr_created1"
    assert tables["items"][0]["table_id"] == "strtbl_created1"


@pytest.mark.parametrize(
    ("resource", "resource_id"), [("template", "etpl_abc123"), ("folder", "lib_abc123")]
)
async def test_archived_template_or_folder_is_rejected(
    runtime, fake, resource, resource_id
) -> None:
    resources = fake.entry_templates if resource == "template" else fake.folders
    resources[resource_id]["archived"] = True

    with pytest.raises(UnsupportedError, match="archived"):
        await runtime.planner.plan(None, "entry", REQUEST)
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("specification", "resource"),
    [
        ({**REQUEST, "entry_template_id": "etpl_missing"}, "template"),
        ({**REQUEST, "folder_id": "lib_missing"}, "folder"),
    ],
)
async def test_missing_template_or_folder_is_rejected(
    runtime, fake, specification, resource
) -> None:
    with pytest.raises(NotFoundError):
        await runtime.planner.plan(None, "entry", specification)
    assert fake.writes() == []


async def test_entry_schema_fields_require_a_template_schema(runtime, fake) -> None:
    fake.entry_templates["etpl_abc123"].pop("schema")

    with pytest.raises(ValidationError, match="no schema"):
        await runtime.planner.plan(
            None,
            "entry",
            {**REQUEST, "schema_fields": {"temperature": {"value": 21}}},
        )
    assert fake.writes() == []


async def test_entry_plan_rejects_bad_schema_fields(runtime, fake) -> None:
    with pytest.raises(ValidationError, match="wrong type"):
        await runtime.planner.plan(
            None,
            "entry",
            {**REQUEST, "schema_fields": {"temperature": {"value": "warm"}}},
        )
    assert fake.writes() == []


async def test_entry_create_with_uncertain_response_cannot_be_retried(runtime, fake) -> None:
    planned = await runtime.planner.plan(None, "entry", REQUEST)
    fake.payload_overrides.append(("/api/v3/entry", 201, {"name": REQUEST["name"]}))

    with pytest.raises(PartialCompletionError):
        await runtime.planner.apply(planned.id)
    assert runtime.repository.load_journal(planned.id).status.value == "unknown"
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)
    assert len(fake.writes()) == 1
