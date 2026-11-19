"""Schema field planning and field value validation."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any

from ..errors import (
    UnsupportedError,
    ValidationError,
)
from ..models import (
    ChangeJournal,
    ChangePlan,
)
from ..services import (
    field_definition_type,
)
from .base import PlannerBase, PlanParts, write_event_details


class FieldNormalization(PlannerBase):
    """Validation and normalization of schema field values shared by fields and results planners."""

    def _normalize_schema_field_updates(
        self,
        updates: dict[str, Any],
        definitions: list[dict[str, Any]],
        *,
        require_all_required: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        by_system = {
            str(item["systemName"]): item for item in definitions if item.get("systemName")
        }
        by_id = {str(item["id"]): item for item in definitions if item.get("id")}
        if not by_system:
            raise UnsupportedError(
                "The assigned schema did not expose addressable field definitions"
            )
        normalized: dict[str, Any] = {}
        targets: list[dict[str, Any]] = []
        unknown: list[str] = []
        for supplied_key, field_input in updates.items():
            definition = by_system.get(supplied_key) or by_id.get(supplied_key)
            if definition is None:
                unknown.append(supplied_key)
                continue
            system_name = str(definition["systemName"])
            if system_name in normalized:
                raise ValidationError(
                    "The same schema field was supplied more than once",
                    details={"system_name": system_name},
                )
            if not isinstance(field_input, dict) or set(field_input) != {"value"}:
                raise ValidationError(
                    "Each schema field input must contain exactly one value property",
                    details={"field": supplied_key},
                )
            if definition.get("archived"):
                raise UnsupportedError(
                    "Archived schema fields cannot be updated",
                    details={"field": system_name},
                )
            if definition.get("derivationType") not in {None, "DIRECT"}:
                raise UnsupportedError(
                    "Computed or snapshot schema fields cannot be updated directly",
                    details={
                        "field": system_name,
                        "derivation_type": definition.get("derivationType"),
                    },
                )
            self._validate_schema_field_value(definition, field_input["value"])
            normalized[system_name] = deepcopy(field_input)
            targets.append(
                {
                    "field_definition_id": definition.get("id"),
                    "system_name": system_name,
                    "name": definition.get("name"),
                    "type": field_definition_type(definition),
                }
            )
        if unknown:
            raise ValidationError(
                "Schema fields must be addressed by system name or field-definition ID",
                details={
                    "unknown_fields": unknown,
                    "available_system_names": sorted(by_system),
                    "available_field_ids": sorted(by_id),
                },
            )
        if require_all_required:
            missing_required = sorted(
                str(item["systemName"])
                for item in definitions
                if item.get("systemName")
                and item.get("isRequired")
                and not item.get("archived")
                and item.get("derivationType") in {None, "DIRECT"}
                and str(item["systemName"]) not in normalized
            )
            if missing_required:
                raise ValidationError(
                    "Result records must provide every required schema field",
                    details={"missing_required_fields": missing_required},
                )
        return normalized, targets

    def _field_definition_fingerprints(
        self,
        definitions: list[dict[str, Any]],
        system_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        fingerprints = []
        for definition in definitions:
            system_name = definition.get("systemName")
            if not system_name or (system_names is not None and system_name not in system_names):
                continue
            fingerprints.append(
                {
                    "id": definition.get("id"),
                    "system_name": system_name,
                    "type": field_definition_type(definition),
                    "is_multi": bool(definition.get("isMulti")),
                    "is_required": bool(definition.get("isRequired")),
                    "archived": bool(definition.get("archived")),
                    "derivation_type": definition.get("derivationType"),
                    "numeric_min": definition.get("numericMin"),
                    "numeric_max": definition.get("numericMax"),
                }
            )
        return sorted(fingerprints, key=lambda item: str(item["system_name"]))

    def _validate_schema_field_value(self, definition: dict[str, Any], value: Any) -> None:
        field = str(definition.get("systemName") or definition.get("id"))
        if value is None:
            if definition.get("isRequired"):
                raise ValidationError(
                    "A required schema field cannot be cleared", details={"field": field}
                )
            return
        field_type = field_definition_type(definition)
        is_multi = bool(definition.get("isMulti"))
        values = value if is_multi else [value]
        if is_multi and not isinstance(value, list):
            raise ValidationError(
                "A multi-value schema field requires an array", details={"field": field}
            )
        if field_type in {"TEXT", "LONG_TEXT"}:
            valid = all(isinstance(item, str) for item in values)
        elif field_type == "INTEGER":
            valid = all(isinstance(item, int) and not isinstance(item, bool) for item in values)
        elif field_type == "FLOAT":
            valid = all(
                isinstance(item, (int, float)) and not isinstance(item, bool) for item in values
            )
        elif field_type == "BOOLEAN":
            valid = all(isinstance(item, bool) for item in values)
        elif field_type == "DATE":
            valid = all(self._is_date(item) for item in values)
        elif field_type == "DATETIME":
            valid = all(self._is_datetime(item) for item in values)
        elif field_type == "JSON":
            valid = True
        elif field_type in {"LINK", "DROPDOWN"}:
            valid = all(isinstance(item, str) and bool(item) for item in values)
        else:
            raise UnsupportedError(
                "This schema field type cannot be safely validated",
                details={"field": field, "type": field_type},
            )
        if not valid:
            raise ValidationError(
                "Schema field value has the wrong type",
                details={"field": field, "type": field_type, "is_multi": is_multi},
            )
        if field_type in {"INTEGER", "FLOAT"}:
            minimum = definition.get("numericMin")
            maximum = definition.get("numericMax")
            if minimum is not None and any(item < minimum for item in values):
                raise ValidationError(
                    "Schema field value is below its minimum",
                    details={"field": field, "minimum": minimum},
                )
            if maximum is not None and any(item > maximum for item in values):
                raise ValidationError(
                    "Schema field value is above its maximum",
                    details={"field": field, "maximum": maximum},
                )

    def _is_date(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def _is_datetime(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None


class FieldsPlanning(FieldNormalization):
    """Planning and applying of entry schema field updates."""

    async def _plan_fields(self, entry_id: str, request: dict[str, Any]) -> PlanParts:
        updates = request.get("schema_fields")
        if not isinstance(updates, dict) or not updates:
            raise ValidationError("Field plan requires non-empty schema_fields")
        current = await self.service.get_entry_fields(entry_id)
        if not current.get("schema"):
            raise UnsupportedError("Entry has no assigned schema")
        definitions = current.get("field_definitions") or []
        normalized, targets = self._normalize_schema_field_updates(updates, definitions)
        request["schema_fields"] = normalized
        fields = current.get("fields_by_system_name") or {}
        before_values = {key: fields.get(key) for key in normalized}
        after_values = {**before_values, **normalized}
        schema_id = (current.get("schema") or {}).get("id")
        return (
            {"fields": before_values, "targets": targets},
            {"fields": after_values, "targets": targets},
            {
                "schema_id": schema_id,
                "observed_values": before_values,
                "field_definitions": self._field_definition_fingerprints(
                    definitions, set(normalized)
                ),
                "modified_at": current.get("modified_at"),
            },
            ["immediate pre-write field-value comparison"],
            ["Entry field updates do not expose an atomic document-version precondition"],
        )

    async def _apply_fields(self, plan: ChangePlan, journal: ChangeJournal) -> dict[str, Any]:
        current = await self.service.get_entry_fields(plan.entry_id)
        current_schema_id = (current.get("schema") or {}).get("id")
        self._expect(
            current_schema_id,
            plan.preconditions.get("schema_id"),
            "entry schema",
        )
        self._expect(
            self._field_definition_fingerprints(
                current.get("field_definitions") or [],
                set(plan.preconditions["observed_values"]),
            ),
            plan.preconditions.get("field_definitions"),
            "entry field definitions",
        )
        observed = {
            key: (current.get("fields_by_system_name") or {}).get(key)
            for key in plan.preconditions["observed_values"]
        }
        self._expect(observed, plan.preconditions["observed_values"], "entry field values")
        response = await self.api.request(
            "PATCH",
            f"/entry/{plan.entry_id}",
            json={"schemaFields": plan.request["schema_fields"]},
        )
        self._event(journal, "update_fields", "succeeded", write_event_details(response))
        return {"response": response, "concurrency": "best_effort_value_comparison"}
