"""Validate the request bodies this client sends against the checked-in contracts.

The fixtures in ``contracts/`` are the subset of the reviewed Benchling OpenAPI
that the toolkit actually sends. The public multi-megabyte specifications are
not vendored; ``contracts/openapi-manifest.json`` records their checksums.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_benchling import ENTRY_ID

ROOT = Path(__file__).parents[1]
BODIES = json.loads((ROOT / "contracts" / "request-bodies.json").read_text(encoding="utf-8"))
LISTS = json.loads((ROOT / "contracts" / "list-parameters.json").read_text(encoding="utf-8"))


class ContractError(Exception):
    """A request body does not match the checked-in contract."""


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    if "$ref" not in schema:
        return schema
    name = schema["$ref"].removeprefix("#/")
    target = root[name]
    rest = {key: value for key, value in schema.items() if key != "$ref"}
    if not rest:
        return target
    return {"allOf": [target, rest]}


def _type_ok(instance: Any, expected: str | list[str]) -> bool:
    kinds = expected if isinstance(expected, list) else [expected]

    def one(kind: str) -> bool:
        if kind == "object":
            return isinstance(instance, dict)
        if kind == "array":
            return isinstance(instance, list)
        if kind == "string":
            return isinstance(instance, str)
        if kind == "boolean":
            return isinstance(instance, bool)
        if kind == "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        if kind == "number":
            return isinstance(instance, (int, float)) and not isinstance(instance, bool)
        if kind == "null":
            return instance is None
        raise AssertionError(kind)

    return any(one(kind) for kind in kinds)


def _check(instance: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    schema = _resolve(schema, root)
    if "allOf" in schema:
        for item in schema["allOf"]:
            _check(instance, item, root, path)
    if "anyOf" in schema:
        errors: list[str] = []
        for item in schema["anyOf"]:
            try:
                _check(instance, item, root, path)
                break
            except ContractError as exc:
                errors.append(str(exc))
        else:
            raise ContractError(f"{path} matched no alternative: {errors}")
    if "if" in schema:
        try:
            _check(instance, schema["if"], root, path)
        except ContractError:
            pass
        else:
            _check(instance, schema["then"], root, path)
    if "const" in schema and instance != schema["const"]:
        raise ContractError(f"{path} must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ContractError(f"{path} must be one of {schema['enum']!r}")
    if "type" in schema and not _type_ok(instance, schema["type"]):
        raise ContractError(f"{path} has the wrong type")
    object_keys = {"properties", "required", "additionalProperties", "minProperties"}
    if object_keys & schema.keys():
        if not isinstance(instance, dict):
            raise ContractError(f"{path} must be an object")
        for key in schema.get("required", []):
            if key not in instance:
                raise ContractError(f"{path}.{key} is required")
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            raise ContractError(f"{path} has too few properties")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in schema.get("properties", {}):
                _check(value, schema["properties"][key], root, f"{path}.{key}")
            elif additional is False:
                raise ContractError(f"{path}.{key} is not allowed")
            elif isinstance(additional, dict):
                _check(value, additional, root, f"{path}.{key}")
    if schema.get("type") == "array":
        if not isinstance(instance, list):
            raise ContractError(f"{path} must be an array")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ContractError(f"{path} has too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ContractError(f"{path} has too many items")
        if "items" in schema:
            for index, item in enumerate(instance):
                _check(item, schema["items"], root, f"{path}[{index}]")


def validate(instance: Any, name: str) -> None:
    """Raise ContractError when ``instance`` does not match a named contract."""
    _check(instance, BODIES[name], BODIES, "$")


def _schema_for(call) -> str:
    if call.path.endswith(":bulk-create"):
        return "assay_results_bulk_create"
    if "/document-part-unstructured-table/" in call.path:
        return "unstructured_table"
    if call.path.endswith(f"/entry/{ENTRY_ID}"):
        return "entry_update"
    raise AssertionError(call.path)


async def _apply(runtime, family: str, request: dict[str, Any]) -> None:
    planned = await runtime.planner.plan(ENTRY_ID, family, request)
    await runtime.planner.apply(planned.id)


async def test_every_write_matches_the_vendored_request_contract(runtime, fake) -> None:
    await _apply(
        runtime,
        "table",
        {
            "table_part_id": "docpart_table1",
            "operation": "set_cells",
            "updates": [{"row": 0, "column": 1, "value": "3"}],
        },
    )
    await _apply(
        runtime,
        "table",
        {
            "table_part_id": "docpart_table1",
            "operation": "replace",
            "table": {
                "caption": {"text": "cap", "formats": []},
                "columnDefinitions": [{"name": "Only", "width": 90, "isLocked": True}],
                "rows": [{"cells": [{"inputValue": {"type": "text_value", "value": "v"}}]}],
            },
        },
    )
    await _apply(
        runtime,
        "document",
        {"operation": "insert", "parts": [{"contentType": "text", "text": "tail"}]},
    )
    await _apply(runtime, "document", {"operation": "delete", "from_part_id": "docpart_text2"})
    await _apply(
        runtime,
        "document",
        {"operation": "move", "from_part_id": "docpart_text1", "position": "end_of_document"},
    )
    await _apply(
        runtime,
        "document",
        {"operation": "replace_text", "part_id": "docpart_text1", "text": "after"},
    )
    await _apply(runtime, "fields", {"schema_fields": {"temperature": {"value": 42}}})
    await _apply(
        runtime,
        "results",
        {
            "table_id": "strtbl_example",
            "assay_results": [
                {
                    "schemaId": "assaysch_abc123",
                    "fields": {"ct": {"value": 12}},
                    "projectId": "src_abc123",
                }
            ],
        },
    )
    writes = fake.writes()
    assert writes
    for call in writes:
        validate(call.body, _schema_for(call))
    kinds = {call.body.get("partsMutation", {}).get("operationType") for call in writes}
    assert {"insert", "delete", "move"} <= kinds
    assert any("schemaFields" in call.body for call in writes)
    assert any(call.path.endswith(":bulk-create") for call in writes)


def test_contract_rejects_fields_the_client_must_not_send() -> None:
    with pytest.raises(ContractError, match="id"):
        validate(
            {
                "tableId": "strtbl_example",
                "assayResults": [
                    {
                        "id": "result_local",
                        "schemaId": "assaysch_abc123",
                        "fields": {"ct": {"value": 1}},
                    }
                ],
            },
            "assay_results_bulk_create",
        )
    with pytest.raises(ContractError, match="evaluatedValue"):
        validate(
            {
                "versionId": "tver_1",
                "columnDefinitions": [],
                "rows": [
                    {
                        "cells": [
                            {
                                "inputValue": {"type": "text_value", "value": "a"},
                                "evaluatedValue": "a",
                            }
                        ]
                    }
                ],
            },
            "unstructured_table",
        )
    with pytest.raises(ContractError, match="displayValue"):
        validate(
            {"schemaFields": {"temperature": {"value": 1, "displayValue": "1"}}}, "entry_update"
        )


async def test_list_query_parameters_stay_inside_the_vendored_allow_list(runtime, fake) -> None:
    await runtime.service.list_entries(
        project="src_abc123",
        schema_id="entsch_abc123",
        name="Synthetic experiment",
        modified_after="2026-01-01",
        modified_before="2026-12-31",
    )
    page = await runtime.service.list_schemas(limit=2)
    await runtime.service.list_schemas(limit=2, cursor=page["next_token"])
    await runtime.service.list_entries(
        folder="lib_abc123",
        project="src_abc123",
        schema_id="entsch_abc123",
        name="Synthetic experiment",
        modified_after="2026-06-01",
        modified_before="2026-06-30",
        cursor="0",
    )
    allowed = LISTS["routes"]
    listed = [call for call in fake.calls if f"{call.method} {call.path}" in allowed]
    assert listed
    for call in listed:
        key = f"{call.method} {call.path}"
        assert set(call.params) <= set(allowed[key]), key
    v3_entry_lists = [call for call in fake.calls if call.path.endswith("/entry/items")]
    assert v3_entry_lists
    assert all("folderId" not in call.params for call in v3_entry_lists)
    assert "folderId" not in allowed["GET /api/v3/entry/items"]
    assert any(call.params.get("folderId") == "lib_abc123" for call in fake.calls)
