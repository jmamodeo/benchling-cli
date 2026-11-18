from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART

from benchling_agent.errors import (
    ConflictError,
    NotFoundError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from benchling_agent.services import (
    entry_version,
    field_definition_type,
    is_section,
    is_unstructured_table,
    part_content,
)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"type": "integer"}, "INTEGER"),
        ({"__typename": "TextFieldDefinition"}, "TEXT"),
        ({"__typename": "LongTextFieldDefinition"}, "LONG_TEXT"),
        ({"__typename": "benchling.DateTimeFieldDefinition"}, "DATETIME"),
        ({"__typename": "DropdownLinkFieldDefinition"}, "DROPDOWN"),
        ({"__typename": "EntityLinkFieldDefinition"}, "LINK"),
        ({"__typename": "Surprise"}, "UNKNOWN"),
        ({}, "UNKNOWN"),
    ],
)
def test_field_definition_types_are_stable_across_response_variants(definition, expected) -> None:
    assert field_definition_type(definition) == expected


def test_part_helpers_handle_nested_and_flat_shapes() -> None:
    nested = {"content": {"__typename": "SectionDocumentContent", "title": "T"}}
    flat = {"__typename": "DocumentPartUnstructuredTable"}
    assert part_content(nested)["title"] == "T"
    assert part_content(flat) is flat
    assert is_section(nested) and not is_section(flat)
    assert is_unstructured_table(flat)
    assert not is_unstructured_table({"__typename": "DocumentPartStructuredTable"})


def test_entry_version_accepts_either_key_and_rejects_missing() -> None:
    assert entry_version({"versionMetadata": {"id": "v9"}}) == "v9"
    with pytest.raises(UpstreamError, match="version metadata"):
        entry_version({"id": ENTRY_ID})


@pytest.mark.parametrize("reference", ["https://your-tenant-dev.benchling.com/s/no-id-here"])
async def test_url_without_entry_id_is_rejected(runtime, reference) -> None:
    with pytest.raises(ValidationError, match="does not contain"):
        await runtime.service.resolve_entry(reference)


async def test_resolve_strips_whitespace_and_canonicalizes(runtime) -> None:
    resolved = await runtime.service.resolve_entry(f"  {ENTRY_ID}\n")
    assert resolved["display_id"] == "EXP-1"
    assert resolved["name"] == "Synthetic experiment"


async def test_canonical_id_that_does_not_exist_is_not_found(runtime) -> None:
    with pytest.raises(NotFoundError):
        await runtime.service.resolve_entry("etr_missing")


async def test_display_lookup_ignores_non_exact_matches(runtime, fake) -> None:
    fake.display_id = "EXP-10"
    with pytest.raises(NotFoundError):
        await runtime.service.resolve_entry("EXP-1")


async def test_table_pages_are_concatenated_and_marked_complete(runtime, fake) -> None:
    fake.table_page_size = 1
    table = await runtime.service.get_table(TABLE_PART)
    assert len(table["content"]["rows"]) == 3
    assert table["pagination"] == {"rows": 3, "complete": True}
    token_calls = [c.params.get("nextToken") for c in fake.calls if TABLE_PART in c.path]
    assert token_calls == [None, "1", "2"]


