from __future__ import annotations

import pytest
from fake_benchling import ENTRY_ID, TABLE_PART

from benchling_agent.errors import (
    ConflictError,
    NotFoundError,
    PartialCompletionError,
    UnsupportedError,
    ValidationError,
)


async def plan(runtime, request, part_id=TABLE_PART):
    return await runtime.planner.plan(
        ENTRY_ID, "table", {"table_part_id": part_id, **request}
    )


def cell(row, column, value):
    return {"row": row, "column": column, "value": value}


@pytest.mark.parametrize(
    ("request_body", "expected"),
    [
        ({"operation": "set_cells"}, "non-empty updates"),
        ({"operation": "set_cells", "updates": []}, "non-empty updates"),
        ({"operation": "set_cells", "updates": [{"row": 0, "column": 0}]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(-1, 0, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(0, -1, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(True, 0, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(0, 1.5, "x")]}, "coordinates"),
        ({"operation": "set_cells", "updates": ["A1"]}, "coordinates"),
        ({"operation": "set_cells", "updates": [cell(3, 0, "x")]}, "outside the table"),
        ({"operation": "set_cells", "updates": [cell(0, 2, "x")]}, "outside the table"),
        ({"operation": "set_cells", "updates": [cell(0, 0, "a"), cell(0, 0, "b")]}, "same cell"),
        ({"operation": "append_rows"}, "non-empty rows"),
        ({"operation": "append_rows", "rows": []}, "non-empty rows"),
        ({"operation": "append_rows", "rows": [["only one"]]}, "wrong number of cells"),
        ({"operation": "append_rows", "rows": [["a", "b", "c"]]}, "wrong number of cells"),
        ({"operation": "append_rows", "rows": ["flat"]}, "must contain cells"),
        ({"operation": "append_rows", "rows": [{"cells": "ab"}]}, "must contain cells"),
        ({"operation": "replace"}, "table object"),
        ({"operation": "replace", "table": []}, "table object"),
        ({"operation": "replace", "table": {"columnDefinitions": ["Sample"], "rows": []}},
         "column definition"),
        ({"operation": "replace", "table": {"columnDefinitions": "Sample", "rows": []}},
         "arrays"),
        ({"operation": "replace", "table": {"columnDefinitions": ["Sample"]}},
         "wrong number of cells"),
        ({"operation": "replace", "table": {"rows": [["only one"]]}}, "wrong number of cells"),
        ({"operation": "replace", "table": {"rows": "none"}}, "must contain cells"),
        ({"operation": "sort"}, "Unknown table operation"),
        ({}, "Unknown table operation"),
    ],
)
async def test_invalid_table_specs_are_rejected_without_writing(
    runtime, fake, request_body, expected
) -> None:
    with pytest.raises(ValidationError) as caught:
        await plan(runtime, request_body)
    assert expected in caught.value.message
    assert fake.writes() == []


async def test_table_part_id_is_required(runtime) -> None:
    with pytest.raises(ValidationError, match="table_part_id"):
        await runtime.planner.plan(ENTRY_ID, "table", {"operation": "append_rows"})


async def test_structured_tables_cannot_be_edited(runtime) -> None:
    with pytest.raises(UnsupportedError, match="unstructured"):
        await plan(runtime, {"operation": "append_rows", "rows": [["a", "b"]]},
                   part_id="docpart_struct1")


async def test_table_part_of_another_entry_is_not_found(runtime) -> None:
    with pytest.raises(NotFoundError):
        await plan(runtime, {"operation": "append_rows", "rows": [["a", "b"]]},
                   part_id="docpart_elsewhere")


async def test_text_part_is_not_an_editable_table(runtime) -> None:
    with pytest.raises(UnsupportedError):
        await plan(runtime, {"operation": "append_rows", "rows": [["a", "b"]]},
                   part_id="docpart_text1")


async def test_writing_into_a_merged_child_cell_is_unsupported(runtime) -> None:
    with pytest.raises(UnsupportedError, match="merged child"):
        await plan(runtime, {"operation": "set_cells", "updates": [cell(1, 1, "x")]})


async def test_set_cells_preview_coerces_values_and_keeps_cell_state(runtime, fake) -> None:
    planned = await plan(
        runtime, {"operation": "set_cells", "updates": [cell(0, 1, 42), cell(2, 0, "D")]}
    )
    rows = planned.after["content"]["rows"]
    assert rows[0]["cells"][1]["inputValue"] == {"type": "text_value", "value": "42"}
    assert rows[0]["cells"][1]["format"] == {"backgroundColor": "#fff000"}
    assert rows[0]["cells"][1]["selectorId"] == "selector_1"
    assert rows[1]["cells"][0]["mergeRange"] == {"mergeRowSpan": 1, "mergeColSpan": 2}
    assert rows[2]["cells"][0]["inputValue"]["value"] == "D"
    assert planned.after["content"]["isEvaluated"] is False
    assert all("evaluatedValue" not in c for row in rows for c in row["cells"])
    assert planned.before["content"]["isEvaluated"] is True
    assert planned.before["content"]["rows"][0]["cells"][1]["evaluatedValue"]
    assert planned.preconditions == {"table_version": "tver_1", "entry_id": ENTRY_ID}
    assert planned.warnings and "evaluated" in planned.warnings[0]
    assert fake.writes() == []


async def test_append_rows_accepts_lists_and_cell_objects(runtime) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "append_rows",
            "rows": [
                ["D", 4],
                {"cells": [{"inputValue": {"type": "text_value", "value": "E"}}, {}, ]},
            ],
        },
    )
    rows = planned.after["content"]["rows"]
    assert len(rows) == 5
    assert rows[3]["cells"][1]["inputValue"]["value"] == "4"
    assert rows[4]["cells"][1] == {"inputValue": {"type": "text_value", "value": ""}}
    assert len(planned.before["content"]["rows"]) == 3


