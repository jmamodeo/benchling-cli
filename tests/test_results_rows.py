"""Paged reads of V3 results-table rows against the fake API."""

from __future__ import annotations

import pytest
from fake_benchling import TENANT

from benchling_toolkit.errors import NotFoundError, UpstreamError, ValidationError

PART = "docpart_rt1"
ROWS_PATH = f"/document-part-results-table/{PART}/rows/items"
ROWS_URL = f"{TENANT}/api/v3{ROWS_PATH}"


def _row_calls(fake) -> list:
    return [call for call in fake.calls if call.path.endswith("/rows/items")]


async def test_results_table_rows_return_one_page_and_a_cursor(runtime, fake) -> None:
    fake.add_results_table()
    page = await runtime.service.list_results_table_rows(f"  {PART}  ", limit=2)
    assert page["id"] == PART
    assert page["version_id"] == "ver_1"
    assert page["name"] == "qPCR, plate 1"
    assert page["column_definitions"][0]["name"] == "Ct"
    assert page["result_schema_id"] == "assaysch_abc123"
    assert page["row_count"] == 3
    assert page["submission_status"] == "HAS_UNSUBMITTED_CHANGES"
    assert page["next_token"] == "2"
    assert page["items"][0]["cells"][0]["inputValue"] == {"type": "integer_value", "value": 12}
    assert page["items"][0]["result"] == {"__typename": "ResultRef", "id": "result_1"}
    assert page["items"][0]["submissionStatus"] == "SUBMITTED"
    assert page["items"][0]["rowId"] == "row_1"
    assert [row["id"] for row in page["items"]] == ["strow_1", "strow_2"]
    assert fake.writes() == []
    (call,) = _row_calls(fake)
    assert call.params == {"pageSize": "2"}
    assert call.method == "GET"
    assert call.path.endswith(f"/document-part-results-table/{PART}/rows/items")

    second = await runtime.service.list_results_table_rows(PART, limit=2, cursor=page["next_token"])
    assert [row["id"] for row in second["items"]] == ["strow_3"]
    assert second["items"][0]["cells"][0]["inputValue"] == {"type": "text_value", "value": "ok"}
    assert second["next_token"] is None
    assert _row_calls(fake)[1].params == {"pageSize": "2", "nextToken": "2"}


async def test_short_upstream_row_pages_are_followed_until_the_limit(runtime, fake) -> None:
    fake.add_results_table()
    fake.results_row_page_size = 1
    page = await runtime.service.list_results_table_rows(PART, limit=2)
    assert [row["id"] for row in page["items"]] == ["strow_1", "strow_2"]
    assert page["next_token"] == "2"
    calls = _row_calls(fake)
    assert [call.params for call in calls] == [
        {"pageSize": "2"},
        {"pageSize": "1", "nextToken": "1"},
    ]


async def test_the_default_results_row_page_is_fifty(runtime, fake) -> None:
    fake.add_results_table()
    page = await runtime.service.list_results_table_rows(PART)
    assert [row["id"] for row in page["items"]] == ["strow_1", "strow_2", "strow_3"]
    assert page["next_token"] is None
    assert _row_calls(fake)[0].params == {"pageSize": "50"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"part_id": "", "limit": 1},
        {"part_id": "   ", "limit": 1},
        {"part_id": 1, "limit": 1},
        {"part_id": PART, "limit": 0},
        {"part_id": PART, "limit": 101},
        {"part_id": PART, "limit": True},
        {"part_id": PART, "cursor": ""},
        {"part_id": PART, "cursor": 1},
    ],
)
async def test_results_rows_reject_bad_input_before_calling_the_api(runtime, fake, kwargs) -> None:
    part_id = kwargs.pop("part_id")
    with pytest.raises(ValidationError):
        await runtime.service.list_results_table_rows(part_id, **kwargs)
    assert fake.calls == []


async def test_missing_results_table_is_not_found(runtime) -> None:
    with pytest.raises(NotFoundError):
        await runtime.service.list_results_table_rows("docpart_missing")