async def test_repeated_table_pagination_token_is_an_upstream_error(runtime, fake) -> None:
    original = fake._get_table

    def stuck(call, part_id):
        status, payload = original(call, part_id)
        payload["nextToken"] = "same"
        return status, payload

    fake._get_table = stuck
    with pytest.raises(UpstreamError, match="repeated a pagination token"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_with_non_list_rows_is_rejected(runtime, fake) -> None:
    fake.table_content["rows"] = "oops"
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_for_entry_rejects_non_table_and_duplicate_parts(runtime, fake) -> None:
    with pytest.raises(UnsupportedError, match="unstructured"):
        await runtime.service.get_table_for_entry(ENTRY_ID, TEXT_PART)
    fake.parts.append(dict(fake.parts[3]))
    with pytest.raises(UpstreamError, match="duplicate"):
        await runtime.service.get_table_for_entry(ENTRY_ID, TABLE_PART)


async def test_full_snapshot_reports_each_unavailable_dependency(runtime, fake) -> None:
    fake.fail("GET", "/entry-schema/", 403, times=20)
    fake.fail("GET", "/entries/", 403, times=20)
    fake.fail("GET", "/result/items", 403)
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    reasons = {issue["reason"] for issue in snapshot["issues"]}
    assert {
        "schema_unavailable",
        "v2_fields_unavailable",
        "linked_results_unavailable",
    } <= reasons
    assert snapshot["completeness"] == "partial"
    assert snapshot["result_retrieval"]["complete"] is False
    assert snapshot["result_retrieval"]["error"] == "Benchling denied access"
    assert snapshot["schema"] is None and snapshot["fields"] is None


async def test_full_snapshot_collects_every_result_page(runtime, fake) -> None:
    fake.results_page_size = 1
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert [r["id"] for r in snapshot["results"]] == ["result_1", "result_2", "result_3"]
    assert snapshot["result_retrieval"]["pages"] == 3
    assert snapshot["result_retrieval"]["records"] == 3


async def test_unknown_part_types_are_preserved_and_flagged(runtime, fake) -> None:
    fake.parts.append({"id": "docpart_new_kind", "__typename": "DocumentPartHologram", "x": 1})
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    flagged = [i for i in snapshot["issues"] if i["scope"] == "part:docpart_new_kind"]
    assert flagged[0]["reason"] == "unknown_part_type_preserved_as_opaque_data"
    assert snapshot["parts"][-1]["x"] == 1


async def test_entry_without_schema_has_no_field_definitions(runtime, fake) -> None:
    fake.entry_schema_id = None
    data = await runtime.service.get_entry_fields(ENTRY_ID)
    assert data["schema"] is None
    assert data["field_definitions"] == [] and data["fields_by_system_name"] == {}
    assert data["fields"] == {"Temperature": 20, "Notes": "n"}


async def test_fields_by_system_name_fall_back_to_system_name_keys(runtime, fake) -> None:
    fake.entry_fields = {"temperature": 7}
    data = await runtime.service.get_entry_fields(ENTRY_ID)
    assert data["fields_by_system_name"]["temperature"] == 7
    assert data["fields_by_system_name"]["notes"] is None


async def test_result_table_discovery_validates_identifiers(runtime, fake) -> None:
    original = fake._get_entry_v2

    def tweak(call, entry_id):
        status, payload = original(call, entry_id)
        notes = payload["entry"]["days"][0]["notes"]
        payload["entry"]["days"][0]["notes"] = variant(notes)
        return status, payload

    fake._get_entry_v2 = tweak
    duplicate = lambda notes: [notes[0], dict(notes[0])]  # noqa: E731
    invalid = lambda notes: [{**notes[0], "apiId": "bad_id"}]  # noqa: E731
    variant = duplicate
    with pytest.raises(UpstreamError, match="duplicate result-table"):
        await runtime.service.list_result_tables(ENTRY_ID)
    with pytest.raises(UpstreamError, match="Benchling returned the same"):
        await runtime.service.get_result_table(ENTRY_ID, "strtbl_example")
    variant = invalid
    with pytest.raises(UpstreamError, match="no valid API identifier"):
        await runtime.service.list_result_tables(ENTRY_ID)


async def test_missing_result_table_is_not_found(runtime) -> None:
    with pytest.raises(NotFoundError):
        await runtime.service.get_result_table(ENTRY_ID, "strtbl_unknown")


async def test_project_comes_from_entry_project_or_folder(runtime, fake) -> None:
    assert (await runtime.service.get_entry_fields(ENTRY_ID))["project_id"] == "src_abc123"
    original = fake._get_entry_v2

    def direct(call, entry_id):
        status, payload = original(call, entry_id)
        payload["entry"]["project"] = {"id": "src_direct"}
        return status, payload

    fake._get_entry_v2 = direct
    fake.calls.clear()
    assert (await runtime.service.get_entry_fields(ENTRY_ID))["project_id"] == "src_direct"
    assert not any("/folders/" in c.path for c in fake.calls)


async def test_v2_objects_must_carry_an_id(runtime, fake) -> None:
    fake._get_project = lambda call, pid: (200, {"project": {"name": "no id"}})
    with pytest.raises(UpstreamError, match="expected object"):
        await runtime.service.get_project("src_abc123")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"task": {"status": "SUCCEEDED"}}, "SUCCEEDED"), ({"status": "RUNNING"}, "RUNNING")],
)
async def test_task_status_accepts_wrapped_and_flat_payloads(
    runtime, fake, payload, expected
) -> None:
    fake._get_task = lambda call, task_id: (200, payload)
    assert (await runtime.service.get_task("task_abc123"))["status"] == expected


async def test_task_without_status_is_rejected(runtime, fake) -> None:
    fake._get_task = lambda call, task_id: (200, {"response": {}})
    with pytest.raises(UpstreamError, match="status"):
        await runtime.service.get_task("task_abc123")


@pytest.mark.parametrize("size", [0, 101])
async def test_list_results_validates_page_size_before_calling_the_api(runtime, fake, size) -> None:
    with pytest.raises(ValidationError):
        await runtime.service.list_results(page_size=size)
    assert fake.calls == []


async def test_get_schema_routes_result_schemas_separately(runtime, fake) -> None:
    await runtime.service.get_schema("assaysch_abc123")
    assert any(c.path.startswith("/api/v3/result-schema/") for c in fake.calls)
    fake.calls.clear()
    await runtime.service.get_schema("entsch_abc123")
    assert all("/entry-schema/" in c.path for c in fake.calls)


async def test_doctor_records_per_endpoint_failures_without_raising(runtime, fake) -> None:
    fake.fail("GET", "/api/v2/entries", 403)
    report = await runtime.service.doctor()
    assert report["ok"] is False
    assert report["capabilities"]["document_parts"] is True
    assert report["capabilities"]["entry_fields"] is False
    assert report["capabilities"]["result_upload_pending_integration"] is False
    assert report["capabilities"]["structured_table_edit"] is False
    assert report["checks"][1] == {
        "name": "v2_entries",
        "ok": False,
        "error": "Benchling denied access",
        "code": "access_denied",
    }


async def test_snapshot_conflict_when_entry_keeps_changing(runtime, fake) -> None:
    original = fake._get_entry

    def moving(call, entry_id):
        fake.entry_version += 1
        return original(call, entry_id)

    fake._get_entry = moving
    with pytest.raises(ConflictError, match="changed while"):
        await runtime.service.get_entry(ENTRY_ID, full=True)


async def test_snapshot_retries_once_when_entry_changes_during_assembly(runtime, fake) -> None:
    original = fake._get_entry
    reads = {"count": 0}

    def settles(call, entry_id):
        reads["count"] += 1
        if reads["count"] == 3:  # the first snapshot's closing read
            fake.entry_version += 1
        return original(call, entry_id)

    fake._get_entry = settles
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert snapshot["version_id"] == "ver_2"