async def test_append_row_with_non_dict_cell_is_stringified(runtime) -> None:
    planned = await plan(
        runtime, {"operation": "append_rows", "rows": [{"cells": ["F", 6]}]}
    )
    assert planned.after["content"]["rows"][3]["cells"][1]["inputValue"]["value"] == "6"


async def test_replace_preserves_existing_cell_state_for_matching_positions(runtime) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "replace",
            "table": {
                "rows": [
                    {"cells": [{}, {"inputValue": {"type": "text_value", "value": "9"}}]},
                    {"cells": [{}, {}]},
                    ["Z", "26"],
                    ["new", "row"],
                ]
            },
        },
    )
    rows = planned.after["content"]["rows"]
    assert rows[0]["cells"][0]["inputValue"]["value"] == "A"
    assert rows[0]["cells"][1]["inputValue"]["value"] == "9"
    assert rows[0]["cells"][1]["format"] == {"backgroundColor": "#fff000"}
    assert rows[0]["cells"][1]["selectorId"] == "selector_1"
    assert rows[1]["cells"][0]["mergeRange"] == {"mergeRowSpan": 1, "mergeColSpan": 2}
    assert rows[1]["cells"][1]["mergeRangeParent"] == {"row": 1, "col": 0}
    assert rows[2]["cells"][0]["inputValue"]["value"] == "Z"
    assert rows[3]["cells"][0] == {"inputValue": {"type": "text_value", "value": "new"}}
    assert planned.after["content"]["name"] == "Measurements"


async def test_replace_without_rows_only_updates_metadata(runtime) -> None:
    planned = await plan(
        runtime, {"operation": "replace", "table": {"name": "Renamed", "caption": {"text": "c"}}}
    )
    assert planned.after["content"]["name"] == "Renamed"
    assert len(planned.after["content"]["rows"]) == 3


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"rows": [{"cells": [{"mergeRange": {"mergeRowSpan": 1, "mergeColSpan": 1}}, {}]}]
          }, None),
        ({"rows": [{"cells": [{"mergeRange": {"mergeRowSpan": 0, "mergeColSpan": 1}}, {}]}]},
         "merge range is invalid"),
        ({"rows": [{"cells": [{"mergeRange": {"mergeRowSpan": 1, "mergeColSpan": 3}}, {}]}]},
         "merge range is invalid"),
        ({"rows": [{"cells": [{"mergeRange": {"mergeRowSpan": 5, "mergeColSpan": 1}}, {}]}]},
         "merge range is invalid"),
        ({"rows": [{"cells": [{"mergeRange": "wide"}, {}]}]}, "merge range is invalid"),
        ({"rows": [{"cells": [{"mergeRangeParent": "x"}, {}]}]}, "must be an object"),
        ({"rows": [{"cells": [{"mergeRangeParent": {"row": 9, "col": 0}}, {}]}]},
         "outside the table"),
        ({"rows": [{"cells": [{"mergeRangeParent": {"row": 0, "col": 1}}, {}]}]},
         "covering merge anchor"),
        ({"rows": [{"cells": [{"selectorId": 5}, {}]}]}, "selectorId"),
        ({"rows": [{"cells": [{"inputValue": {"type": "number_value", "value": "1"}}, {}]}]},
         "text inputValue"),
        ({"rows": [{"cells": [{"inputValue": {"type": "text_value", "value": 1}}, {}]}]},
         "text inputValue"),
    ],
)
async def test_replace_validates_cell_metadata(runtime, table, message) -> None:
    request = {"operation": "replace", "table": table}
    if message is None:
        assert (await plan(runtime, request)).after["content"]["rows"]
        return
    with pytest.raises(ValidationError, match=message):
        await plan(runtime, request)


