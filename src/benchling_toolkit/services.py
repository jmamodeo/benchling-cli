"""Read-side Benchling operations: entry resolution, snapshots, tables, schemas and results."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .api import BenchlingAPI
from .errors import ConflictError, NotFoundError, UnsupportedError, UpstreamError, ValidationError
from .models import CompletenessIssue, EntrySnapshot

ENTRY_ID = re.compile(r"^etr_[A-Za-z0-9]+$")
ENTRY_ID_IN_URL = re.compile(r"/(etr_[A-Za-z0-9]+)(?:[-/]|$)")
KNOWN_PART_TYPES = {
    "DocumentPartSection",
    "DocumentPartText",
    "DocumentPartHeader",
    "DocumentPartListItem",
    "DocumentPartCodeBlock",
    "DocumentPartUnstructuredTable",
    "DocumentPartStructuredTable",
    "DocumentPartResultsStructuredTable",
    "DocumentPartRegistrationStructuredTable",
    "DocumentPartInventoryStructuredTable",
    "DocumentPartAttachment",
    "SectionDocumentContent",
    "TextDocumentContent",
    "ListItemDocumentContent",
    "HeaderDocumentContent",
    "CodeBlockDocumentContent",
    "UnstructuredTableDocumentContentSummary",
    "ResultsStructuredTableDocumentContent",
    "RegistrationStructuredTableDocumentContent",
    "InventoryStructuredTableDocumentContent",
    "StructuredTableDocumentContent",
    "AttachmentDocumentContent",
    "AttachmentPlaceholderDocumentContent",
    "ImageGridDocumentContent",
    "BenchlingItemDocumentContent",
    "RunDocumentContent",
    "TextBoxDocumentContent",
}


def entry_version(entry: dict[str, Any]) -> str:
    """Return the entry's version ID; raises UpstreamError if version metadata is absent."""
    metadata = entry.get("versionMetadata") or {}
    version = metadata.get("versionId") or metadata.get("id")
    if not version:
        raise UpstreamError(
            "Entry response did not include version metadata",
            details={"entry_id": entry.get("id")},
        )
    return str(version)


def part_content(part: dict[str, Any]) -> dict[str, Any]:
    """Support both legacy nested content and current flattened V3 parts.

    Original API objects remain unchanged in output; unknown types stay opaque.
    """
    content = part.get("content")
    return content if isinstance(content, dict) else part


def part_type(part: dict[str, Any]) -> str:
    """Return the part's __typename, or "Unknown" when absent."""
    content = part_content(part)
    return str(content.get("__typename") or "Unknown")


def is_section(part: dict[str, Any]) -> bool:
    """True for section parts, with or without a namespace prefix on the type."""
    return part_type(part).split(".")[-1] in {"SectionDocumentContent", "DocumentPartSection"}


def is_unstructured_table(part: dict[str, Any]) -> bool:
    """True for free-form (unstructured) table parts."""
    return (
        "UnstructuredTableDocumentContent" in part_type(part)
        or part_type(part) == "DocumentPartUnstructuredTable"
    )


def is_structured_table(part: dict[str, Any]) -> bool:
    """True for any structured table part (results, registration, inventory); excludes unstructured."""
    kind = part_type(part)
    return (
        "StructuredTableDocumentContent" in kind
        or kind.startswith("DocumentPart")
        and "StructuredTable" in kind
    ) and "Unstructured" not in kind


def field_definition_type(definition: dict[str, Any]) -> str:
    """Return a stable field type across the V3 union's response variants."""
    explicit = definition.get("type")
    if explicit:
        return str(explicit).upper()
    typename = str(definition.get("__typename") or "").split(".")[-1]
    primitive = {
        "TextFieldDefinition": "TEXT",
        "LongTextFieldDefinition": "LONG_TEXT",
        "FloatFieldDefinition": "FLOAT",
        "IntegerFieldDefinition": "INTEGER",
        "BooleanFieldDefinition": "BOOLEAN",
        "DateFieldDefinition": "DATE",
        "DateTimeFieldDefinition": "DATETIME",
        "JsonFieldDefinition": "JSON",
        "DropdownLinkFieldDefinition": "DROPDOWN",
    }
    if typename in primitive:
        return primitive[typename]
    if typename.endswith("LinkFieldDefinition"):
        return "LINK"
    return "UNKNOWN"


