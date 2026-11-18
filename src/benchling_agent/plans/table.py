"""Unstructured table edit planning."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import (
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from .base import PlannerBase, PlanParts


class TablePlanning(PlannerBase):
    """Planning and applying of unstructured table edits."""

    async def _plan_table(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        part_id = request.get("table_part_id")
        if not isinstance(part_id, str):
            raise ValidationError("Table plan requires table_part_id")
        target = await self.service.get_table_for_entry(entry_id, part_id)
        table = target["table"]
        content = deepcopy(table.get("content") or {})
        if "UnstructuredTable" not in str(content.get("__typename", "")):
            raise UnsupportedError("Only unstructured tables can be edited")
        before = {
            "table_part_id": part_id,
            "version_id": table.get("versionId"),
            "content": content,
        }
        after_content = deepcopy(content)
        operation = request.get("operation")
        if operation == "set_cells":
            updates = request.get("updates")
            if not isinstance(updates, list) or not updates:
                raise ValidationError("set_cells requires non-empty updates")
            rows = after_content.get("rows") or []
            seen_coordinates: set[tuple[int, int]] = set()
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or "value" not in update
                    or isinstance(update.get("row"), bool)
                    or not isinstance(update.get("row"), int)
                    or isinstance(update.get("column"), bool)
                    or not isinstance(update.get("column"), int)
                    or update["row"] < 0
                    or update["column"] < 0
                ):
                    raise ValidationError(
                        "Each cell update requires nonnegative integer row and column coordinates and a value",
                        details={"update": update},
                    )
                coordinate = (update["row"], update["column"])
                if coordinate in seen_coordinates:
                    raise ValidationError(
                        "A table plan cannot update the same cell twice",
                        details={"row": coordinate[0], "column": coordinate[1]},
                    )
                seen_coordinates.add(coordinate)
                try:
                    row = update["row"]
                    column = update["column"]
                    cell = rows[row]["cells"][column]
                except (KeyError, TypeError, IndexError) as exc:
                    raise ValidationError(
                        "Cell coordinate is outside the table",
                        details={"update": update},
                    ) from exc
                if cell.get("mergeRangeParent"):
                    raise UnsupportedError("Cannot write directly to a merged child cell")
                cell["inputValue"] = {
                    "type": "text_value",
                    "value": str(update.get("value", "")),
                }
                cell.pop("evaluatedValue", None)
        elif operation == "append_rows":
            rows = request.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValidationError("append_rows requires non-empty rows")
            expected = len(after_content.get("columnDefinitions") or [])
            normalized = [self._normalize_row(row, expected) for row in rows]
            after_content.setdefault("rows", []).extend(normalized)
        elif operation == "replace":
            replacement = request.get("table")
            if not isinstance(replacement, dict):
                raise ValidationError("replace requires a table object")
            after_content = deepcopy(content)
            after_content.update(deepcopy(replacement))
            if "rows" in replacement:
                self._preserve_table_cell_state(
                    content.get("rows") or [], after_content.get("rows") or []
                )
            expected = len(after_content.get("columnDefinitions") or [])
            after_content["rows"] = [
                self._normalize_row(row, expected) for row in after_content.get("rows", [])
            ]
        else:
            raise ValidationError(
                "Unknown table operation",
                details={"supported": ["set_cells", "append_rows", "replace"]},
            )
        expected = len(after_content.get("columnDefinitions") or [])
        after_content["rows"] = [
            self._normalize_row(row, expected) for row in after_content.get("rows", [])
        ]
        for row in after_content["rows"]:
            for cell in row["cells"]:
                cell.pop("evaluatedValue", None)
        after_content["isEvaluated"] = False
        self._validate_table_content(after_content)
        after = {
            "table_part_id": part_id,
            "version_id": "changes_on_apply",
            "content": after_content,
        }
        return (
            before,
            after,
            {"table_version": table.get("versionId"), "entry_id": entry_id},
            ["server-enforced document version check", "complete table replacement"],
            [
                "API table edits clear evaluated values until Benchling evaluates the table in the browser"
            ],
        )

    async def _apply_table(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        part_id = plan.after["table_part_id"]
        current = await self.service.get_table(part_id)
        self._expect(current.get("versionId"), plan.preconditions["table_version"], "table version")
        payload = self._table_write_payload(plan.after["content"])
        payload["versionId"] = plan.preconditions["table_version"]
        response = await self.api.request(
            "PATCH", f"/document-part-unstructured-table/{part_id}", json=payload
        )
        self._event(journal, "replace_table", "succeeded", {"response": response})
        return {"table_part_id": part_id, "response": response, "evaluated_values_cleared": True}

    def _normalize_row(self, row: Any, expected_columns: int) -> dict[str, Any]:
        if isinstance(row, list):
            cells = [{"inputValue": {"type": "text_value", "value": str(value)}} for value in row]
            row = {"cells": cells}
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
            raise ValidationError("Each table row must contain cells")
        if expected_columns and len(row["cells"]) != expected_columns:
            raise ValidationError(
                "Table row has the wrong number of cells",
                details={"expected": expected_columns, "actual": len(row["cells"])},
            )
        normalized = deepcopy(row)
        for index, cell in enumerate(normalized["cells"]):
            if not isinstance(cell, dict):
                normalized["cells"][index] = {
                    "inputValue": {"type": "text_value", "value": str(cell)}
                }
            elif "inputValue" not in cell:
                cell["inputValue"] = {"type": "text_value", "value": ""}
        return normalized

    def _preserve_table_cell_state(
        self, previous_rows: list[dict[str, Any]], replacement_rows: list[Any]
    ) -> None:
        preserved_keys = {
            "inputValue",
            "format",
            "mergeRange",
            "mergeRangeParent",
            "selectorId",
        }
        for row_index, row in enumerate(replacement_rows):
            if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
                continue
            if row_index >= len(previous_rows):
                continue
            previous_cells = previous_rows[row_index].get("cells") or []
            for column_index, cell in enumerate(row["cells"]):
                if not isinstance(cell, dict) or column_index >= len(previous_cells):
                    continue
                previous_cell = previous_cells[column_index]
                if not isinstance(previous_cell, dict):
                    continue
                for key in preserved_keys:
                    if key not in cell and key in previous_cell:
                        cell[key] = deepcopy(previous_cell[key])

    def _validate_table_content(self, content: dict[str, Any]) -> None:
        columns = content.get("columnDefinitions") or []
        rows = content.get("rows") or []
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValidationError("Table columns and rows must be arrays")
        if any(not isinstance(column, dict) for column in columns):
            raise ValidationError("Every table column definition must be an object")
        width = len(columns)
        for row_index, row in enumerate(rows):
            cells = row.get("cells") if isinstance(row, dict) else None
            if not isinstance(cells, list) or len(cells) != width:
                raise ValidationError(
                    "Every table row must match the number of columns",
                    details={
                        "row": row_index,
                        "expected_columns": width,
                        "actual_columns": len(cells) if isinstance(cells, list) else None,
                    },
                )
            for column_index, cell in enumerate(cells):
                if not isinstance(cell, dict):
                    raise ValidationError("Every table cell must be an object")
                input_value = cell.get("inputValue")
                if (
                    not isinstance(input_value, dict)
                    or input_value.get("type") != "text_value"
                    or not isinstance(input_value.get("value"), str)
                ):
                    raise ValidationError(
                        "Every unstructured-table cell requires a text inputValue",
                        details={"row": row_index, "column": column_index},
                    )
                merge = cell.get("mergeRange")
                if merge is not None:
                    if (
                        not isinstance(merge, dict)
                        or not isinstance(merge.get("mergeRowSpan"), int)
                        or not isinstance(merge.get("mergeColSpan"), int)
                        or merge["mergeRowSpan"] < 1
                        or merge["mergeColSpan"] < 1
                        or row_index + merge["mergeRowSpan"] > len(rows)
                        or column_index + merge["mergeColSpan"] > width
                    ):
                        raise ValidationError(
                            "A table merge range is invalid",
                            details={"row": row_index, "column": column_index},
                        )
                parent = cell.get("mergeRangeParent")
                if parent is not None:
                    if not isinstance(parent, dict):
                        raise ValidationError("A mergeRangeParent must be an object")
                    parent_row, parent_column = parent.get("row"), parent.get("col")
                    if (
                        not isinstance(parent_row, int)
                        or not isinstance(parent_column, int)
                        or parent_row < 0
                        or parent_column < 0
                        or parent_row >= len(rows)
                        or parent_column >= width
                    ):
                        raise ValidationError(
                            "A merged child points outside the table",
                            details={"row": row_index, "column": column_index},
                        )
                    anchor = rows[parent_row]["cells"][parent_column]
                    anchor_range = anchor.get("mergeRange") or {}
                    if not (
                        parent_row <= row_index < parent_row + anchor_range.get("mergeRowSpan", 0)
                        and parent_column
                        <= column_index
                        < parent_column + anchor_range.get("mergeColSpan", 0)
                    ):
                        raise ValidationError(
                            "A merged child does not point to a covering merge anchor",
                            details={"row": row_index, "column": column_index},
                        )
                selector_id = cell.get("selectorId")
                if selector_id is not None and not isinstance(selector_id, str):
                    raise ValidationError("selectorId must be a string or null")

    def _table_write_payload(self, content: dict[str, Any]) -> dict[str, Any]:
        self._validate_table_content(content)
        payload: dict[str, Any] = {}
        if "name" in content:
            payload["name"] = content["name"]
        if "caption" in content:
            caption = content["caption"]
            payload["caption"] = (
                self._select(caption, {"text", "formats", "links", "mentions"})
                if isinstance(caption, dict)
                else caption
            )
        payload["columnDefinitions"] = [
            self._select(column, {"name", "width", "isLocked"})
            for column in content.get("columnDefinitions", [])
        ]
        payload["rows"] = []
        for row in content.get("rows", []):
            output_row: dict[str, Any] = {"cells": []}
            for cell in row["cells"]:
                output_cell: dict[str, Any] = {
                    "inputValue": self._select(cell["inputValue"], {"type", "value"})
                }
                if "format" in cell:
                    value = cell["format"]
                    output_cell["format"] = (
                        self._select(
                            value,
                            {
                                "alignment",
                                "backgroundColor",
                                "foregroundColor",
                                "link",
                                "properties",
                            },
                        )
                        if isinstance(value, dict)
                        else value
                    )
                for key, allowed in (
                    ("mergeRange", {"mergeRowSpan", "mergeColSpan"}),
                    ("mergeRangeParent", {"row", "col"}),
                ):
                    if key in cell:
                        value = cell[key]
                        output_cell[key] = (
                            self._select(value, allowed) if isinstance(value, dict) else value
                        )
                if "selectorId" in cell:
                    output_cell["selectorId"] = cell["selectorId"]
                output_row["cells"].append(output_cell)
            payload["rows"].append(output_row)
        return payload

    def _select(self, value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        return {key: deepcopy(item) for key, item in value.items() if key in allowed}
