"""Write a Benchling table or assay results to a local file and return a summary.

The file holds the rows. The return value is row count, column names, path, and
a SHA-256 checksum, so a large table does not enter an agent's context. Reads
follow every page. Benchling is not modified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import listing
from .errors import UpstreamError, ValidationError
from .redact import redact_text
from .services import BenchlingService

_KINDS = {"table", "results-table", "assay-results"}
_FORMATS = {"json", "csv"}
_IDENTITY = ("id", "entryId", "schemaId", "projectId", "createdAt", "modifiedAt")
_COLUMN_KEYS = ("name", "systemName", "columnId")


async def export_dataset(
    service: BenchlingService,
    *,
    kind: object,
    path: object,
    file_format: object,
    overwrite: object = False,
    part_id: object = None,
    entry: object = None,
    schema_id: object = None,
) -> dict[str, Any]:
    """Read one dataset, write it to ``path``, and return a summary.

    ``kind`` is ``table``, ``results-table``, or ``assay-results``. ``file_format``
    is ``json`` or ``csv``. Table and results-table exports require ``part_id``.
    Assay-result exports accept ``entry`` and ``schema_id`` and read every page.
    An existing file is left unchanged unless ``overwrite`` is true.
    """
    selected = _kind(kind)
    selected_format = _format(file_format)
    replace = _overwrite(overwrite)
    part = _optional(part_id, "part_id")
    entry_ref = _optional(entry, "entry")
    schema_ref = _optional(schema_id, "schema_id")
    _check_selector(selected, part, entry_ref, schema_ref)
    destination = _destination(path, overwrite=replace)
    if selected == "assay-results":
        columns, rows, source = await _load_assay_results(service, entry_ref, schema_ref)
    elif part is None:
        raise ValidationError("table and results-table exports require part_id")
    elif selected == "results-table":
        columns, rows, source = await _load_results_table(service, part)
    else:
        columns, rows, source = await _load_table(service, part)
    payload = _render(selected, columns, rows, source, file_format=selected_format)
    _write(destination, payload, overwrite=replace)
    return {
        "kind": selected,
        "format": selected_format,
        "path": str(destination),
        "row_count": len(rows),
        "columns": columns,
        "checksum": hashlib.sha256(payload).hexdigest(),
        "checksum_algorithm": "sha256",
        "source": source,
    }


def _kind(value: object) -> str:
    key = value.strip().casefold().replace("_", "-") if isinstance(value, str) else ""
    if key in _KINDS:
        return key
    raise ValidationError(
        "kind must be table, results-table, or assay-results",
        details={"kind": value.strip() if isinstance(value, str) else None},
    )


def _format(value: object) -> str:
    key = value.strip().casefold() if isinstance(value, str) else ""
    if key in _FORMATS:
        return key
    raise ValidationError(
        "format must be json or csv",
        details={"format": value.strip() if isinstance(value, str) else None},
    )


def _overwrite(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError("overwrite must be true or false")


def _optional(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _check_selector(
    kind: str, part_id: str | None, entry: str | None, schema_id: str | None
) -> None:
    if kind in {"table", "results-table"}:
        if entry is not None or schema_id is not None:
            raise ValidationError("entry and schema apply only to assay-results")
        return
    if part_id is not None:
        raise ValidationError("part_id applies only to table and results-table exports")


def _destination(value: object, *, overwrite: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("path must be a non-empty string")
    path = Path(value.strip()).expanduser().resolve()
    if path.exists() and not path.is_file():
        raise ValidationError("Export path is not a file", details={"path": str(path)})
    if path.exists() and not overwrite:
        raise ValidationError(
            "Refusing to overwrite an existing file without overwrite",
            details={"path": str(path)},
        )
    if not path.parent.is_dir():
        raise ValidationError("Export directory does not exist", details={"path": str(path)})
    return path


async def _load_table(
    service: BenchlingService, part_id: str
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Read every unstructured-table row page via the existing table read."""
    table = await service.get_table(part_id)
    content = table["content"]
    raw_rows = content["rows"]
    columns = _column_names(content.get("columnDefinitions"), _width(raw_rows))
    source: dict[str, Any] = {"part_id": str(table.get("id") or part_id)}
    _add_text(source, "name", content.get("name"))
    version = (table.get("versionMetadata") or {}).get("versionId")
    _add_text(source, "version_id", version)
    return columns, [_table_row(row, columns) for row in raw_rows], source


