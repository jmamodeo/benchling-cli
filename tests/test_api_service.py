from __future__ import annotations

from collections import Counter

import httpx
import pytest

from benchling_agent.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    ValidationError,
)
from benchling_agent.services import (
    BenchlingService,
    build_sections,
    is_structured_table,
    part_type,
)


def test_current_flat_parts_are_preserved_and_sections_resolve():
    parts = [
        {"id": "docpart_s1", "__typename": "DocumentPartSection", "title": "Results"},
        {"id": "docpart_t1", "__typename": "DocumentPartText", "text": "Hello"},
        {"id": "docpart_s2", "__typename": "DocumentPartSection", "title": "Conclusion"},
    ]
    assert part_type(parts[1]) == "DocumentPartText"
    assert build_sections(parts)[0]["child_part_ids"] == ["docpart_t1"]
    assert "content" not in parts[1]
    assert is_structured_table({"__typename": "DocumentPartResultsStructuredTable"})


@pytest.mark.asyncio
async def test_v2_entry_envelope_and_folder_project_relationship(config, credentials):
    def handler(request):
        path = request.url.path
        if path == "/api/v3/entry/etr_one":
            return response_for(request, {"id": "etr_one"})
        if path == "/api/v2/entries/etr_one":
            return response_for(request, {"entry": {"id": "etr_one", "folderId": "lib_one", "days": [{"notes": [
                {"type": "results_table", "apiId": "strtbl_one", "name": "API table", "assayResultSchemaId": "assaysch_one"},
                {"type": "lookup_table", "apiId": "strtbl_other"},
            ]}]}})
        if path == "/api/v2/folders/lib_one":
            return response_for(request, {"folder": {"id": "lib_one", "projectId": "src_one"}})
        if path == "/api/v2/projects/src_one":
            return response_for(request, {"project": {"id": "src_one", "name": "Sandbox"}})
        if path == "/api/v2/tasks/task_one":
            return response_for(request, {"status": "SUCCEEDED", "response": {"assayResults": ["result-uuid"]}})
        raise AssertionError(path)
    api = make_api(config, credentials, handler)
    service = BenchlingService(api)
    tables = await service.list_result_tables("etr_one")
    assert tables["project_id"] == "src_one"
    assert len(tables["items"]) == 1
    assert (await service.get_result_table("etr_one", "strtbl_one"))["schema_id"] == "assaysch_one"
    assert (await service.get_project("src_one"))["name"] == "Sandbox"
    assert (await service.get_task("task_one"))["status"] == "SUCCEEDED"
    await api.close()

from conftest import make_api


def response_for(request: httpx.Request, payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=request)


@pytest.mark.asyncio
async def test_full_entry_paginates_parts_and_preserves_unknown_types(
    config, credentials
) -> None:
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] += 1
        if path == "/api/v3/entry/etr_one":
            return response_for(
                request,
                {
                    "id": "etr_one",
                    "displayId": "EXP-1",
                    "name": "Entry",
                    "versionMetadata": {"versionId": "etrver_1"},
                },
            )
        if path == "/api/v3/entry/etr_one/parts/items":
            if request.url.params.get("nextToken") == "page2":
                return response_for(
                    request,
                    {
                        "items": [
                            {
                                "id": "docpart_unknown",
                                "content": {"__typename": "FutureDocumentContent", "value": 7},
                            }
                        ],
                        "nextToken": "",
                    },
                )
            return response_for(
                request,
                {
                    "items": [
                        {
                            "id": "docpart_section",
                            "content": {"__typename": "SectionDocumentContent", "title": "Day 1"},
                        },
                        {
                            "id": "docpart_text",
                            "content": {"__typename": "TextDocumentContent", "text": "hello"},
                        },
                    ],
                    "nextToken": "page2",
                },
            )
        if path == "/api/v2/entries/etr_one":
            return response_for(request, {"id": "etr_one", "fields": {}, "schema": None})
        if path == "/api/v3/result/items":
            return response_for(request, {"items": [], "nextToken": ""})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    api = make_api(config, credentials, handler)
    service = BenchlingService(api)
    result = await service.get_entry("etr_one", full=True)
    assert [part["id"] for part in result["parts"]] == [
        "docpart_section",
        "docpart_text",
        "docpart_unknown",
    ]
    assert result["sections"][0]["child_part_ids"] == ["docpart_text", "docpart_unknown"]
    assert result["completeness"] == "partial"
    assert result["issues"][0]["reason"] == "unknown_part_type_preserved_as_opaque_data"
    assert result["result_retrieval"]["consistency"] == "independent_non_atomic"
    assert result["result_retrieval"]["complete"] is True
    assert calls["/api/v3/entry/etr_one/parts/items"] == 2
    await api.close()


