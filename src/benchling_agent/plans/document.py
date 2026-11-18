"""Document-part change planning."""

from __future__ import annotations

from typing import Any

from ..errors import (
    BenchlingAgentError,
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import (
    entry_version,
    part_content,
    part_type,
)
from .base import PlannerBase, PlanParts


class DocumentPlanning(PlannerBase):
    """Planning and applying of document-part changes."""

    async def _plan_document(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        snapshot = await self.service.get_entry(entry_id, full=True)
        operation = request.get("operation")
        parts = snapshot["parts"]
        by_id = {part.get("id"): part for part in parts}
        if operation == "insert":
            new_parts = request.get("parts")
            position = request.get("position", "end_of_document")
            if not isinstance(new_parts, list) or not new_parts:
                raise ValidationError("Document insert requires a non-empty parts list")
            if any(
                not isinstance(part, dict) or not isinstance(part.get("contentType"), str)
                for part in new_parts
            ):
                raise ValidationError("Every inserted document part requires a contentType")
            if (
                position not in {"beginning_of_document", "end_of_document"}
                and position not in by_id
            ):
                raise ValidationError("Insert position is not a document part")
            before = {"version_id": snapshot["version_id"], "position": position, "parts": []}
            after = {"version_id": "changes_on_apply", "position": position, "parts": new_parts}
        elif operation == "delete":
            first = request.get("from_part_id")
            last = request.get("to_part_id", first)
            selected = self._part_range(parts, first, last)
            before = {"version_id": snapshot["version_id"], "parts": selected}
            after = {"version_id": "changes_on_apply", "parts": []}
        elif operation == "move":
            first = request.get("from_part_id")
            last = request.get("to_part_id", first)
            selected = self._part_range(parts, first, last)
            destination = request.get("position")
            if (
                destination not in {"beginning_of_document", "end_of_document"}
                and destination not in by_id
            ):
                raise ValidationError("Move destination is not a document part")
            if destination in {part.get("id") for part in selected}:
                raise ValidationError("Move destination cannot be inside the source range")
            before = {"version_id": snapshot["version_id"], "parts": selected}
            after = {"version_id": "changes_on_apply", "position": destination, "parts": selected}
        elif operation == "replace_text":
            part_id = request.get("part_id")
            original = by_id.get(part_id)
            if not original:
                raise ValidationError("Text replacement target was not found")
            if part_type(original).split(".")[-1] not in {
                "TextDocumentContent",
                "DocumentPartText",
            }:
                raise UnsupportedError("replace_text only supports text document parts")
            if not isinstance(request.get("text"), str):
                raise ValidationError("replace_text requires text")
            before = {"version_id": snapshot["version_id"], "part": original}
            content = part_content(original)
            replacement = {
                "contentType": "text",
                "text": request["text"],
                "formats": request.get("formats", content.get("formats", [])),
                "links": request.get("links", content.get("links", [])),
                "mentions": request.get("mentions", content.get("mentions", [])),
                "indentation": request.get("indentation", content.get("indentation", 0)),
            }
            after = {"version_id": "changes_on_apply", "part": replacement}
        else:
            raise ValidationError(
                "Unknown document operation",
                details={"supported": ["insert", "delete", "move", "replace_text"]},
            )
        return (
            before,
            after,
            {
                "entry_version": snapshot["version_id"],
                "part_ids": [part.get("id") for part in parts if part.get("id")],
            },
            ["server-enforced document version check"],
            (
                ["Text replacement requires two versioned requests and can partially complete"]
                if operation == "replace_text"
                else []
            ),
        )

    async def _apply_document(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        current = await self.api.request("GET", f"/entry/{plan.entry_id}")
        version = entry_version(current)
        self._expect(version, plan.preconditions["entry_version"], "entry version")
        request = plan.request
        operation = request["operation"]
        if operation == "replace_text":
            target = request["part_id"]
            insertion = {
                "operationType": "insert",
                "version": version,
                "coordinates": {"position": target},
                "parts": [plan.after["part"]],
            }
            first = await self.api.request(
                "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": insertion}
            )
            self._event(journal, "insert_replacement", "succeeded", {"response": first})
            self.repository.save_journal(journal)
            updated = await self.api.request("GET", f"/entry/{plan.entry_id}")
            next_version = entry_version(updated)
            deletion = {
                "operationType": "delete",
                "version": next_version,
                "coordinates": {"fromDocumentPartId": target, "toDocumentPartId": target},
            }
            second = await self.api.request(
                "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": deletion}
            )
            self._event(journal, "delete_original", "succeeded", {"response": second})
            discovered = await self._discover_new_part_ids(
                plan.entry_id, plan.preconditions.get("part_ids", [])
            )
            return {
                "operations": [first, second],
                "changed_part_ids": [target, *discovered["ids"]],
                "part_id_discovery": discovered,
            }

        if operation == "insert":
            mutation = {
                "operationType": "insert",
                "version": version,
                "coordinates": {"position": request.get("position", "end_of_document")},
                "parts": request["parts"],
            }
        elif operation == "delete":
            mutation = {
                "operationType": "delete",
                "version": version,
                "coordinates": {
                    "fromDocumentPartId": request["from_part_id"],
                    "toDocumentPartId": request.get("to_part_id", request["from_part_id"]),
                },
            }
        else:
            mutation = {
                "operationType": "move",
                "version": version,
                "sourceCoordinates": {
                    "fromDocumentPartId": request["from_part_id"],
                    "toDocumentPartId": request.get("to_part_id", request["from_part_id"]),
                },
                "destinationCoordinates": {"position": request["position"]},
            }
        response = await self.api.request(
            "PATCH", f"/entry/{plan.entry_id}", json={"partsMutation": mutation}
        )
        self._event(journal, operation, "succeeded", {"response": response})
        if operation == "insert":
            discovered = await self._discover_new_part_ids(
                plan.entry_id, plan.preconditions.get("part_ids", [])
            )
            changed_part_ids = discovered["ids"]
        else:
            discovered = {"complete": True, "ids": []}
            changed_part_ids = [
                part.get("id") for part in plan.before.get("parts", []) if part.get("id")
            ]
        return {
            "operation": operation,
            "response": response,
            "changed_part_ids": changed_part_ids,
            "part_id_discovery": discovered,
        }

    def _part_range(
        self, parts: list[dict[str, Any]], first: Any, last: Any
    ) -> list[dict[str, Any]]:
        ids = [part.get("id") for part in parts]
        try:
            start, end = ids.index(first), ids.index(last)
        except ValueError as exc:
            raise ValidationError("Document range contains an unknown part ID") from exc
        if end < start:
            raise ValidationError("Document range is reversed")
        return parts[start : end + 1]

    async def _discover_new_part_ids(
        self, entry_id: str, previous_ids: list[str]
    ) -> dict[str, Any]:
        try:
            current = [
                item.get("id")
                async for item in self.api.paginate(
                    f"/entry/{entry_id}/parts/items", params={"pageSize": 100}
                )
                if item.get("id")
            ]
            previous = set(previous_ids)
            return {"complete": True, "ids": [item for item in current if item not in previous]}
        except BenchlingAgentError as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:
            return {
                "complete": False,
                "ids": [],
                "error": {"code": "local_discovery_error", "type": type(exc).__name__},
            }
