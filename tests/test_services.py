from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART

from benchling_toolkit.errors import (
    AccessError,
    ConflictError,
    NotFoundError,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from benchling_toolkit.services import (
    build_sections,
    entry_version,
    field_definition_type,
    is_results_table,
    is_section,
    is_structured_table,
    is_unavailable_part,
    is_unstructured_table,
    part_content,
    part_type,
)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"type": "integer"}, "INTEGER"),
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
    assert part_type({"__typename": "DocumentPartText"}) == "DocumentPartText"
    assert is_structured_table({"__typename": "DocumentPartResultsStructuredTable"})
    assert is_results_table({"__typename": "DocumentPartResultsTable"})
    assert is_unavailable_part({"__typename": "DocumentPartUnavailable"})
    assert not is_results_table(flat)


def test_section_boundaries_follow_flat_part_order() -> None:
    parts = [
        {"id": "p0", "content": {"__typename": "TextDocumentContent"}},
        {"id": "s1", "content": {"__typename": "SectionDocumentContent", "title": "A"}},
        {"id": "p1", "content": {"__typename": "HeaderDocumentContent"}},
        {"id": "s2", "content": {"__typename": "SectionDocumentContent", "title": "B"}},
        {"id": "p2", "content": {"__typename": "TextDocumentContent"}},
    ]
    sections = build_sections(parts)
    assert sections[0]["child_part_ids"] == ["p1"]
    assert sections[0]["end_index"] == 2
    assert [s["part_id"] for s in sections] == ["s1", "s2"]
    assert sections[1]["child_part_ids"] == ["p2"]


def test_entry_version_accepts_either_key_and_rejects_missing() -> None:
    assert entry_version({"versionMetadata": {"id": "v9"}}) == "v9"
    with pytest.raises(UpstreamError, match="version metadata"):
        entry_version({"id": ENTRY_ID})


async def test_url_without_entry_id_is_rejected(runtime) -> None:
    with pytest.raises(ValidationError, match="does not contain"):
        await runtime.service.resolve_entry("https://your-tenant-dev.benchling.com/s/no-id-here")


async def test_ambiguous_display_id_is_rejected(runtime, fake) -> None:
    fake._list_entries_v2 = lambda call: (
        200,
        {
            "entries": [
                {"id": ENTRY_ID, "displayId": "EXP-1"},
                {"id": "etr_other", "displayId": "EXP-1"},
            ]
        },
    )
    with pytest.raises(ValidationError) as caught:
        await runtime.service.resolve_entry("EXP-1")
    assert caught.value.details["entry_ids"] == [ENTRY_ID, "etr_other"]


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
    assert table["versionMetadata"]["versionId"] == "ver_1"
    assert table["content"]["rows"][0]["cells"][0]["inputValue"] == {
        "type": "text_value",
        "value": "A",
    }
    row_calls = [c for c in fake.calls if c.path.endswith("/rows/items")]
    assert [c.params.get("nextToken") for c in row_calls] == [None, "1", "2"]
    assert all(c.params.get("pageSize") == "100" for c in row_calls)