@pytest.mark.asyncio
async def test_full_entry_rejects_two_concurrent_changes(config, credentials) -> None:
    entry_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal entry_reads
        path = request.url.path
        if path == "/api/v3/entry/etr_one":
            entry_reads += 1
            return response_for(
                request,
                {"id": "etr_one", "versionMetadata": {"versionId": f"v{entry_reads}"}},
            )
        if path == "/api/v3/entry/etr_one/parts/items":
            return response_for(request, {"items": [], "nextToken": ""})
        if path == "/api/v2/entries/etr_one":
            return response_for(request, {"id": "etr_one", "fields": {}, "schema": None})
        if path == "/api/v3/result/items":
            return response_for(request, {"items": [], "nextToken": ""})
        raise AssertionError(path)

    api = make_api(config, credentials, handler)
    service = BenchlingService(api)
    with pytest.raises(ConflictError):
        await service.get_entry("etr_one", full=True)
    await api.close()


@pytest.mark.asyncio
async def test_duplicate_section_title_requires_part_id(config, credentials) -> None:
    api = make_api(config, credentials, lambda request: response_for(request, {}))
    service = BenchlingService(api)

    async def snapshot(reference: str, *, full: bool = False):
        return {
            "resolved": {"entry_id": "etr_one"},
            "version_id": "v1",
            "sections": [
                {"part_id": "s1", "title": "Results", "child_part_ids": []},
                {"part_id": "s2", "title": "Results", "child_part_ids": []},
            ],
            "parts": [],
            "tables": {},
        }

    service.get_entry = snapshot  # type: ignore[method-assign]
    with pytest.raises(ValidationError):
        await service.get_section("etr_one", "Results")
    await api.close()


def test_section_boundaries_follow_flat_part_order() -> None:
    parts = [
        {"id": "s1", "content": {"__typename": "SectionDocumentContent", "title": "A"}},
        {"id": "p1", "content": {"__typename": "HeaderDocumentContent"}},
        {"id": "s2", "content": {"__typename": "SectionDocumentContent", "title": "B"}},
        {"id": "p2", "content": {"__typename": "TextDocumentContent"}},
    ]
    sections = build_sections(parts)
    assert sections[0]["child_part_ids"] == ["p1"]
    assert sections[0]["end_index"] == 1
    assert sections[1]["child_part_ids"] == ["p2"]