async def test_results_rows_reject_a_bad_part_before_paging(runtime, fake) -> None:
    parent = {
        "id": PART,
        "rowCount": 1,
        "rows": ROWS_URL,
        "versionMetadata": {"versionId": "ver_1"},
        "columnDefinitions": [],
    }
    fake.payload_overrides.append(
        (f"/document-part-results-table/{PART}", 200, {**parent, "rowCount": True})
    )
    with pytest.raises(UpstreamError, match="row count"):
        await runtime.service.list_results_table_rows(PART)
    fake.payload_overrides.append(
        (
            f"/document-part-results-table/{PART}",
            200,
            {
                **parent,
                "rows": f"https://other.example/api/v3/document-part-results-table/{PART}/rows/items",
            },
        )
    )
    with pytest.raises(UpstreamError, match="does not belong"):
        await runtime.service.list_results_table_rows(PART)
    fake.payload_overrides.append(
        (f"/document-part-results-table/{PART}", 200, {**parent, "columnDefinitions": "Ct"})
    )
    with pytest.raises(UpstreamError, match="invalid columns"):
        await runtime.service.list_results_table_rows(PART)
    assert _row_calls(fake) == []


async def test_results_rows_reject_a_malformed_page_and_a_repeated_token(runtime, fake) -> None:
    fake.add_results_table()
    fake.payload_overrides.append((ROWS_PATH, 200, {"items": "nope"}))
    with pytest.raises(UpstreamError, match="unexpected shape"):
        await runtime.service.list_results_table_rows(PART, limit=1)

    fake.payload_overrides.append((ROWS_PATH, 200, {"items": ["x"], "nextToken": ""}))
    with pytest.raises(UpstreamError, match="non-object"):
        await runtime.service.list_results_table_rows(PART, limit=1)

    fake.payload_overrides.append((ROWS_PATH, 200, {"items": [], "nextToken": 1}))
    with pytest.raises(UpstreamError, match="not a string"):
        await runtime.service.list_results_table_rows(PART, limit=1)

    fake.payload_overrides.append(
        (
            ROWS_PATH,
            200,
            {
                "items": [
                    {"id": "strow_a", "cells": []},
                    {"id": "strow_b", "cells": []},
                ],
                "nextToken": "",
            },
        )
    )
    with pytest.raises(UpstreamError, match="more items than pageSize"):
        await runtime.service.list_results_table_rows(PART, limit=1)

    fake.repeat_next_token = True
    fake.results_row_page_size = 1
    with pytest.raises(UpstreamError, match="repeated pagination token"):
        await runtime.service.list_results_table_rows(PART, limit=5)


async def test_results_rows_reject_a_cell_that_is_not_a_value(runtime, fake) -> None:
    fake.add_results_table()
    fake.corrupt_cells = True
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.list_results_table_rows(PART)


@pytest.mark.parametrize(
    "row",
    [
        {"id": 5, "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}]},
        {"rowIndex": True, "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}]},
        {
            "isSubmittable": "yes",
            "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}],
        },
        {
            "submissionStatus": "UNKNOWN",
            "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}],
        },
        {
            "result": {"__typename": "ResultRef", "id": 5},
            "cells": [{"inputValue": {"__typename": "TextValue", "value": "ok"}}],
        },
        {"cells": []},
    ],
)
async def test_results_rows_reject_invalid_nested_row_metadata_and_cell_counts(
    runtime, fake, row
) -> None:
    fake.add_results_table()
    fake.payload_overrides.append((ROWS_PATH, 200, {"items": [row], "nextToken": ""}))
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.list_results_table_rows(PART)


async def test_full_results_table_read_rejects_a_mismatched_cell_count(runtime, fake) -> None:
    fake.add_results_table()
    fake.results_parts[PART]["rows"][0]["cells"].append(
        {"inputValue": {"__typename": "TextValue", "value": "extra"}}
    )
    with pytest.raises(UpstreamError, match="invalid rows"):
        await runtime.service.get_results_table_part(PART)