async def test_repeated_table_pagination_token_is_an_upstream_error(runtime, fake) -> None:
    original = fake._table_rows

    def stuck(call, part_id):
        status, payload = original(call, part_id)
        payload["nextToken"] = "same"
        return status, payload

    fake._table_rows = stuck
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_with_non_list_rows_is_rejected(runtime, fake) -> None:
    fake.rows_field = ["not-a-url"]
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_rows_url_must_stay_on_the_tenant(runtime, fake) -> None:
    fake.rows_field = (
        "https://other.example/api/v3/document-part-unstructured-table/docpart_table1/rows/items"
    )
    with pytest.raises(UpstreamError, match="does not belong"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_row_count_must_match_the_pages(runtime, fake) -> None:
    fake.row_count_override = 9
    with pytest.raises(UpstreamError, match="row count"):
        await runtime.service.get_table(TABLE_PART)


async def test_table_without_entry_version_is_rejected(runtime, fake) -> None:
    fake.omit_table_version = True
    with pytest.raises(UpstreamError, match="version metadata"):
        await runtime.service.get_table(TABLE_PART)


async def test_unstructured_cell_without_text_is_rejected(runtime, fake) -> None:
    fake.corrupt_cells = True
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.get_table(TABLE_PART)


async def test_full_entry_and_part_reads_include_table_rows(runtime) -> None:
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    rows = snapshot["tables"][TABLE_PART]["content"]["rows"]
    assert [row["cells"][0]["inputValue"]["value"] for row in rows] == ["A", "B", "C"]
    part = await runtime.service.get_part(ENTRY_ID, TABLE_PART)
    assert part["detail"]["content"]["rows"][2]["cells"][1]["inputValue"]["value"] == "=SUM(A1:A2)"


async def test_results_table_rows_and_unavailable_parts_are_modeled(runtime, fake) -> None:
    fake.parts.append(
        {"id": "docpart_rt1", "__typename": "DocumentPartResultsTable", "name": "Assay"}
    )
    fake.results_parts["docpart_rt1"] = {
        "name": "Assay",
        "columnDefinitions": [
            {"__typename": "SchemaFieldColumnDefinition", "name": "Ct", "columnId": "col_ct"}
        ],
        "rows": [
            {
                "id": "strow_1",
                "cells": [{"inputValue": {"__typename": "IntegerValue", "value": 12}}],
                "submissionStatus": "UNSUBMITTED",
            }
        ],
        "resultSchema": {"id": "assaysch_abc123"},
    }
    fake.parts.append({"id": "docpart_unavail", "__typename": "DocumentPartUnavailable"})
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    table = snapshot["tables"]["docpart_rt1"]
    assert table["content"]["columnDefinitions"][0]["name"] == "Ct"
    assert table["content"]["rows"][0]["cells"][0]["inputValue"] == {
        "type": "integer_value",
        "value": 12,
    }
    reasons = {issue["reason"] for issue in snapshot["issues"]}
    assert "document_part_unavailable" in reasons
    assert "unknown_part_type_preserved_as_opaque_data" not in {
        issue["reason"] for issue in snapshot["issues"] if issue["scope"] == "part:docpart_unavail"
    }
    part = await runtime.service.get_part(ENTRY_ID, "docpart_rt1")
    assert part["detail"]["rowCount"] == 1


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


async def test_full_snapshot_collects_every_part_page(runtime, fake) -> None:
    fake.parts_page_size = 1
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert [part["id"] for part in snapshot["parts"]] == [part["id"] for part in fake.parts]
    parts_calls = [c for c in fake.calls if c.path.endswith("/parts/items")]
    assert len(parts_calls) == len(fake.parts)


async def test_inaccessible_table_fails_the_entry_read(runtime, fake) -> None:
    fake.fail("GET", "/document-part-unstructured-table/", 403)
    with pytest.raises(AccessError):
        await runtime.service.get_entry(ENTRY_ID, full=True)


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


async def test_result_table_relationship_comes_from_v2_entry(runtime, fake) -> None:
    listed = await runtime.service.list_result_tables(ENTRY_ID)
    assert listed["project_id"] == "src_abc123"
    assert [t["table_id"] for t in listed["items"]] == ["strtbl_example"]
    table = await runtime.service.get_result_table(ENTRY_ID, "strtbl_example")
    assert table["schema_id"] == fake.result_table_schema


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


async def test_list_results_filters_by_linked_entity_and_optional_field(runtime, fake) -> None:
    await runtime.service.list_results(entity_id="ent_abc123", entity_field_id="tsf_entity")
    (call,) = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert call.params == {
        "pageSize": "100",
        "schemaFieldLinkValue.linkedItemIds": "ent_abc123",
        "schemaFieldLinkValue.schemaFieldIds": "tsf_entity",
    }

    fake.calls.clear()
    await runtime.service.list_all_results(entity_id="ent_abc123", entity_field_id="tsf_entity")
    (call,) = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert call.params["schemaFieldLinkValue.linkedItemIds"] == "ent_abc123"
    assert call.params["schemaFieldLinkValue.schemaFieldIds"] == "tsf_entity"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entity_id": ""},
        {"entity_id": "ent_a,ent_b"},
        {"entity_field_id": "tsf_entity"},
    ],
)
async def test_list_results_rejects_bad_entity_filters_before_calling_api(
    runtime, fake, kwargs
) -> None:
    with pytest.raises(ValidationError):
        await runtime.service.list_results(**kwargs)
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
    assert report["capabilities"]["unstructured_table_cell_edit"] is True
    assert report["capabilities"]["unstructured_table_row_insert"] is True
    assert "unstructured_table_replace" not in report["capabilities"]
    assert report["checks"][1] == {
        "name": "v2_entries",
        "ok": False,
        "error": "Benchling denied access",
        "code": "access_denied",
        "hint": "Check that this Benchling app is allowed to read the tenant.",
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


async def test_entry_without_schema_or_folder_has_no_schema_snapshot_or_project(
    runtime, fake
) -> None:
    fake.entry_schema_id = None
    original = fake._get_entry_v2

    def unfiled(call, entry_id):
        status, payload = original(call, entry_id)
        del payload["entry"]["folderId"]
        return status, payload

    fake._get_entry_v2 = unfiled
    snapshot = await runtime.service.get_entry(ENTRY_ID, full=True)
    assert snapshot["schema"] is None
    assert not any(i["scope"] == "entry_schema" for i in snapshot["issues"])
    assert (await runtime.service.list_result_tables(ENTRY_ID))["project_id"] is None