async def test_apply_sends_versioned_payload_without_evaluated_values(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "set_cells", "updates": [cell(0, 1, "3")]})
    applied = await runtime.planner.apply(planned.id)
    assert applied["journal"]["result"]["evaluated_values_cleared"] is True
    (payload,) = fake.table_payloads
    assert payload["versionId"] == "tver_1"
    assert payload["name"] == "Measurements"
    assert payload["columnDefinitions"] == [{"name": "Sample"}, {"name": "Value"}]
    assert set(payload) == {"name", "columnDefinitions", "rows", "versionId"}
    first = payload["rows"][0]["cells"][1]
    assert first == {
        "inputValue": {"type": "text_value", "value": "3"},
        "format": {"backgroundColor": "#fff000"},
        "selectorId": "selector_1",
    }
    assert payload["rows"][1]["cells"][0]["mergeRange"] == {"mergeRowSpan": 1, "mergeColSpan": 2}
    assert payload["rows"][1]["cells"][1]["mergeRangeParent"] == {"row": 1, "col": 0}
    assert fake.table_version == 2


async def test_payload_drops_unknown_cell_and_format_keys_but_keeps_caption(
    runtime, fake
) -> None:
    planned = await plan(
        runtime,
        {
            "operation": "replace",
            "table": {
                "caption": {"text": "cap", "formats": [], "extra": 1},
                "columnDefinitions": [{"name": "Only", "width": 90, "isLocked": True, "x": 1}],
                "rows": [
                    {
                        "cells": [
                            {
                                "inputValue": {"type": "text_value", "value": "v", "x": 1},
                                "format": {"alignment": "center", "bogus": 1},
                                "unknown": True,
                            }
                        ]
                    }
                ],
            },
        },
    )
    await runtime.planner.apply(planned.id)
    (payload,) = fake.table_payloads
    assert payload["caption"] == {"text": "cap", "formats": []}
    assert payload["columnDefinitions"] == [{"name": "Only", "width": 90, "isLocked": True}]
    assert payload["rows"] == [
        {
            "cells": [
                {
                    "inputValue": {"type": "text_value", "value": "v"},
                    "format": {"alignment": "center"},
                }
            ]
        }
    ]


async def test_append_rows_apply_adds_rows_to_remote_table(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    await runtime.planner.apply(planned.id)
    assert len(fake.table_content["rows"]) == 4
    assert fake.table_content["rows"][3]["cells"][0]["inputValue"]["value"] == "D"


async def test_apply_refuses_stale_table_without_writing(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.table_version += 1
    with pytest.raises(ConflictError) as caught:
        await runtime.planner.apply(planned.id)
    assert caught.value.details == {"expected": "tver_1", "actual": "tver_2"}
    assert fake.writes() == []


async def test_server_side_conflict_marks_plan_failed(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.fail("PATCH", "document-part-unstructured-table", 409)
    with pytest.raises(ConflictError):
        await runtime.planner.apply(planned.id)
    status = await runtime.planner.status(planned.id)
    assert status["journal"]["status"] == "failed"
    assert [event["status"] for event in status["journal"]["events"]] == ["started", "failed"]


async def test_unconfirmed_table_write_is_unknown(runtime, fake) -> None:
    planned = await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})
    fake.fail("PATCH", "document-part-unstructured-table", 502)
    with pytest.raises(PartialCompletionError):
        await runtime.planner.apply(planned.id)
    assert (await runtime.planner.status(planned.id))["journal"]["status"] == "unknown"


async def test_table_part_version_mismatch_during_planning_is_a_conflict(runtime, fake) -> None:
    fake.parts[3]["versionId"] = "tver_other"
    with pytest.raises(ConflictError, match="while its target entry"):
        await plan(runtime, {"operation": "append_rows", "rows": [["D", "4"]]})

