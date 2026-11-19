"""Every JSON body the client sends must satisfy the vendored OpenAPI request schema."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "contracts" / "openapi-manifest.json").read_text())

_V3_ENTRY = re.compile(r"/api/v3/entry/[^/]+$")
_V2_BULK = re.compile(r"/api/v2/assay-results:bulk-create$")


def _contract(name: str) -> dict[str, Any]:
    matches = [item for item in MANIFEST["contracts"] if item["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"manifest is missing {name}")
    return matches[0]


def test_vendored_specs_match_the_manifest_checksums() -> None:
    for name in ("benchling-v3", "benchling-v2"):
        contract = _contract(name)
        snapshot = ROOT / str(contract["snapshot"])
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        assert digest == contract["sha256"]
        assert contract["captured_at"] == "2026-11-19"


@lru_cache(maxsize=4)
def _spec(name: str) -> dict[str, Any]:
    contract = _contract(name)
    return yaml.safe_load((ROOT / str(contract["snapshot"])).read_text())


def _validator(spec_name: str, schema_name: str) -> Draft202012Validator:
    spec = _spec(spec_name)
    uri = f"https://benchling.example/{spec_name}.yaml"
    registry = Registry().with_resource(
        uri, Resource.from_contents(spec, default_specification=DRAFT202012)
    )
    return Draft202012Validator(
        {"$ref": f"{uri}#/components/schemas/{schema_name}"},
        registry=registry,
    )


def _schema_errors(spec_name: str, schema_name: str, instance: Any) -> list[str]:
    validator = _validator(spec_name, schema_name)
    return [
        error.message
        for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    ]


def _request_schema(call: Any) -> tuple[str, str] | None:
    if call.method == "PATCH" and _V3_ENTRY.search(call.path):
        return "benchling-v3", "UpdateEntryInput"
    if call.method == "POST" and _V2_BULK.search(call.path):
        return None
    raise AssertionError(f"no request schema for {call.method} {call.path}")


def _bulk_create_errors(body: dict[str, Any]) -> list[str]:
    """Validate a bulk-create body against the two schemas the V2 contract composes.

    `AssayResultsBulkCreateInTableRequest` is an allOf of a schema that forbids
    additional properties and a schema that adds `tableId`. Standard JSON Schema
    therefore rejects `tableId`, which the live API requires. Check each published
    branch, and record that the composed schema still rejects the live body.
    """
    composed = _schema_errors("benchling-v2", "AssayResultsBulkCreateInTableRequest", body)
    if not composed:
        return []
    if not any("tableId" in message for message in composed):
        return composed
    if not isinstance(body.get("tableId"), str) or not body["tableId"]:
        return ["tableId must be the string declared on AssayResultsBulkCreateInTableRequest"]
    without_table = {key: value for key, value in body.items() if key != "tableId"}
    return _schema_errors("benchling-v2", "AssayResultsBulkCreateRequest", without_table)


def test_retired_document_coordinates_fail_the_current_schema() -> None:
    """The drift that sent `coordinates` must be rejected by the vendored schema."""
    body = {
        "partsMutation": {
            "operationType": "insert",
            "version": "etrver_example",
            "coordinates": {"position": "end_of_document"},
            "parts": [{"contentType": "text", "text": "hello"}],
        }
    }
    errors = _schema_errors("benchling-v3", "UpdateEntryInput", body)
    assert errors
    assert any("coordinates" in message for message in errors)


async def test_every_client_request_body_matches_the_openapi_schema(runtime, fake) -> None:
    text = {"contentType": "text", "text": "introduced"}
    await _apply(
        runtime,
        "document",
        {"operation": "insert", "position": "end_of_document", "parts": [text]},
    )
    await _apply(
        runtime,
        "document",
        {
            "operation": "move",
            "from_part_id": "docpart_text2",
            "position": "beginning_of_document",
        },
    )
    await _apply(runtime, "document", {"operation": "delete", "from_part_id": "docpart_text2"})
    await _apply(
        runtime, "document", {"operation": "replace_text", "part_id": TEXT_PART, "text": "edited"}
    )
    await _apply(
        runtime,
        "table",
        {
            "operation": "set_cells",
            "table_part_id": TABLE_PART,
            "updates": [{"row": 0, "column": 1, "value": "1.25"}],
        },
    )
    await _apply(
        runtime,
        "table",
        {"operation": "append_rows", "table_part_id": TABLE_PART, "rows": [["D", "4"]]},
    )
    await _apply(runtime, "fields", {"schema_fields": {"temperature": {"value": 21}}})
    await _apply(
        runtime,
        "results",
        {
            "table_id": "strtbl_example",
            "assay_results": [
                {
                    "schemaId": "assaysch_abc123",
                    "fields": {"ct": {"value": 12}, "note": {"value": "n"}},
                }
            ],
        },
    )

    writes = [call for call in fake.calls if call.method != "GET" and call.path != "/oauth/token"]
    assert writes
    operations = set()
    for call in writes:
        assert isinstance(call.body, dict)
        schema = _request_schema(call)
        if schema is None:
            errors = _bulk_create_errors(call.body)
        else:
            errors = _schema_errors(schema[0], schema[1], call.body)
        assert errors == [], (call.method, call.path, errors, call.body)
        mutation = call.body.get("partsMutation")
        if isinstance(mutation, dict):
            operations.add(mutation["operationType"])
            assert "coordinates" not in mutation
            assert "sourceCoordinates" not in mutation
            assert "destinationCoordinates" not in mutation
    assert operations == {
        "insert",
        "delete",
        "move",
        "update_table_cells",
        "insert_table_rows",
    }
    field_bodies = [call.body["schemaFields"] for call in writes if "schemaFields" in call.body]
    assert field_bodies == [{"tsf_temp": {"value": 21}}]


async def _apply(runtime, family: str, request: dict[str, Any]) -> None:
    planned = await runtime.planner.plan(ENTRY_ID, family, request)
    await runtime.planner.apply(planned.id)
