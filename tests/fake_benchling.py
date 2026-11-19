"""A small stateful, offline stand-in for the Benchling REST API.

It is served through ``httpx.MockTransport`` so the real client, service and
planner code runs unmodified. All identifiers and URLs are synthetic.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx

ENTRY_ID = "etr_abc123"
TABLE_PART = "docpart_table1"
TEXT_PART = "docpart_text1"
TENANT = "https://your-tenant-dev.benchling.com"


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, str]
    body: Any


@dataclass
class Fault:
    method: str
    contains: str
    status: int
    skip: int
    times: int


def text_part(part_id: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": part_id,
        "content": {
            "__typename": "TextDocumentContent",
            "text": text,
            "formats": [],
            "links": [],
            "mentions": [],
            "indentation": 0,
            **extra,
        },
    }


def text_cell(value: str, **extra: Any) -> dict[str, Any]:
    return {"inputValue": {"type": "text_value", "value": value}, **extra}


def default_table_content() -> dict[str, Any]:
    return {
        "__typename": "UnstructuredTableDocumentContent",
        "name": "Measurements",
        "columnDefinitions": [{"name": "Sample"}, {"name": "Value"}],
        "isEvaluated": True,
        "rows": [
            {
                "cells": [
                    text_cell("A"),
                    text_cell(
                        "1",
                        evaluatedValue={"type": "text_value", "value": "1"},
                        format={"backgroundColor": "#fff000"},
                        selectorId="selector_1",
                    ),
                ]
            },
            {
                "cells": [
                    text_cell("B", mergeRange={"mergeRowSpan": 1, "mergeColSpan": 2}),
                    text_cell("", mergeRangeParent={"row": 1, "col": 0}),
                ]
            },
            {"cells": [text_cell("C"), text_cell("=SUM(A1:A2)")]},
        ],
    }


def field_definitions() -> list[dict[str, Any]]:
    base = {"derivationType": "DIRECT"}
    return [
        {
            **base,
            "id": "tsf_temp",
            "systemName": "temperature",
            "name": "Temperature",
            "__typename": "IntegerFieldDefinition",
            "isRequired": True,
            "numericMin": 0,
            "numericMax": 100,
        },
        {**base, "id": "tsf_notes", "systemName": "notes", "name": "Notes", "type": "text"},
        {
            **base,
            "id": "tsf_ratio",
            "systemName": "ratio",
            "name": "Ratio",
            "__typename": "FloatFieldDefinition",
            "numericMin": 0.5,
        },
        {
            **base,
            "id": "tsf_flag",
            "systemName": "flag",
            "name": "Flag",
            "__typename": "BooleanFieldDefinition",
        },
        {
            **base,
            "id": "tsf_day",
            "systemName": "run_date",
            "name": "Run date",
            "__typename": "DateFieldDefinition",
        },
        {
            **base,
            "id": "tsf_start",
            "systemName": "started_at",
            "name": "Started at",
            "__typename": "DateTimeFieldDefinition",
        },
        {
            **base,
            "id": "tsf_tags",
            "systemName": "tags",
            "name": "Tags",
            "__typename": "TextFieldDefinition",
            "isMulti": True,
        },
        {
            **base,
            "id": "tsf_meta",
            "systemName": "meta",
            "name": "Meta",
            "__typename": "JsonFieldDefinition",
        },
        {
            **base,
            "id": "tsf_link",
            "systemName": "sample_link",
            "name": "Sample link",
            "__typename": "EntityLinkFieldDefinition",
        },
        {
            **base,
            "id": "tsf_weird",
            "systemName": "weird",
            "name": "Weird",
            "__typename": "MysteryFieldDefinition",
        },
        {
            "id": "tsf_calc",
            "systemName": "calculated",
            "name": "Calculated",
            "__typename": "IntegerFieldDefinition",
            "derivationType": "CALCULATED",
        },
        {
            **base,
            "id": "tsf_old",
            "systemName": "old",
            "name": "Old",
            "__typename": "TextFieldDefinition",
            "archived": True,
        },
    ]


class FakeBenchling:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.faults: list[Fault] = []
        self.token_status = 200
        self.token_body: dict[str, Any] | None = None
        self.access_token = "token"
        self.stamp: str | None = None
        self.entry_version = 1
        self.entry_name = "Synthetic experiment"
        self.display_id = "EXP-1"
        self.parts: list[dict[str, Any]] = [
            {"id": "docpart_sec1", "__typename": "DocumentPartSection", "title": "Methods"},
            text_part(TEXT_PART, "before", formats=[{"type": "bold", "start": 0, "end": 3}]),
            {"id": "docpart_sec2", "__typename": "DocumentPartSection", "title": "Results"},
            {
                "id": TABLE_PART,
                "versionId": "tver_1",
                "content": {"__typename": "UnstructuredTableDocumentContentSummary"},
            },
            {
                "id": "docpart_struct1",
                "content": {"__typename": "ResultsStructuredTableDocumentContent"},
            },
            text_part("docpart_text2", "after"),
        ]
        self.table_version = 1
        self.table_content = default_table_content()
        self.table_page_size: int | None = None
        self.parts_page_size = 100
        self.results_page_size = 100
        self.next_part_number = 1
        self.entry_fields: dict[str, Any] = {"Temperature": 20, "Notes": "n"}
        self.entry_schema_id: str | None = "entsch_abc123"
        self.field_defs = {
            "entsch_abc123": field_definitions(),
            "assaysch_abc123": [
                {
                    "id": "assayfield_ct",
                    "systemName": "ct",
                    "name": "Ct",
                    "__typename": "IntegerFieldDefinition",
                    "isRequired": True,
                    "derivationType": "DIRECT",
                },
                {
                    "id": "assayfield_note",
                    "systemName": "note",
                    "name": "Note",
                    "__typename": "TextFieldDefinition",
                    "derivationType": "DIRECT",
                },
            ],
        }
        self.result_table_schema = "assaysch_abc123"
        self.results: list[dict[str, Any]] = [
            {"id": f"result_{n}", "entryId": ENTRY_ID, "schemaId": "assaysch_abc123"}
            for n in range(1, 4)
        ]
        self.task: dict[str, Any] = {"status": "RUNNING"}
        self.table_payloads: list[dict[str, Any]] = []
        self.entry_patches: list[dict[str, Any]] = []
        self.bulk_payloads: list[dict[str, Any]] = []
        self.bulk_response: dict[str, Any] = {"taskId": "task_abc123"}

    # ---- helpers -----------------------------------------------------
    def fail(
        self, method: str, contains: str, status: int = 500, *, skip: int = 0, times: int = 1
    ) -> None:
        self.faults.append(Fault(method, contains, status, skip, times))

    def writes(self) -> list[Call]:
        return [call for call in self.calls if call.method != "GET" and call.path != "/oauth/token"]

    def part_ids(self) -> list[str]:
        return [part["id"] for part in self.parts]

    def part_text(self, part_id: str) -> str:
        part = next(item for item in self.parts if item["id"] == part_id)
        return str(part["content"]["text"])

    @property
    def version(self) -> str:
        return f"ver_{self.entry_version}"

    # ---- transport ---------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return self._oauth(request)
        body = json.loads(request.content) if request.content else None
        call = Call(request.method, request.url.path, dict(request.url.params), body)
        self.calls.append(call)
        for fault in self.faults:
            if fault.method == call.method and fault.contains in call.path and fault.times > 0:
                if fault.skip > 0:
                    fault.skip -= 1
                    continue
                fault.times -= 1
                if fault.status == 0:
                    raise RuntimeError("simulated local failure")
                body: dict[str, Any] = {"error": {"message": "injected"}}
                if self.stamp:
                    body["note"] = self.stamp
                return httpx.Response(fault.status, json=body, request=request)
        status, payload = self._route(call)
        if self.stamp and call.method != "GET":
            payload = {**payload, "note": self.stamp}
        return httpx.Response(status, json=payload, request=request)

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode() if request.content else "")
        self.calls.append(
            Call(
                request.method,
                request.url.path,
                {},
                {
                    "grant_type": form.get("grant_type", [""])[0],
                    "client_id": form.get("client_id", [""])[0],
                },
            )
        )
        if self.token_status >= 400:
            return httpx.Response(
                self.token_status,
                json=self.token_body or {"error": "invalid_client"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "access_token": self.access_token,
                "token_type": "bearer",
                "expires_in": 900,
            },
            request=request,
        )

    def _route(self, call: Call) -> tuple[int, dict[str, Any]]:
        match = re.fullmatch(r"/api/v(\d)(/.*)", call.path)
        assert match, call.path
        version, path = int(match.group(1)), match.group(2)
        for pattern, name in ROUTES:
            found = re.fullmatch(pattern, path)
            if found and (f"{call.method} v{version}" in name or f"{call.method} *" in name):
                method = getattr(self, "_" + name.split("|")[-1])
                result: tuple[int, dict[str, Any]] = method(call, *found.groups())
                return result
        return 404, {"error": {"message": f"no route {call.method} {call.path}"}}

    # ---- v3 entries --------------------------------------------------
    def _entry_object(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": ENTRY_ID,
            "displayId": self.display_id,
            "name": self.entry_name,
            "versionMetadata": {"versionId": self.version},
        }
        if self.entry_schema_id:
            entry["schema"] = {"id": self.entry_schema_id}
        return entry

    def _get_entry(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        if entry_id != ENTRY_ID:
            return 404, {"error": {"message": "entry not found"}}
        return 200, self._entry_object()

    def _list_entries_v2(self, call: Call) -> tuple[int, dict[str, Any]]:
        wanted = [item for item in call.params.get("displayIds", "").split(",") if item]
        entries = [{"id": ENTRY_ID, "displayId": self.display_id, "name": self.entry_name}]
        return 200, {"entries": [e for e in entries if not wanted or e["displayId"] in wanted]}

    def _page(
        self, items: list[Any], call: Call, size: int | None, key: str = "items"
    ) -> tuple[int, dict[str, Any]]:
        start = int(call.params.get("nextToken") or 0)
        size = size or len(items) or 1
        end = start + size
        payload: dict[str, Any] = {key: items[start:end]}
        payload["nextToken"] = str(end) if end < len(items) else ""
        return 200, payload

    def _parts_items(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        parts = self.parts
        wanted = call.params.get("id.anyOf")
        if wanted:
            parts = [part for part in parts if part["id"] in wanted.split(",")]
        return self._page(deepcopy(parts), call, self.parts_page_size)

    def _patch_entry(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        body = call.body
        self.entry_patches.append(body)
        if "schemaFields" in body:
            for system_name, wrapper in body["schemaFields"].items():
                definition = next(
                    item
                    for item in self.field_defs["entsch_abc123"]
                    if item["systemName"] == system_name
                )
                self.entry_fields[definition["name"]] = wrapper["value"]
            return 200, {"id": entry_id}
        mutation = body["partsMutation"]
        if mutation["version"] != self.version:
            return 409, {"error": {"message": "stale version"}}
        kind = mutation["operationType"]
        if kind == "insert":
            self._insert(mutation["coordinates"]["position"], mutation["parts"])
        elif kind == "delete":
            coords = mutation["coordinates"]
            first = self.part_ids().index(coords["fromDocumentPartId"])
            last = self.part_ids().index(coords["toDocumentPartId"])
            del self.parts[first : last + 1]
        elif kind == "move":
            src = mutation["sourceCoordinates"]
            first = self.part_ids().index(src["fromDocumentPartId"])
            last = self.part_ids().index(src["toDocumentPartId"])
            moved = self.parts[first : last + 1]
            del self.parts[first : last + 1]
            self._insert_existing(mutation["destinationCoordinates"]["position"], moved)
        self.entry_version += 1
        return 200, {"id": entry_id}

    def _insert(self, position: str, new_parts: list[dict[str, Any]]) -> None:
        created = []
        for part in new_parts:
            part_id = f"docpart_new{self.next_part_number}"
            self.next_part_number += 1
            content = {key: value for key, value in part.items() if key != "contentType"}
            content["__typename"] = "TextDocumentContent"
            created.append({"id": part_id, "content": content})
        self._insert_existing(position, created)

    def _insert_existing(self, position: str, parts: list[dict[str, Any]]) -> None:
        if position == "beginning_of_document":
            index = 0
        elif position == "end_of_document":
            index = len(self.parts)
        else:
            index = self.part_ids().index(position) + 1
        self.parts[index:index] = parts

    # ---- tables ------------------------------------------------------
    def _get_table(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        if part_id != TABLE_PART:
            return 404, {"error": {"message": "table not found"}}
        rows = self.table_content["rows"]
        content = deepcopy(self.table_content)
        payload: dict[str, Any] = {"id": part_id, "versionId": f"tver_{self.table_version}"}
        if self.table_page_size:
            start = int(call.params.get("nextToken") or 0)
            end = start + self.table_page_size
            content["rows"] = deepcopy(rows[start:end])
            payload["nextToken"] = str(end) if end < len(rows) else ""
        payload["content"] = content
        return 200, payload

    def _patch_table(self, call: Call, part_id: str) -> tuple[int, dict[str, Any]]:
        body = call.body
        self.table_payloads.append(body)
        if body["versionId"] != f"tver_{self.table_version}":
            return 409, {"error": {"message": "stale table"}}
        self.table_content = {
            **self.table_content,
            **{key: value for key, value in body.items() if key != "versionId"},
        }
        self.table_version += 1
        self.parts[3]["versionId"] = f"tver_{self.table_version}"
        return 200, {"id": part_id, "versionId": f"tver_{self.table_version}"}

    # ---- schemas -----------------------------------------------------
    def _schema(self, call: Call, schema_id: str) -> tuple[int, dict[str, Any]]:
        if schema_id not in self.field_defs:
            return 404, {"error": {"message": "schema not found"}}
        return 200, {"id": schema_id, "name": f"Schema {schema_id}"}

    def _schema_fields(self, call: Call, schema_id: str) -> tuple[int, dict[str, Any]]:
        return self._page(deepcopy(self.field_defs[schema_id]), call, 5)

    # ---- v2 entry / project / task -----------------------------------
    def _get_entry_v2(self, call: Call, entry_id: str) -> tuple[int, dict[str, Any]]:
        entry: dict[str, Any] = {
            "id": ENTRY_ID,
            "folderId": "lib_abc123",
            "modifiedAt": "2026-01-01T00:00:00Z",
            "fields": dict(self.entry_fields),
            "days": [
                {
                    "notes": [
                        {
                            "type": "results_table",
                            "apiId": "strtbl_example",
                            "name": "Assay results",
                            "assayResultSchemaId": self.result_table_schema,
                            "columns": ["ct"],
                        },
                        {"type": "text", "text": "ignored"},
                    ]
                }
            ],
        }
        if self.entry_schema_id:
            entry["schema"] = {"id": self.entry_schema_id}
        return 200, {"entry": entry}

    def _get_folder(self, call: Call, folder_id: str) -> tuple[int, dict[str, Any]]:
        return 200, {"folder": {"id": folder_id, "projectId": "src_abc123"}}

    def _get_project(self, call: Call, project_id: str) -> tuple[int, dict[str, Any]]:
        if project_id != "src_abc123":
            return 404, {"error": {"message": "project not found"}}
        return 200, {"project": {"id": project_id, "name": "Synthetic project"}}

    def _get_task(self, call: Call, task_id: str) -> tuple[int, dict[str, Any]]:
        return 200, dict(self.task)

    def _bulk_create(self, call: Call) -> tuple[int, dict[str, Any]]:
        self.bulk_payloads.append(call.body)
        return 202, dict(self.bulk_response)

    # ---- results -----------------------------------------------------
    def _list_results(self, call: Call) -> tuple[int, dict[str, Any]]:
        items = self.results
        if "entry.eq" in call.params:
            items = [r for r in items if r["entryId"] == call.params["entry.eq"]]
        if "schema.eq" in call.params:
            items = [r for r in items if r["schemaId"] == call.params["schema.eq"]]
        return self._page(deepcopy(items), call, self.results_page_size)

    def _get_result(self, call: Call, result_id: str) -> tuple[int, dict[str, Any]]:
        for item in self.results:
            if item["id"] == result_id:
                return 200, deepcopy(item)
        return 404, {"error": {"message": "result not found"}}

    def _doctor_v3(self, call: Call) -> tuple[int, dict[str, Any]]:
        return 200, {"items": [], "nextToken": ""}


ROUTES: list[tuple[str, str]] = [
    (r"/entry/items", "GET v3|doctor_v3"),
    (r"/entries", "GET v2|list_entries_v2"),
    (r"/entry/(etr_\w+)", "GET v3|get_entry"),
    (r"/entry/(etr_\w+)", "PATCH v3|patch_entry"),
    (r"/entry/(etr_\w+)/parts/items", "GET v3|parts_items"),
    (r"/document-part-unstructured-table/(\w+)", "GET v3|get_table"),
    (r"/document-part-unstructured-table/(\w+)", "PATCH v3|patch_table"),
    (r"/(?:entry|result)-schema/(\w+)", "GET v3|schema"),
    (r"/(?:entry|result)-schema/(\w+)/field-definitions/items", "GET v3|schema_fields"),
    (r"/result/items", "GET v3|list_results"),
    (r"/result/(\w+)", "GET v3|get_result"),
    (r"/entries/(\w+)", "GET v2|get_entry_v2"),
    (r"/folders/(\w+)", "GET v2|get_folder"),
    (r"/projects/(\w+)", "GET v2|get_project"),
    (r"/tasks/(\w+)", "GET v2|get_task"),
    (r"/assay-results:bulk-create", "POST v2|bulk_create"),
]