def build_sections(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a flat part list into sections; each records its index range and child part IDs.

    Parts before the first section belong to no section.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, part in enumerate(parts):
        if is_section(part):
            if current:
                current["end_index"] = index - 1
            content = part_content(part)
            current = {
                "part_id": part.get("id"),
                "title": content.get("title"),
                "date": content.get("date"),
                "start_index": index,
                "end_index": index,
                "child_part_ids": [],
            }
            sections.append(current)
        elif current:
            current["child_part_ids"].append(part.get("id"))
            current["end_index"] = index
    return sections


class BenchlingService:
    """Read operations built on BenchlingAPI that return plain JSON-ready dicts."""

    def __init__(self, api: BenchlingAPI) -> None:
        self.api = api

    async def resolve_entry(self, reference: str) -> dict[str, Any]:
        """Resolve an entry ID, tenant URL or display ID to its canonical ID.

        Raises ValidationError for a foreign-tenant URL or ambiguous display ID,
        and NotFoundError when nothing matches.
        """
        reference = reference.strip()
        parsed_id: str | None = None
        if ENTRY_ID.fullmatch(reference):
            parsed_id = reference
        elif reference.startswith("https://"):
            parsed = urlparse(reference)
            if parsed.netloc != urlparse(self.api.environment.base_url).netloc:
                raise ValidationError(
                    "Entry URL belongs to a different tenant",
                    details={"environment_tenant": self.api.environment.base_url, "url": reference},
                )
            match = ENTRY_ID_IN_URL.search(parsed.path)
            parsed_id = match.group(1) if match else None
            if not parsed_id:
                raise ValidationError("URL does not contain a Benchling entry ID")
        if parsed_id:
            entry = await self.api.request("GET", f"/entry/{parsed_id}")
            return {
                "input": reference,
                "entry_id": entry.get("id", parsed_id),
                "display_id": entry.get("displayId"),
                "name": entry.get("name"),
                "resolved_by": "canonical_id",
            }

        matches = [
            item
            async for item in self.api.paginate(
                "/entries",
                version=2,
                params={"displayIds": reference, "pageSize": 100},
                item_key="entries",
            )
        ]
        exact = [item for item in matches if item.get("displayId") == reference]
        if len(exact) > 1:
            raise ValidationError(
                "Entry display ID is ambiguous",
                details={"display_id": reference, "entry_ids": [x.get("id") for x in exact]},
            )
        if not exact:
            raise NotFoundError(
                "No entry matches the supplied ID",
                details={"reference": reference},
            )
        entry = exact[0]
        return {
            "input": reference,
            "entry_id": entry["id"],
            "display_id": entry.get("displayId"),
            "name": entry.get("name"),
            "resolved_by": "display_id",
        }

    async def get_entry(self, reference: str, *, full: bool = False) -> dict[str, Any]:
        """Fetch an entry; with full=True assemble a snapshot of parts, tables and schema.

        A full snapshot is retried once if the entry version changes mid-read,
        then raises ConflictError.
        """
        resolved = await self.resolve_entry(reference)
        entry_id = resolved["entry_id"]
        if not full:
            entry = await self.api.request("GET", f"/entry/{entry_id}")
            return {"resolved": resolved, "entry": entry, "version_id": entry_version(entry)}
        for attempt in range(2):
            first = await self.api.request("GET", f"/entry/{entry_id}")
            version = entry_version(first)
            snapshot = await self._assemble_snapshot(first, version)
            last = await self.api.request("GET", f"/entry/{entry_id}")
            final_version = entry_version(last)
            if final_version == version:
                return {
                    "resolved": resolved,
                    **snapshot.model_dump(mode="json", by_alias=True),
                }
            if attempt == 1:
                raise ConflictError(
                    "Entry changed while the complete snapshot was being assembled",
                    details={
                        "entry_id": entry_id,
                        "initial_version": version,
                        "final_version": final_version,
                    },
                )
        raise AssertionError("unreachable")

    async def inspect_entry(self, reference: str) -> dict[str, Any]:
        """Summarize a full entry snapshot: sections, part summaries, table sizes and completeness."""
        snapshot = await self.get_entry(reference, full=True)
        return {
            "resolved": snapshot["resolved"],
            "entry": snapshot["entry"],
            "version_id": snapshot["version_id"],
            "project_id": (snapshot.get("fields") or {}).get("project_id"),
            "result_tables": snapshot.get("result_tables", []),
            "sections": snapshot["sections"],
            "parts": [
                {
                    "id": part.get("id"),
                    "type": part_type(part),
                    "summary": part_content(part),
                }
                for part in snapshot["parts"]
            ],
            "tables": {
                key: {
                    "part_id": key,
                    "type": value.get("content", {}).get("__typename"),
                    "name": value.get("content", {}).get("name"),
                    "row_count": len(value.get("content", {}).get("rows", [])),
                }
                for key, value in snapshot["tables"].items()
            },
            "schema": snapshot.get("schema"),
            "completeness": snapshot["completeness"],
            "issues": snapshot["issues"],
        }

    async def get_section(self, reference: str, section: str) -> dict[str, Any]:
        """Return one section's parts and tables, matched by part ID or title.

        Raises NotFoundError if none match and ValidationError if the title is ambiguous.
        """
        snapshot = await self.get_entry(reference, full=True)
        matches = [
            item
            for item in snapshot["sections"]
            if item.get("part_id") == section or item.get("title") == section
        ]
        if not matches:
            raise NotFoundError("Section was not found", details={"section": section})
        if len(matches) > 1:
            raise ValidationError(
                "Section title is ambiguous; use its part ID",
                details={"section": section, "part_ids": [x["part_id"] for x in matches]},
            )
        target = matches[0]
        ids = {target["part_id"], *target["child_part_ids"]}
        return {
            "entry_id": snapshot["resolved"]["entry_id"],
            "version_id": snapshot["version_id"],
            "section": target,
            "parts": [part for part in snapshot["parts"] if part.get("id") in ids],
            "tables": {key: value for key, value in snapshot["tables"].items() if key in ids},
        }

    async def get_part(self, reference: str, part_id: str) -> dict[str, Any]:
        """Return one document part and any table detail; raises NotFoundError if absent."""
        snapshot = await self.get_entry(reference, full=True)
        for part in snapshot["parts"]:
            if part.get("id") == part_id:
                return {
                    "entry_id": snapshot["resolved"]["entry_id"],
                    "version_id": snapshot["version_id"],
                    "part": part,
                    "detail": snapshot["tables"].get(part_id),
                }
        raise NotFoundError("Document part was not found", details={"part_id": part_id})

    async def get_table(self, part_id: str) -> dict[str, Any]:
        """Fetch every page of an unstructured table and merge the rows into one response.

        Raises UpstreamError on invalid rows or a repeated pagination token.
        """
        rows: list[dict[str, Any]] = []
        detail: dict[str, Any] | None = None
        params: dict[str, Any] = {"pageSize": 1000}
        seen: set[str] = set()
        while True:
            payload = await self.api.request(
                "GET", f"/document-part-unstructured-table/{part_id}", params=params
            )
            if detail is None:
                detail = payload
            content = part_content(payload)
            page_rows = content.get("rows") or []
            if not isinstance(page_rows, list):
                raise UpstreamError("Table response contained invalid rows")
            rows.extend(page_rows)
            token = payload.get("nextToken")
            if not token:
                break
            if token in seen:
                raise UpstreamError("Table response repeated a pagination token")
            seen.add(token)
            params["nextToken"] = token
        assert detail is not None
        part_content(detail)["rows"] = rows
        detail["pagination"] = {"rows": len(rows), "complete": True}
        return detail

    async def get_table_for_entry(self, reference: str, part_id: str) -> dict[str, Any]:
        """Resolve and verify that an unstructured table part belongs to one entry."""
        resolved = await self.resolve_entry(reference)
        matches = [
            item
            async for item in self.api.paginate(
                f"/entry/{resolved['entry_id']}/parts/items",
                params={"id.anyOf": part_id, "pageSize": 100},
            )
            if item.get("id") == part_id
        ]
        if not matches:
            raise NotFoundError(
                "Table part does not belong to the selected entry",
                details={"entry_id": resolved["entry_id"], "part_id": part_id},
            )
        if len(matches) > 1:
            raise UpstreamError(
                "Benchling returned a duplicate document part",
                details={"entry_id": resolved["entry_id"], "part_id": part_id},
            )
        part = matches[0]
        if not is_unstructured_table(part):
            raise UnsupportedError(
                "Only unstructured table document parts can be edited",
                details={"part_id": part_id, "type": part_type(part)},
            )
        table = await self.get_table(part_id)
        part_version = part.get("versionId") or (part.get("versionMetadata") or {}).get("versionId")
        table_version = table.get("versionId")
        if part_version and table_version and part_version != table_version:
            raise ConflictError(
                "Table changed while its target entry was being verified",
                details={
                    "part_version": part_version,
                    "table_version": table_version,
                    "part_id": part_id,
                },
            )
        return {"resolved": resolved, "part": part, "table": table}

    async def get_schema(self, schema_id: str) -> dict[str, Any]:
        """Return a result or entry schema (chosen by ID prefix) with all its field definitions."""
        if schema_id.startswith("assaysch_"):
            base = f"/result-schema/{schema_id}"
        else:
            base = f"/entry-schema/{schema_id}"
        schema = await self.api.request("GET", base)
        fields = [
            item
            async for item in self.api.paginate(
                f"{base}/field-definitions/items", params={"pageSize": 100}
            )
        ]
        return {"schema": schema, "field_definitions": fields}

    async def get_entry_fields(self, reference: str) -> dict[str, Any]:
        """Return an entry's custom fields keyed by raw and system name, with schema, project and result tables."""
        resolved = await self.resolve_entry(reference)
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{resolved['entry_id']}", version=2), "entry"
        )
        schema_ref = entry.get("schema")
        schema_detail: dict[str, Any] | None = None
        definitions: list[dict[str, Any]] = []
        if isinstance(schema_ref, dict) and schema_ref.get("id"):
            schema_detail = await self.get_schema(str(schema_ref["id"]))
            definitions = schema_detail["field_definitions"]
        raw_fields = entry.get("fields") or {}
        fields_by_system_name: dict[str, Any] = {}
        for definition in definitions:
            system_name = definition.get("systemName")
            display_name = definition.get("name")
            if not system_name:
                continue
            if display_name in raw_fields:
                value = raw_fields[display_name]
            elif system_name in raw_fields:
                value = raw_fields[system_name]
            else:
                value = None
            fields_by_system_name[str(system_name)] = value
        return {
            "entry_id": resolved["entry_id"],
            "schema": schema_ref,
            "schema_detail": schema_detail,
            "field_definitions": definitions,
            "fields": raw_fields,
            "fields_by_system_name": fields_by_system_name,
            "modified_at": entry.get("modifiedAt"),
            "project_id": await self._entry_project_id(entry),
            "result_tables": self._result_tables_from_entry(entry),
        }

    def _v2_object(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        obj = payload.get(key, payload)
        if not isinstance(obj, dict) or not obj.get("id"):
            raise UpstreamError(
                "V2 response did not contain the expected object", details={"object": key}
            )
        return obj

    async def _entry_project_id(self, entry: dict[str, Any]) -> str | None:
        project = entry.get("project") or {}
        if project.get("id"):
            return str(project["id"])
        if entry.get("folderId"):
            folder = self._v2_object(
                await self.api.request("GET", f"/folders/{entry['folderId']}", version=2), "folder"
            )
            return folder.get("projectId")
        return None

    def _result_tables_from_entry(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        tables = []
        seen: set[str] = set()
        for day in entry.get("days") or []:
            for note in day.get("notes") or []:
                if note.get("type") != "results_table":
                    continue
                table_id = note.get("apiId")
                if not isinstance(table_id, str) or not table_id.startswith("strtbl_"):
                    raise UpstreamError("Result table has no valid API identifier")
                if table_id in seen:
                    raise UpstreamError("Benchling returned a duplicate result-table identifier")
                seen.add(table_id)
                tables.append(
                    {
                        "entry_id": entry["id"],
                        "table_id": table_id,
                        "name": note.get("name"),
                        "schema_id": note.get("assayResultSchemaId"),
                        "columns": note.get("columns") or [],
                    }
                )
        return tables

    async def list_result_tables(self, reference: str) -> dict[str, Any]:
        """List the V2 Results tables embedded in an entry, with its project ID."""
        resolved = await self.resolve_entry(reference)
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{resolved['entry_id']}", version=2), "entry"
        )
        return {
            "resolved": resolved,
            "project_id": await self._entry_project_id(entry),
            "items": self._result_tables_from_entry(entry),
            "complete": True,
            "id_namespace": "v2_structured_table",
        }

    async def get_result_table(self, entry_id: str, table_id: str) -> dict[str, Any]:
        """Find an explicit V2 Results table within one entry."""
        entry = self._v2_object(
            await self.api.request("GET", f"/entries/{entry_id}", version=2), "entry"
        )
        matches: list[dict[str, Any]] = []
        for day in entry.get("days") or []:
            for note in day.get("notes") or []:
                if note.get("type") == "results_table" and note.get("apiId") == table_id:
                    matches.append(note)
        if not matches:
            raise NotFoundError(
                "Result table was not found on the selected entry",
                details={"entry_id": entry_id, "table_id": table_id},
            )
        if len(matches) > 1:
            raise UpstreamError(
                "Benchling returned the same result-table ID more than once",
                details={"entry_id": entry_id, "table_id": table_id},
            )
        note = matches[0]
        return {
            "entry_id": entry_id,
            "table_id": table_id,
            "name": note.get("name"),
            "schema_id": note.get("assayResultSchemaId"),
            "columns": note.get("columns") or [],
        }

    async def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a V2 project; raises UpstreamError if the response lacks an object ID."""
        return self._v2_object(
            await self.api.request("GET", f"/projects/{project_id}", version=2), "project"
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a V2 task; raises UpstreamError if the response has no string status."""
        payload = await self.api.request("GET", f"/tasks/{task_id}", version=2)
        task = payload.get("task", payload)
        # Task responses may be a flat status object without an id property.
        if not isinstance(task, dict) or not isinstance(task.get("status"), str):
            raise UpstreamError("V2 task response did not contain a status")
        return task

    async def list_results(
        self,
        *,
        entry: str | None = None,
        schema_id: str | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of results, optionally filtered by entry and schema.

        page_size must be 1-100 (ValidationError otherwise); next_token is None on the last page.
        """
        if page_size < 1 or page_size > 100:
            raise ValidationError("page_size must be between 1 and 100")
        params: dict[str, Any] = {"pageSize": page_size}
        if cursor:
            params["nextToken"] = cursor
        if entry:
            params["entry.eq"] = (await self.resolve_entry(entry))["entry_id"]
        if schema_id:
            params["schema.eq"] = schema_id
        payload = await self.api.request("GET", "/result/items", params=params)
        return {
            "items": payload.get("items", []),
            "next_token": payload.get("nextToken") or None,
        }

    async def get_result(self, result_id: str) -> dict[str, Any]:
        """Fetch a single result by ID."""
        return await self.api.request("GET", f"/result/{result_id}")

    async def doctor(self) -> dict[str, Any]:
        """Probe V3 and V2 entry endpoints; failures are reported per check rather than raised."""
        checks: list[dict[str, Any]] = []
        for name, version, path, params in (
            ("v3_entries", 3, "/entry/items", {"pageSize": 1}),
            ("v2_entries", 2, "/entries", {"pageSize": 1}),
        ):
            try:
                await self.api.request("GET", path, version=version, params=params)
                checks.append({"name": name, "ok": True})
            except Exception as exc:
                checks.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": getattr(exc, "message", str(exc)),
                        "code": getattr(exc, "code", type(exc).__name__),
                    }
                )
        by_name = {check["name"]: check["ok"] for check in checks}
        v3_ok = by_name.get("v3_entries", False)
        v2_ok = by_name.get("v2_entries", False)
        return {
            "env": self.api.environment.name,
            "tenant_url": self.api.environment.base_url,
            "auth_method": self.api.environment.auth_method.value,
            "early_access_header": self.api.environment.early_access,
            "checks": checks,
            "capabilities": {
                "document_parts": v3_ok,
                "entry_fields": v3_ok and v2_ok,
                "unstructured_table_replace": v3_ok,
                "structured_table_edit": False,
                "result_upload_pending_integration": v2_ok,
                "mcp_transport": "stdio",
            },
            "ok": all(check["ok"] for check in checks),
        }

    async def _assemble_snapshot(self, entry: dict[str, Any], version: str) -> EntrySnapshot:
        entry_id = str(entry["id"])
        parts = [
            item
            async for item in self.api.paginate(
                f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
            )
        ]
        issues: list[CompletenessIssue] = []
        tables = await self._collect_part_tables(parts, issues)
        schema = await self._fetch_entry_schema(entry, issues)
        fields = await self._fetch_entry_fields(entry_id, issues)
        results, result_retrieval = await self._fetch_linked_results(entry_id, issues)

        return EntrySnapshot(
            entry=entry,
            version_id=version,
            parts=parts,
            sections=build_sections(parts),
            tables=tables,
            entry_schema=schema,
            fields=fields,
            result_tables=(fields or {}).get("result_tables", []),
            results=results,
            result_retrieval=result_retrieval,
            completeness="partial" if issues else "complete",
            issues=issues,
        )

    async def _collect_part_tables(
        self, parts: list[dict[str, Any]], issues: list[CompletenessIssue]
    ) -> dict[str, dict[str, Any]]:
        """Fetch unstructured table details, recording issues for unknown or unavailable parts."""
        tables: dict[str, dict[str, Any]] = {}
        for part in parts:
            part_id = part.get("id")
            kind = part_type(part).split(".")[-1]
            if kind not in KNOWN_PART_TYPES:
                issues.append(
                    CompletenessIssue(
                        scope=f"part:{part_id or 'unknown'}",
                        reason="unknown_part_type_preserved_as_opaque_data",
                        details={"type": part_type(part)},
                    )
                )
            if is_unstructured_table(part) and part_id:
                try:
                    tables[part_id] = await self.get_table(part_id)
                except Exception as exc:
                    issues.append(
                        CompletenessIssue(
                            scope=f"table:{part_id}",
                            reason="detail_unavailable",
                            details={"error": getattr(exc, "message", str(exc))},
                        )
                    )
            elif is_structured_table(part) and part_id:
                issues.append(
                    CompletenessIssue(
                        scope=f"table:{part_id}",
                        reason="structured_table_detail_not_available_through_document_api",
                    )
                )
        return tables

    async def _fetch_entry_schema(
        self, entry: dict[str, Any], issues: list[CompletenessIssue]
    ) -> dict[str, Any] | None:
        """Fetch the entry's schema, recording an issue if it is unavailable."""
        schema_ref = entry.get("typedSchema") or entry.get("schema")
        if not (isinstance(schema_ref, dict) and schema_ref.get("id")):
            return None
        try:
            return await self.get_schema(str(schema_ref["id"]))
        except Exception as exc:
            issues.append(
                CompletenessIssue(
                    scope="entry_schema",
                    reason="schema_unavailable",
                    details={"error": getattr(exc, "message", str(exc))},
                )
            )
            return None

    async def _fetch_entry_fields(
        self, entry_id: str, issues: list[CompletenessIssue]
    ) -> dict[str, Any] | None:
        """Fetch V2 entry fields, recording an issue if they are unavailable."""
        try:
            return await self.get_entry_fields(entry_id)
        except Exception as exc:
            issues.append(
                CompletenessIssue(
                    scope="entry_fields",
                    reason="v2_fields_unavailable",
                    details={"error": getattr(exc, "message", str(exc))},
                )
            )
            return None

    async def _fetch_linked_results(
        self, entry_id: str, issues: list[CompletenessIssue]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Page through all results linked to the entry; returns records and retrieval metadata."""
        results: list[dict[str, Any]] = []
        result_retrieval: dict[str, Any] = {
            "consistency": "independent_non_atomic",
            "entry_filter": entry_id,
            "started_at": datetime.now(UTC).isoformat(),
            "pages": 0,
            "complete": False,
        }
        try:
            cursor: str | None = None
            while True:
                page = await self.list_results(entry=entry_id, page_size=100, cursor=cursor)
                result_retrieval["pages"] += 1
                results.extend(page["items"])
                cursor = page["next_token"]
                if not cursor:
                    break
            result_retrieval["complete"] = True
        except Exception as exc:
            result_retrieval["error"] = getattr(exc, "message", str(exc))
            issues.append(
                CompletenessIssue(
                    scope="results",
                    reason="linked_results_unavailable",
                    details={"error": getattr(exc, "message", str(exc))},
                )
            )
        result_retrieval["finished_at"] = datetime.now(UTC).isoformat()
        result_retrieval["records"] = len(results)
        return results, result_retrieval