async def _load_results_table(
    service: BenchlingService, part_id: str
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Follow results-table row pages until next_token is absent."""
    first = await service.list_results_table_rows(part_id, limit=listing.LIST_LIMIT_MAX)
    collected: list[dict[str, Any]] = list(first["items"])
    cursor = first["next_token"]
    while cursor:
        page = await service.list_results_table_rows(
            part_id, limit=listing.LIST_LIMIT_MAX, cursor=cursor
        )
        collected.extend(page["items"])
        cursor = page["next_token"]
    expected = first["row_count"]
    if expected != len(collected):
        raise UpstreamError(
            "Table row count did not match the rows that were returned",
            details={"part_id": part_id, "row_count": expected, "returned": len(collected)},
        )
    columns = _column_names(first.get("column_definitions"), _width(collected))
    source: dict[str, Any] = {"part_id": str(first.get("id") or part_id)}
    _add_text(source, "name", first.get("name"))
    _add_text(source, "version_id", first.get("version_id"))
    _add_text(source, "schema_id", first.get("result_schema_id"))
    return columns, [_table_row(row, columns) for row in collected], source


async def _load_assay_results(
    service: BenchlingService, entry: str | None, schema_id: str | None
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Read every assay-result page for the optional entry and schema filters."""
    collected = await service.list_all_results(entry=entry, schema_id=schema_id)
    records = collected["items"]
    columns = _assay_columns(records)
    source: dict[str, Any] = {}
    _add_text(source, "entry_id", collected.get("entry_id"))
    _add_text(source, "schema_id", collected.get("schema_id"))
    return columns, [_assay_row(record, columns) for record in records], source


def _add_text(source: dict[str, Any], key: str, value: object) -> None:
    if isinstance(value, str) and value:
        source[key] = value


def _width(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return max(len(row["cells"]) for row in rows)


def _column_names(definitions: object, width: int) -> list[str]:
    defs = definitions if isinstance(definitions, list) else []
    count = max(len(defs), width)
    names = [
        _column_name(defs[index] if index < len(defs) else None, index) for index in range(count)
    ]
    return _unique(names)


def _column_name(definition: object, index: int) -> str:
    if isinstance(definition, dict):
        for key in _COLUMN_KEYS:
            candidate = definition.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return f"column_{index}"


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique.append(name if count == 1 else f"{name}__{count}")
    return unique


def _table_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    values = [_cell_value(cell) for cell in row["cells"]]
    values.extend([None] * (len(columns) - len(values)))
    return {column: values[index] for index, column in enumerate(columns)}


def _cell_value(cell: dict[str, Any]) -> Any:
    """Prefer a computed evaluation, otherwise the value that was entered."""
    evaluation = cell.get("evaluationValue")
    if isinstance(evaluation, dict) and evaluation.get("value") is not None:
        return evaluation["value"]
    entered = cell.get("inputValue")
    if isinstance(entered, dict) and "value" in entered:
        return entered["value"]
    return None


def _assay_columns(records: list[dict[str, Any]]) -> list[str]:
    present = {key for record in records for key in record}
    columns = [key for key in _IDENTITY if key in present]
    seen = set(columns)
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        for name in fields:
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            columns.append(name)
    for record in records:
        for key, value in record.items():
            if key in seen or key == "fields" or isinstance(value, dict | list):
                continue
            seen.add(key)
            columns.append(key)
    return columns


def _assay_row(record: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    return {column: _assay_value(record, column) for column in columns}


def _assay_value(record: dict[str, Any], column: str) -> Any:
    fields = record.get("fields")
    if isinstance(fields, dict) and column in fields and column not in _IDENTITY:
        return _unwrap_field(fields[column])
    return record.get(column)


def _unwrap_field(value: Any) -> Any:
    """Use a field's value, including each item of a multi-value field."""
    if isinstance(value, list):
        return [_unwrap_field(item) for item in value]
    if isinstance(value, dict):
        if "value" in value:
            return value["value"]
        if "textValue" in value:
            return value["textValue"]
        if "displayValue" in value:
            return value["displayValue"]
    return value


def _render(
    kind: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    source: dict[str, Any],
    *,
    file_format: str,
) -> bytes:
    if file_format == "json":
        document = {"kind": kind, "source": source, "columns": columns, "rows": rows}
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[column]) for column in columns])
    return buffer.getvalue().encode("utf-8")


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _write(path: Path, payload: bytes, *, overwrite: bool) -> None:
    """Write ``payload`` atomically. ``os.link`` refuses to replace an existing file."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        temporary.write_bytes(payload)
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValidationError(
                    "Refusing to overwrite an existing file without overwrite",
                    details={"path": str(path)},
                ) from exc
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(
            "Could not write export file",
            details={"path": str(path), "reason": redact_text(str(exc))},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
