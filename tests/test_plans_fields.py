from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID

from benchling_toolkit.errors import ConflictError, UnsupportedError, ValidationError


async def plan(runtime, fields):
    return await runtime.planner.plan(ENTRY_ID, "fields", {"schema_fields": fields})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("temperature", 0),
        ("temperature", 100),
        ("notes", "free text"),
        ("notes", None),
        ("ratio", 0.5),
        ("ratio", 3),
        ("flag", False),
        ("run_date", "2026-01-31"),
        ("started_at", "2026-01-31T10:00:00Z"),
        ("started_at", "2026-01-31T10:00:00-05:00"),
        ("tags", ["a", "b"]),
        ("tags", []),
        ("meta", {"anything": [1, 2]}),
        ("sample_link", "etr_abc123"),
    ],
)
async def test_valid_field_values_are_planned(runtime, fake, key, value) -> None:
    planned = await plan(runtime, {key: {"value": value}})
    assert planned.request["schema_fields"] == {key: {"value": value}}
    assert planned.after["fields"][key] == {"value": value}
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("temperature", "5"),
        ("temperature", 5.5),
        ("temperature", True),
        ("temperature", -1),
        ("temperature", 101),
        ("temperature", None),
        ("notes", 5),
        ("ratio", 0.4),
        ("ratio", True),
        ("ratio", "1.0"),
        ("flag", "yes"),
        ("flag", 1),
        ("run_date", "2026-1-31"),
        ("run_date", "01/31/2026"),
        ("run_date", "2026-01-31T00:00:00Z"),
        ("run_date", 20260131),
        ("started_at", "2026-01-31T10:00:00"),
        ("started_at", "yesterday"),
        ("started_at", 5),
        ("tags", "a"),
        ("tags", [1]),
        ("sample_link", ""),
        ("sample_link", 7),
    ],
)
async def test_invalid_field_values_are_rejected(runtime, fake, key, value) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, {key: {"value": value}})
    assert caught.value.details["field"] == key


@pytest.mark.parametrize("key", ["weird", "calculated", "old"])
async def test_unsupported_or_read_only_fields_cannot_be_planned(runtime, key) -> None:
    with pytest.raises(UnsupportedError) as caught:
        await plan(runtime, {key: {"value": 1}})
    assert caught.value.details["field"] == key


async def test_field_definition_ids_normalize_to_system_names(runtime, fake) -> None:
    planned = await plan(runtime, {"tsf_notes": {"value": "x"}, "temperature": {"value": 30}})
    assert set(planned.request["schema_fields"]) == {"notes", "temperature"}
    assert {t["field_definition_id"] for t in planned.after["targets"]} == {
        "tsf_notes",
        "tsf_temp",
    }
    assert planned.before["fields"] == {"notes": "n", "temperature": 20}


async def test_unknown_fields_list_available_addresses(runtime) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, {"nonexistent": {"value": 1}, "Temperature": {"value": 1}})
    details = caught.value.details
    assert details["unknown_fields"] == ["nonexistent", "Temperature"]
    assert "temperature" in details["available_system_names"]
    assert "tsf_temp" in details["available_field_ids"]


@pytest.mark.parametrize(
    "fields",
    [
        {"temperature": {"value": 1, "extra": 2}},
        {"temperature": {}},
        {"temperature": 5},
        {"temperature": {"value": 1}, "tsf_temp": {"value": 2}},
    ],
)
async def test_malformed_or_duplicate_field_inputs_are_rejected(runtime, fields) -> None:
    with pytest.raises(ValidationError):
        await plan(runtime, fields)


@pytest.mark.parametrize("fields", [{}, []])
async def test_empty_field_plan_is_rejected(runtime, fields) -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        await runtime.planner.plan(ENTRY_ID, "fields", {"schema_fields": fields})


async def test_entry_without_schema_is_unsupported(runtime, fake) -> None:
    fake.entry_schema_id = None
    with pytest.raises(UnsupportedError, match="no assigned schema"):
        await plan(runtime, {"temperature": {"value": 1}})


async def test_schema_without_addressable_fields_is_unsupported(runtime, fake) -> None:
    fake.field_defs["entsch_abc123"] = []
    with pytest.raises(UnsupportedError, match="addressable"):
        await plan(runtime, {"temperature": {"value": 1}})


async def test_apply_sends_normalized_schema_fields_and_updates_entry(runtime, fake) -> None:
    planned = await plan(runtime, {"tsf_temp": {"value": 42}})
    applied = await runtime.planner.apply(planned.id)
    assert fake.entry_patches == [{"schemaFields": {"temperature": {"value": 42}}}]
    assert fake.entry_fields["Temperature"] == 42
    assert applied["journal"]["result"]["concurrency"] == "best_effort_value_comparison"
    assert applied["journal"]["status"] == "succeeded"


async def test_apply_detects_changed_field_definitions(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    next(d for d in fake.field_defs["entsch_abc123"] if d["id"] == "tsf_temp")["numericMax"] = 50
    with pytest.raises(ConflictError, match="field definitions"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_schema_change(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    fake.field_defs["entsch_other"] = fake.field_defs["entsch_abc123"]
    fake.entry_schema_id = "entsch_other"
    with pytest.raises(ConflictError, match="schema"):
        await runtime.planner.apply(planned.id)
    assert fake.writes() == []


async def test_apply_detects_value_drift_and_journals_failure(runtime, fake) -> None:
    planned = await plan(runtime, {"temperature": {"value": 42}})
    fake.entry_fields["Temperature"] = 19
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details["actual"] == {"temperature": 19}
    assert fake.writes() == []
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "failed"