@pytest.mark.asyncio
async def test_entry_fields_are_addressable_by_v3_system_name(
    config, credentials
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/entry/etr_one":
            return response_for(
                request,
                {"id": "etr_one", "versionMetadata": {"versionId": "v1"}},
            )
        if path == "/api/v2/entries/etr_one":
            return response_for(
                request,
                {
                    "id": "etr_one",
                    "schema": {"id": "entsch_one"},
                    "fields": {"Temperature": {"value": 21}},
                    "modifiedAt": "2026-09-24T12:00:00Z",
                },
            )
        if path == "/api/v3/entry-schema/entsch_one":
            return response_for(request, {"id": "entsch_one", "name": "Experiment"})
        if path == "/api/v3/entry-schema/entsch_one/field-definitions/items":
            return response_for(
                request,
                {
                    "items": [
                        {
                            "id": "tsf_temperature",
                            "__typename": "IntegerFieldDefinition",
                            "name": "Temperature",
                            "systemName": "temperature",
                        }
                    ],
                    "nextToken": "",
                },
            )
        raise AssertionError(path)

    api = make_api(config, credentials, handler)
    fields = await BenchlingService(api).get_entry_fields("etr_one")
    assert fields["fields_by_system_name"] == {"temperature": {"value": 21}}
    assert fields["field_definitions"][0]["id"] == "tsf_temperature"
    await api.close()


@pytest.mark.asyncio
async def test_table_target_must_belong_to_selected_entry(config, credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/entry/etr_one":
            return response_for(
                request,
                {"id": "etr_one", "versionMetadata": {"versionId": "v1"}},
            )
        if request.url.path == "/api/v3/entry/etr_one/parts/items":
            return response_for(request, {"items": [], "nextToken": ""})
        raise AssertionError(request.url.path)

    api = make_api(config, credentials, handler)
    with pytest.raises(NotFoundError):
        await BenchlingService(api).get_table_for_entry("etr_one", "docpart_other")
    await api.close()


@pytest.mark.asyncio
async def test_result_table_relationship_comes_from_v2_entry(config, credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/entries/etr_one"
        return response_for(
            request,
            {
                "id": "etr_one",
                "days": [
                    {
                        "notes": [
                            {
                                "type": "results_table",
                                "apiId": "strtbl_one",
                                "name": "Measurements",
                                "assayResultSchemaId": "assaysch_one",
                                "columns": [],
                            }
                        ]
                    }
                ],
            },
        )

    api = make_api(config, credentials, handler)
    table = await BenchlingService(api).get_result_table("etr_one", "strtbl_one")
    assert table["schema_id"] == "assaysch_one"
    await api.close()


@pytest.mark.asyncio
async def test_write_server_error_is_unknown_and_never_retried(config, credentials) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response_for(request, {"message": "failed"}, status=503)

    api = make_api(config, credentials, handler)
    with pytest.raises(PartialCompletionError):
        await api.request("PATCH", "/entry/etr_one", json={"name": "Changed"})
    assert calls == 1
    await api.close()


@pytest.mark.asyncio
async def test_inaccessible_table_is_reported_as_partial(config, credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/entry/etr_one":
            return response_for(
                request,
                {"id": "etr_one", "versionMetadata": {"versionId": "v1"}},
            )
        if path == "/api/v3/entry/etr_one/parts/items":
            return response_for(
                request,
                {
                    "items": [
                        {
                            "id": "docpart_table",
                            "content": {
                                "__typename": "UnstructuredTableDocumentContentSummary"
                            },
                        }
                    ],
                    "nextToken": "",
                },
            )
        if path == "/api/v3/document-part-unstructured-table/docpart_table":
            return response_for(request, {"message": "forbidden"}, status=403)
        if path == "/api/v2/entries/etr_one":
            return response_for(request, {"id": "etr_one", "fields": {}, "schema": None})
        if path == "/api/v3/result/items":
            return response_for(request, {"items": [], "nextToken": ""})
        raise AssertionError(path)

    api = make_api(config, credentials, handler)
    result = await BenchlingService(api).get_entry("etr_one", full=True)
    assert result["completeness"] == "partial"
    assert result["issues"][0]["scope"] == "table:docpart_table"
    assert result["issues"][0]["reason"] == "detail_unavailable"
    await api.close()


@pytest.mark.asyncio
async def test_ambiguous_display_id_is_rejected(config, credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/entries"
        return response_for(
            request,
            {
                "entries": [
                    {"id": "etr_one", "displayId": "EXP-1"},
                    {"id": "etr_two", "displayId": "EXP-1"},
                ],
                "nextToken": "",
            },
        )

    api = make_api(config, credentials, handler)
    with pytest.raises(ValidationError) as caught:
        await BenchlingService(api).resolve_entry("EXP-1")
    assert caught.value.details["entry_ids"] == ["etr_one", "etr_two"]
    await api.close()
