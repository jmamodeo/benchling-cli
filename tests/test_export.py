"""Export writes JSON or CSV and returns a summary, against the fake API."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TENANT, text_cell
from mcp import Client
from typer.testing import CliRunner

from benchling_toolkit import cli
from benchling_toolkit.errors import NotFoundError, UpstreamError, ValidationError
from benchling_toolkit.export import _assay_columns, _cell_value, export_dataset
from benchling_toolkit.mcp_server import build_mcp_server

SUMMARY_KEYS = {
    "kind",
    "format",
    "path",
    "row_count",
    "columns",
    "checksum",
    "checksum_algorithm",
    "source",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _partials(directory: Path) -> list[Path]:
    return list(directory.glob(".*.partial"))


async def _export(runtime, path: Path, **kwargs):
    return await export_dataset(runtime.service, path=str(path), **kwargs)


def _row_calls(fake, suffix: str = "/rows/items") -> list:
    return [call for call in fake.calls if call.path.endswith(suffix)]


async def test_table_export_writes_the_file_and_returns_only_a_summary(
    runtime, fake, tmp_path
) -> None:
    fake.table_page_size = 1
    json_path = tmp_path / "measurements.json"
    summary = await _export(
        runtime,
        json_path,
        kind="TABLE",
        file_format=" Json ",
        part_id=f"  {TABLE_PART}\n",
    )
    assert set(summary) == SUMMARY_KEYS
    assert summary["kind"] == "table"
    assert summary["format"] == "json"
    assert summary["path"] == str(json_path.resolve())
    assert summary["row_count"] == 3
    assert summary["columns"] == ["Sample", "Value"]
    assert summary["checksum"] == _sha256(json_path)
    assert summary["checksum_algorithm"] == "sha256"
    assert summary["source"] == {
        "part_id": TABLE_PART,
        "name": "Measurements",
        "version_id": "ver_1",
    }
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["rows"] == [
        {"Sample": "A", "Value": "1"},
        {"Sample": "B", "Value": ""},
        {"Sample": "C", "Value": "=SUM(A1:A2)"},
    ]
    dumped = json.dumps(summary)
    assert "=SUM(A1:A2)" not in dumped
    assert "rows" not in summary
    assert fake.writes() == []
    assert [call.params.get("nextToken") for call in _row_calls(fake)] == [None, "1", "2"]
    assert _partials(tmp_path) == []

    csv_path = tmp_path / "measurements.csv"
    csv_summary = await _export(
        runtime, csv_path, kind="table", file_format="csv", part_id=TABLE_PART
    )
    assert csv_summary["row_count"] == 3
    assert csv_summary["columns"] == summary["columns"]
    assert csv_summary["checksum"] == _sha256(csv_path)
    assert csv_summary["checksum"] != summary["checksum"]
    assert list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines())) == [
        ["Sample", "Value"],
        ["A", "1"],
        ["B", ""],
        ["C", "=SUM(A1:A2)"],
    ]
    assert "=SUM(A1:A2)" not in json.dumps(csv_summary)


async def test_table_export_uses_column_names_and_the_evaluation(runtime, fake, tmp_path) -> None:
    fake.table_content["name"] = ""
    fake.table_content["columnDefinitions"] = [
        {"name": "Sample"},
        {"name": " ", "systemName": "from_system"},
        {"columnId": "col_x"},
        {"name": " ", "systemName": " ", "columnId": " "},
        "nope",
        {"name": "Sample"},
    ]
    fake.table_content["rows"] = [
        {
            "cells": [
                text_cell("alpha", evaluatedValue={"type": "text_value", "value": "shown-sample"}),
                text_cell("µg/mL"),
            ]
        },
        {"cells": []},
    ]
    path = tmp_path / "wide.json"
    summary = await _export(runtime, path, kind="table", file_format="json", part_id=TABLE_PART)
    assert summary["columns"] == [
        "Sample",
        "from_system",
        "col_x",
        "column_3",
        "column_4",
        "Sample__2",
    ]
    assert summary["source"] == {"part_id": TABLE_PART, "version_id": "ver_1"}
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["rows"][0]["Sample"] == "shown-sample"
    assert document["rows"][0]["from_system"] == "µg/mL"
    assert document["rows"][0]["Sample__2"] is None
    assert document["rows"][1] == {column: None for column in summary["columns"]}
    assert "shown-sample" not in json.dumps(summary)
    assert "µg/mL" not in json.dumps(summary)
    assert "µg/mL" in path.read_text(encoding="utf-8")


async def test_empty_table_export_writes_headers_and_no_rows(runtime, fake, tmp_path) -> None:
    fake.table_content["rows"] = []
    path = tmp_path / "empty.csv"
    summary = await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert summary["row_count"] == 0
    assert summary["columns"] == ["Sample", "Value"]
    assert path.read_text(encoding="utf-8") == "Sample,Value\n"
    assert fake.writes() == []


async def test_non_list_column_definitions_fall_back_to_positions(runtime, fake, tmp_path) -> None:
    fake.table_content["columnDefinitions"] = "Ct"
    fake.table_content["rows"] = [{"cells": [text_cell("only")]}]
    summary = await _export(
        runtime, tmp_path / "fallback.csv", kind="table", file_format="csv", part_id=TABLE_PART
    )
    assert summary["columns"] == ["column_0"]
    assert summary["row_count"] == 1


async def test_results_table_export_follows_short_pages(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.results_parts["docpart_rt1"]["resultSchema"] = None
    fake.results_row_page_size = 1
    path = tmp_path / "plate.csv"
    summary = await _export(
        runtime,
        path,
        kind="Results_Table",
        file_format="csv",
        part_id="docpart_rt1",
    )
    assert summary["kind"] == "results-table"
    assert summary["row_count"] == 3
    assert summary["columns"] == ["Ct"]
    assert summary["source"] == {
        "part_id": "docpart_rt1",
        "name": "qPCR, plate 1",
        "version_id": "ver_1",
    }
    assert "schema_id" not in summary["source"]
    assert list(csv.reader(path.read_text(encoding="utf-8").splitlines())) == [
        ["Ct"],
        ["12"],
        ["1.5"],
        ["ok"],
    ]
    assert "1.5" not in json.dumps(summary)
    assert "strow_1" not in json.dumps(summary)
    assert [call.params.get("nextToken") for call in _row_calls(fake)] == [None, "1", "2"]
    assert fake.writes() == []
    assert _partials(tmp_path) == []


async def test_results_table_export_follows_the_outer_page(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.results_parts["docpart_rt1"]["rows"] = [
        {
            "id": f"strow_{index}",
            "cells": [{"inputValue": {"__typename": "IntegerValue", "value": index}}],
        }
        for index in range(101)
    ]
    path = tmp_path / "long.json"
    summary = await _export(
        runtime, path, kind="results-table", file_format="json", part_id="docpart_rt1"
    )
    assert summary["row_count"] == 101
    assert summary["source"]["schema_id"] == "assaysch_abc123"
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    assert rows[0] == {"Ct": 0}
    assert rows[-1] == {"Ct": 100}
    assert "strow_100" not in json.dumps(summary)
    tokens = [call.params.get("nextToken") for call in _row_calls(fake)]
    assert tokens == [None, "100"]
    assert all(call.method == "GET" for call in fake.calls)


async def test_results_table_export_rejects_a_row_count_mismatch(runtime, fake, tmp_path) -> None:
    fake.add_results_table()
    fake.payload_overrides.append(
        (
            "/document-part-results-table/docpart_rt1",
            200,
            {
                "id": "docpart_rt1",
                "name": "qPCR, plate 1",
                "rowCount": 9,
                "columnDefinitions": [{"name": "Ct"}],
                "rows": f"{TENANT}/api/v3/document-part-results-table/docpart_rt1/rows/items",
                "versionMetadata": {"versionId": "ver_1"},
            },
        )
    )
    path = tmp_path / "bad.csv"
    with pytest.raises(UpstreamError, match="row count"):
        await _export(runtime, path, kind="results-table", file_format="csv", part_id="docpart_rt1")
    assert not path.exists()
    assert _partials(tmp_path) == []


async def test_assay_export_flattens_fields_and_follows_pages(runtime, fake, tmp_path) -> None:
    fake.results = [
        {
            "id": "result_1",
            "entryId": ENTRY_ID,
            "schemaId": "assaysch_abc123",
            "createdAt": "2026-01-01T00:00:00Z",
            "archived": False,
            "fields": {
                "": {"value": "skip-me"},
                "ct": {"value": 12},
                "note": {"textValue": "hello-note"},
                "shown": {"displayValue": "shown-value"},
                "flag": {"value": False},
                "tags": [{"value": "alpha"}, {"value": "beta"}],
                "extra": {"unexpected": 1},
                "plain": 4,
            },
            "entry": {"id": ENTRY_ID},
        },
        {
            "id": "result_2",
            "entryId": ENTRY_ID,
            "schemaId": "assaysch_abc123",
            "fields": {"ct": {"value": 13}, "flag": {"value": True}},
        },
    ]
    fake.results_page_size = 1
    path = tmp_path / "assay.csv"
    summary = await _export(runtime, path, kind="assay-results", file_format="csv")
    assert summary["columns"] == [
        "id",
        "entryId",
        "schemaId",
        "createdAt",
        "ct",
        "note",
        "shown",
        "flag",
        "tags",
        "extra",
        "plain",
        "archived",
    ]
    assert summary["row_count"] == 2
    assert summary["source"] == {}
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[1] == [
        "result_1",
        ENTRY_ID,
        "assaysch_abc123",
        "2026-01-01T00:00:00Z",
        "12",
        "hello-note",
        "shown-value",
        "false",
        '["alpha","beta"]',
        '{"unexpected":1}',
        "4",
        "false",
    ]
    assert rows[2][4] == "13"
    assert rows[2][7] == "true"
    assert rows[2][3] == ""
    assert "skip-me" not in path.read_text(encoding="utf-8")
    assert "hello-note" not in json.dumps(summary)
    assert "entry" not in summary["columns"]
    result_calls = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert [call.params.get("nextToken") for call in result_calls] == [None, "1"]
    assert fake.writes() == []


async def test_assay_export_sends_resolved_filters(runtime, fake, tmp_path) -> None:
    path = tmp_path / "filtered.json"
    summary = await _export(
        runtime,
        path,
        kind="assay_results",
        file_format="json",
        entry="EXP-1",
        schema_id="assaysch_abc123",
    )
    assert summary["row_count"] == 3
    assert summary["columns"] == ["id", "entryId", "schemaId"]
    assert summary["source"] == {"entry_id": ENTRY_ID, "schema_id": "assaysch_abc123"}
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
    assert "result_1" not in json.dumps(summary)
    (call,) = [call for call in fake.calls if call.path.endswith("/result/items")]
    assert call.params["entry.eq"] == ENTRY_ID
    assert call.params["schema.eq"] == "assaysch_abc123"
    assert call.params["pageSize"] == "100"

    empty = tmp_path / "empty.json"
    empty_summary = await _export(
        runtime,
        empty,
        kind="assay-results",
        file_format="json",
        schema_id="assaysch_none",
    )
    assert empty_summary["row_count"] == 0
    assert empty_summary["columns"] == []
    assert empty_summary["source"] == {"schema_id": "assaysch_none"}
    assert json.loads(empty.read_text(encoding="utf-8"))["rows"] == []


async def test_assay_export_stops_on_a_repeated_token(runtime, fake, tmp_path) -> None:
    fake.repeat_next_token = True
    path = tmp_path / "loop.json"
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await _export(runtime, path, kind="assay-results", file_format="json")
    assert not path.exists()
    assert _partials(tmp_path) == []


async def test_export_does_not_write_a_file_when_the_read_fails(runtime, tmp_path) -> None:
    path = tmp_path / "missing.csv"
    with pytest.raises(NotFoundError):
        await _export(runtime, path, kind="table", file_format="csv", part_id="docpart_missing")
    assert not path.exists()


async def test_export_refuses_to_overwrite_without_the_flag(runtime, fake, tmp_path) -> None:
    path = tmp_path / "kept.csv"
    path.write_text("keep-me\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="overwrite"):
        await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert path.read_text(encoding="utf-8") == "keep-me\n"
    assert fake.calls == []

    summary = await _export(
        runtime,
        path,
        kind="table",
        file_format="csv",
        part_id=TABLE_PART,
        overwrite=True,
    )
    assert summary["row_count"] == 3
    assert path.read_text(encoding="utf-8").splitlines()[0] == "Sample,Value"
    assert "keep-me" not in path.read_text(encoding="utf-8")
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": None, "file_format": "json", "part_id": TABLE_PART}, "kind must be"),
        ({"kind": "nope", "file_format": "json", "part_id": TABLE_PART}, "kind must be"),
        ({"kind": "table", "file_format": None, "part_id": TABLE_PART}, "format must be"),
        ({"kind": "table", "file_format": "xml", "part_id": TABLE_PART}, "format must be"),
        ({"kind": "table", "file_format": "csv"}, "require part_id"),
        ({"kind": "table", "file_format": "csv", "part_id": "  "}, "part_id must be"),
        ({"kind": "table", "file_format": "csv", "part_id": 1}, "part_id must be"),
        (
            {"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "entry": ENTRY_ID},
            "assay-results",
        ),
        (
            {
                "kind": "results-table",
                "file_format": "csv",
                "part_id": "docpart_rt1",
                "schema_id": "assaysch_abc123",
            },
            "assay-results",
        ),
        (
            {"kind": "assay-results", "file_format": "csv", "part_id": TABLE_PART},
            "part_id applies only",
        ),
        (
            {"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "overwrite": 1},
            "overwrite must be",
        ),
        ({"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "path": "  "}, "path must"),
        ({"kind": "table", "file_format": "csv", "part_id": TABLE_PART, "path": None}, "path must"),
        (
            {"kind": "assay-results", "file_format": "csv", "entry": "  "},
            "entry must be",
        ),
        (
            {"kind": "assay-results", "file_format": "csv", "schema_id": ""},
            "schema_id must be",
        ),
    ],
)
async def test_export_rejects_bad_arguments_before_any_request(
    runtime, fake, tmp_path, kwargs, match
) -> None:
    path = kwargs.pop("path", str(tmp_path / "out.csv"))
    with pytest.raises(ValidationError, match=match):
        await export_dataset(runtime.service, path=path, **kwargs)
    assert fake.calls == []


async def test_export_rejects_a_directory_and_a_missing_parent(runtime, fake, tmp_path) -> None:
    with pytest.raises(ValidationError, match="not a file"):
        await _export(runtime, tmp_path, kind="table", file_format="csv", part_id=TABLE_PART)
    missing = tmp_path / "missing" / "out.csv"
    with pytest.raises(ValidationError, match="directory does not exist"):
        await _export(runtime, missing, kind="table", file_format="csv", part_id=TABLE_PART)
    assert fake.calls == []


async def test_export_write_errors_leave_no_file(runtime, fake, tmp_path, monkeypatch) -> None:
    path = tmp_path / "new.csv"

    def fail_write(self, data: bytes) -> int:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    with pytest.raises(ValidationError, match="Could not write") as caught:
        await _export(runtime, path, kind="table", file_format="csv", part_id=TABLE_PART)
    assert caught.value.details["reason"] == "read-only file system"
    assert not path.exists()
    assert _partials(tmp_path) == []
    assert fake.writes() == []
    monkeypatch.undo()

    kept = tmp_path / "kept.csv"
    kept.write_text("still-here\n", encoding="utf-8")

    def clash(src: str, dst: str) -> None:
        raise FileExistsError(dst)

    monkeypatch.setattr(os, "link", clash)
    fresh = tmp_path / "race.csv"
    with pytest.raises(ValidationError, match="overwrite"):
        await _export(runtime, fresh, kind="table", file_format="csv", part_id=TABLE_PART)
    assert not fresh.exists()
    assert kept.read_text(encoding="utf-8") == "still-here\n"
    assert _partials(tmp_path) == []


def test_cell_and_field_helpers_cover_empty_values() -> None:
    assert _cell_value({"evaluationValue": {"value": None}, "inputValue": {"value": "kept"}}) == (
        "kept"
    )
    assert _cell_value({}) is None
    assert _cell_value({"inputValue": {"value": ""}}) == ""
    assert _assay_columns([{"fields": {1: {"value": "nope"}, "ct": {"value": 1}}}]) == ["ct"]


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str, env: str | None = "sandbox"):
        prefix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*prefix, *args])
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.exit_code, payload, result

    return run


def test_cli_export_uses_the_envelope_and_exit_codes(invoke, fake, tmp_path) -> None:
    path = tmp_path / "table.csv"
    code, payload, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "CSV",
        "--output",
        str(path),
    )
    assert code == 0
    assert payload["ok"] is True and payload["error"] is None
    assert payload["schema_version"] == "1.0"
    assert payload["meta"]["env"] == "sandbox"
    assert payload["meta"]["tenant_url"] == TENANT
    assert payload["data"]["row_count"] == 3
    assert payload["data"]["checksum"] == _sha256(path)
    assert "=SUM(A1:A2)" not in json.dumps(payload)
    assert fake.writes() == []

    code, refused, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "csv",
        "--output",
        str(path),
    )
    assert code == 2
    assert refused["ok"] is False and refused["data"] is None
    assert refused["error"]["code"] == "validation_error"
    assert path.read_text(encoding="utf-8").splitlines()[1] == "A,1"

    missing = tmp_path / "missing.csv"
    code, not_found, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        "docpart_missing",
        "--format",
        "csv",
        "--output",
        str(missing),
    )
    assert code == 4
    assert not_found["error"]["code"] == "not_found"
    assert not missing.exists()

    code, validation, _ = invoke(
        "export",
        "--kind",
        "table",
        "--part",
        TABLE_PART,
        "--format",
        "csv",
        "--output",
        str(tmp_path / "no-env.csv"),
        env=None,
    )
    assert code == 2
    assert validation["error"]["code"] == "validation_error"


class _Session:
    def __init__(self, server) -> None:
        self.server = server

    async def call_tool(self, name, arguments):
        async with Client(self.server) as active:
            return await active.call_tool(name, arguments)


async def test_mcp_export_returns_a_summary_and_does_not_write_remotely(
    pool, fake, tmp_path
) -> None:
    session = _Session(build_mcp_server(pool))
    path = tmp_path / "assay.json"
    result = await session.call_tool(
        "export",
        {
            "env": "sandbox",
            "kind": "assay-results",
            "path": str(path),
            "format": "json",
            "entry": ENTRY_ID,
        },
    )
    assert result.is_error is False
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["meta"]["env"] == "sandbox"
    assert payload["data"]["row_count"] == 3
    assert payload["data"]["columns"] == ["id", "entryId", "schemaId"]
    assert "result_1" not in json.dumps(payload["data"])
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
    assert fake.writes() == []

    blocked = await session.call_tool(
        "export",
        {"env": "sandbox", "kind": "nope", "path": str(tmp_path / "no.json"), "format": "json"},
    )
    assert blocked.is_error is False
    assert blocked.structured_content["error"]["code"] == "validation_error"
    assert not (tmp_path / "no.json").exists()

    existing = await session.call_tool(
        "export",
        {
            "env": "sandbox",
            "kind": "assay-results",
            "path": str(path),
            "format": "json",
        },
    )
    assert existing.structured_content["error"]["code"] == "validation_error"
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["id"] == "result_1"
