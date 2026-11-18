from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from mcp import Client

from benchling_agent.errors import AuthenticationError, ValidationError
from benchling_agent.mcp_server import build_mcp_server, run_mcp_server
from benchling_agent.runtime import RuntimePool

READ_TOOLS = {
    "list_environments",
    "get_capabilities",
    "resolve_entry",
    "get_entry",
    "inspect_entry",
    "get_section",
    "get_part",
    "get_table",
    "get_schema",
    "get_entry_fields",
    "list_results",
    "get_result",
    "list_result_tables",
    "get_change_status",
}


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert result.is_error is False, result
    payload = result.structured_content
    assert set(payload) == {"schema_version", "ok", "data", "error", "meta"}
    return payload


class Session:
    """Opens a short-lived in-process MCP connection per request.

    Entering the client inside an async fixture breaks anyio cancel scopes, and the
    server keeps no per-connection state, so connecting per call is equivalent.
    """

    def __init__(self, server) -> None:
        self.server = server

    async def call_tool(self, name, arguments):
        async with Client(self.server) as active:
            return await active.call_tool(name, arguments)

    async def list_tools(self):
        async with Client(self.server) as active:
            return await active.list_tools()


@pytest.fixture
def client(pool):
    return Session(build_mcp_server(pool))


@pytest.fixture
def writer(pool):
    return Session(build_mcp_server(pool, writable_envs={"sandbox"}))


async def test_tools_are_annotated_by_whether_they_can_write(client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert READ_TOOLS | {"plan_changes", "apply_changes"} == set(tools)
    for name in READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    assert tools["apply_changes"].annotations.destructive_hint is True
    assert tools["plan_changes"].annotations.read_only_hint is False
    assert tools["plan_changes"].annotations.destructive_hint is False
    schema = tools["get_capabilities"].output_schema
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert "ok" in schema["required"]
    for name in set(tools) - {"list_environments"}:
        assert "env" in tools[name].input_schema["required"], name


async def test_missing_env_argument_is_a_protocol_error(client) -> None:
    result = await client.call_tool("get_entry", {"reference": ENTRY_ID})
    assert result.is_error is True


async def test_unknown_env_returns_structured_error_with_choices(client, fake) -> None:
    payload = await call(client, "get_entry", env="prod", reference=ENTRY_ID)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["available_envs"] == ["sandbox"]
    assert payload["meta"] == {}
    assert fake.calls == []


async def test_list_environments_reports_write_permission(pool, benchling_env, monkeypatch) -> None:
    monkeypatch.setenv("BENCHLING_OTHER_API_ENDPOINT", "https://other-tenant.benchling.com")
    monkeypatch.setenv("BENCHLING_OTHER_CLIENT_ID", "client")
    monkeypatch.setenv("BENCHLING_OTHER_CLIENT_SECRET", "secret")
    active = Session(build_mcp_server(pool, writable_envs={"other"}))
    data = (await call(active, "list_environments"))["data"]["environments"]
    assert {name: item["writes_allowed"] for name, item in data.items()} == {
        "other": True,
        "sandbox": False,
    }
    assert "secret" not in json.dumps(data)


async def test_capabilities_reports_each_api_check(client, fake) -> None:
    payload = await call(client, "get_capabilities", env="sandbox")
    assert payload["ok"] is True
    assert payload["meta"]["env"] == "sandbox"
    assert payload["data"]["capabilities"]["entry_fields"] is True
    fake.fail("GET", "/entry/items", 403)
    degraded = (await call(client, "get_capabilities", env="sandbox"))["data"]
    assert degraded["ok"] is False
    assert degraded["capabilities"]["document_parts"] is False
    assert degraded["checks"][0]["code"] == "access_denied"


async def test_resolve_entry_by_id_and_display_id(client) -> None:
    by_id = (await call(client, "resolve_entry", env="sandbox", reference=ENTRY_ID))["data"]
    by_display = (await call(client, "resolve_entry", env="sandbox", reference="EXP-1"))["data"]
    assert by_id["resolved_by"] == "canonical_id"
    assert by_display["resolved_by"] == "display_id"
    assert by_id["entry_id"] == by_display["entry_id"] == ENTRY_ID
    missing = await call(client, "resolve_entry", env="sandbox", reference="EXP-99")
    assert missing["error"]["code"] == "not_found"


async def test_get_entry_without_full_returns_metadata_only(client, fake) -> None:
    data = (await call(client, "get_entry", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["version_id"] == "ver_1"
    assert "parts" not in data
    assert not any("parts/items" in c.path for c in fake.calls)


async def test_full_entry_pages_parts_results_and_table_rows(client, fake) -> None:
    fake.parts_page_size = 2
    fake.results_page_size = 2
    first = (
        await call(
            client,
            "get_entry",
            env="sandbox",
            reference=ENTRY_ID,
            full=True,
            part_limit=4,
            result_limit=2,
            table_row_limit=1,
        )
    )["data"]
    assert [p["id"] for p in first["parts"]][-1] == TABLE_PART
    assert first["pagination"]["parts"] == {
        "offset": 0,
        "returned": 4,
        "total": 6,
        "next_offset": 4,
    }
    assert first["pagination"]["results"] == {
        "offset": 0,
        "returned": 2,
        "total": 3,
        "next_offset": 2,
    }
    assert first["pagination"]["table_rows"] == {"offset": 0, "limit_per_table": 1}
    assert len(first["tables"][TABLE_PART]["content"]["rows"]) == 1
    assert first["tables"][TABLE_PART]["pagination"] == {
        "offset": 0,
        "returned": 1,
        "total": 3,
        "next_offset": 1,
    }
    second = (
        await call(
            client,
            "get_entry",
            env="sandbox",
            reference=ENTRY_ID,
            full=True,
            part_offset=4,
            part_limit=4,
            result_offset=2,
            table_row_offset=1,
        )
    )["data"]
    assert [p["id"] for p in second["parts"]] == ["docpart_struct1", "docpart_text2"]
    assert second["pagination"]["parts"]["next_offset"] is None
    assert [r["id"] for r in second["results"]] == ["result_3"]
    assert second["tables"] == {}
    assert second["completeness"] == "partial"
    assert {i["reason"] for i in second["issues"]} == {
        "structured_table_detail_not_available_through_document_api"
    }


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_entry", {"reference": ENTRY_ID, "full": True, "part_offset": -1}),
        ("get_entry", {"reference": ENTRY_ID, "full": True, "result_limit": 0}),
        ("get_entry", {"reference": ENTRY_ID, "full": True, "table_row_limit": 0}),
        ("get_section", {"reference": ENTRY_ID, "section": "Results", "part_limit": 0}),
        ("get_section", {"reference": ENTRY_ID, "section": "Results", "table_row_offset": -1}),
        ("get_part", {"reference": ENTRY_ID, "part_id": TEXT_PART, "row_offset": -1}),
        ("get_part", {"reference": ENTRY_ID, "part_id": TEXT_PART, "row_limit": 0}),
        ("get_table", {"part_id": TABLE_PART, "row_offset": -1}),
        ("get_table", {"part_id": TABLE_PART, "row_limit": 0}),
        ("get_schema", {"schema_id": "entsch_abc123", "field_offset": -1}),
        ("get_schema", {"schema_id": "entsch_abc123", "field_limit": 0}),
        ("list_result_tables", {"reference": ENTRY_ID, "offset": -1}),
        ("list_result_tables", {"reference": ENTRY_ID, "limit": 101}),
        ("list_result_tables", {"reference": ENTRY_ID, "limit": 0}),
        ("list_results", {"page_size": 0}),
        ("list_results", {"page_size": 101}),
    ],
)
async def test_invalid_pagination_arguments_return_validation_envelopes(
    client, fake, tool, arguments
) -> None:
    payload = await call(client, tool, env="sandbox", **arguments)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "validation_error"
    assert payload["meta"]["env"] == "sandbox"
    assert fake.writes() == []


async def test_inspect_entry_summarizes_structure(client) -> None:
    data = (await call(client, "inspect_entry", env="sandbox", reference=ENTRY_ID))["data"]
    assert [s["title"] for s in data["sections"]] == ["Methods", "Results"]
    assert data["tables"][TABLE_PART]["row_count"] == 3
    assert data["tables"][TABLE_PART]["name"] == "Measurements"
    assert data["project_id"] == "src_abc123"
    assert data["result_tables"][0]["table_id"] == "strtbl_example"
    assert data["parts"][1] == {
        "id": TEXT_PART,
        "type": "TextDocumentContent",
        "summary": data["parts"][1]["summary"],
    }


async def test_get_section_by_title_or_part_id_with_paging(client, fake) -> None:
    by_title = (
        await call(
            client,
            "get_section",
            env="sandbox",
            reference=ENTRY_ID,
            section="Results",
            part_limit=2,
        )
    )["data"]
    assert [p["id"] for p in by_title["parts"]] == ["docpart_sec2", TABLE_PART]
    assert by_title["pagination"]["parts"] == {
        "offset": 0,
        "returned": 2,
        "total": 4,
        "next_offset": 2,
    }
    assert TABLE_PART in by_title["tables"]
    by_id = (
        await call(
            client,
            "get_section",
            env="sandbox",
            reference=ENTRY_ID,
            section="docpart_sec2",
            part_offset=2,
        )
    )["data"]
    assert [p["id"] for p in by_id["parts"]] == ["docpart_struct1", "docpart_text2"]
    assert by_id["tables"] == {}
    missing = await call(client, "get_section", env="sandbox", reference=ENTRY_ID, section="Nope")
    assert missing["error"]["code"] == "not_found"


async def test_duplicate_section_titles_require_a_part_id(client, fake) -> None:
    fake.parts[0]["title"] = "Results"
    payload = await call(
        client, "get_section", env="sandbox", reference=ENTRY_ID, section="Results"
    )
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["part_ids"] == ["docpart_sec1", "docpart_sec2"]


async def test_get_part_pages_table_detail_but_not_text(client) -> None:
    table = (
        await call(
            client,
            "get_part",
            env="sandbox",
            reference=ENTRY_ID,
            part_id=TABLE_PART,
            row_offset=1,
            row_limit=1,
        )
    )["data"]
    assert table["detail"]["pagination"] == {
        "offset": 1,
        "returned": 1,
        "total": 3,
        "next_offset": 2,
    }
    assert table["detail"]["content"]["rows"][0]["cells"][0]["inputValue"]["value"] == "B"
    text = (await call(client, "get_part", env="sandbox", reference=ENTRY_ID, part_id=TEXT_PART))[
        "data"
    ]
    assert text["detail"] is None
    assert text["part"]["content"]["text"] == "before"
    missing = await call(client, "get_part", env="sandbox", reference=ENTRY_ID, part_id="docpart_x")
    assert missing["error"]["code"] == "not_found"


async def test_get_table_pages_rows_across_upstream_pages(client, fake) -> None:
    fake.table_page_size = 2
    page = (
        await call(
            client, "get_table", env="sandbox", part_id=TABLE_PART, row_offset=2, row_limit=5
        )
    )["data"]
    assert page["pagination"] == {"offset": 2, "returned": 1, "total": 3, "next_offset": None}
    assert page["versionId"] == "tver_1"
    missing = await call(client, "get_table", env="sandbox", part_id="docpart_unknown")
    assert missing["error"]["code"] == "not_found"


async def test_get_schema_pages_field_definitions(client) -> None:
    first = (
        await call(client, "get_schema", env="sandbox", schema_id="entsch_abc123", field_limit=5)
    )["data"]
    assert [f["systemName"] for f in first["field_definitions"]] == [
        "temperature",
        "notes",
        "ratio",
        "flag",
        "run_date",
    ]
    assert first["pagination"] == {"offset": 0, "returned": 5, "total": 12, "next_offset": 5}
    last = (
        await call(
            client,
            "get_schema",
            env="sandbox",
            schema_id="entsch_abc123",
            field_offset=10,
            field_limit=5,
        )
    )["data"]
    assert last["pagination"]["next_offset"] is None and last["pagination"]["returned"] == 2
    result_schema = (await call(client, "get_schema", env="sandbox", schema_id="assaysch_abc123"))[
        "data"
    ]
    assert result_schema["schema"]["id"] == "assaysch_abc123"
    missing = await call(client, "get_schema", env="sandbox", schema_id="entsch_none")
    assert missing["error"]["code"] == "not_found"


async def test_get_entry_fields_maps_values_by_system_name(client) -> None:
    data = (await call(client, "get_entry_fields", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["fields_by_system_name"]["temperature"] == 20
    assert data["fields_by_system_name"]["flag"] is None
    assert data["project_id"] == "src_abc123"
    assert data["schema"] == {"id": "entsch_abc123"}


async def test_list_results_uses_explicit_cursor_pagination(client, fake) -> None:
    fake.results_page_size = 2
    first = (await call(client, "list_results", env="sandbox", entry="EXP-1", page_size=2))["data"]
    assert [r["id"] for r in first["items"]] == ["result_1", "result_2"]
    assert first["next_token"] == "2"
    second = (
        await call(
            client, "list_results", env="sandbox", entry=ENTRY_ID, cursor=first["next_token"]
        )
    )["data"]
    assert [r["id"] for r in second["items"]] == ["result_3"]
    assert second["next_token"] is None
    sent = [c.params for c in fake.calls if c.path.endswith("/result/items")]
    assert sent[0] == {"pageSize": "2", "entry.eq": ENTRY_ID}
    assert sent[1]["nextToken"] == "2"
    filtered = (await call(client, "list_results", env="sandbox", schema_id="assaysch_none"))[
        "data"
    ]
    assert filtered["items"] == []


async def test_get_result_returns_record_or_not_found(client) -> None:
    assert (await call(client, "get_result", env="sandbox", result_id="result_2"))["data"][
        "id"
    ] == "result_2"
    missing = await call(client, "get_result", env="sandbox", result_id="result_zzz")
    assert missing["error"]["code"] == "not_found"


async def test_list_result_tables_pages_targets(client, fake) -> None:
    data = (await call(client, "list_result_tables", env="sandbox", reference=ENTRY_ID))["data"]
    assert data["items"][0]["table_id"] == "strtbl_example"
    assert data["items"][0]["schema_id"] == "assaysch_abc123"
    assert data["pagination"] == {"offset": 0, "returned": 1, "total": 1, "next_offset": None}
    assert data["id_namespace"] == "v2_structured_table"
    past_end = (
        await call(client, "list_result_tables", env="sandbox", reference=ENTRY_ID, offset=1)
    )["data"]
    assert past_end["items"] == [] and past_end["pagination"]["returned"] == 0


async def test_upstream_errors_map_to_envelope_codes(client, fake) -> None:
    for status, code in (
        (401, "authentication_error"),
        (403, "access_denied"),
        (404, "not_found"),
        (400, "validation_error"),
    ):
        fake.fail("GET", f"/entry/{ENTRY_ID}", status)
        payload = await call(client, "resolve_entry", env="sandbox", reference=ENTRY_ID)
        assert payload["error"]["code"] == code, status
        assert payload["error"]["details"]["status"] == status


async def test_unexpected_local_failures_do_not_leak_internals(client, fake) -> None:
    fake.fail("GET", f"/entry/{ENTRY_ID}", 0)
    payload = await call(client, "resolve_entry", env="sandbox", reference=ENTRY_ID)
    assert payload["error"]["code"] == "upstream_error"
    assert payload["error"]["details"] == {"type": "RuntimeError"}
    assert "simulated" not in json.dumps(payload)


async def test_plan_and_status_work_but_apply_is_blocked_by_default(client, fake) -> None:
    planned = await call(
        client,
        "plan_changes",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        specification={"operation": "delete", "from_part_id": TEXT_PART},
    )
    assert planned["ok"] is True
    plan_id = planned["data"]["id"]
    blocked = await call(client, "apply_changes", env="sandbox", plan_id=plan_id)
    assert blocked["error"]["code"] == "unsupported_capability"
    assert "--allow-writes sandbox" in blocked["error"]["message"]
    assert fake.writes() == []
    status = (await call(client, "get_change_status", env="sandbox", plan_id=plan_id))["data"]
    assert status["journal"]["status"] == "pending"
    assert status["plan"]["family"] == "document"


async def test_plan_validation_errors_are_enveloped(client, fake) -> None:
    payload = await call(
        client,
        "plan_changes",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        specification={"operation": "explode"},
    )
    assert payload["error"]["code"] == "validation_error"
    bad_family = await client.call_tool(
        "plan_changes",
        {"env": "sandbox", "entry": ENTRY_ID, "family": "magic", "specification": {}},
    )
    assert bad_family.is_error is True


async def test_apply_changes_when_writes_are_enabled(writer, fake) -> None:
    planned = await call(
        writer,
        "plan_changes",
        env="sandbox",
        entry=ENTRY_ID,
        family="table",
        specification={
            "operation": "append_rows",
            "table_part_id": TABLE_PART,
            "rows": [["D", "4"]],
        },
    )
    plan_id = planned["data"]["id"]
    applied = await call(writer, "apply_changes", env="sandbox", plan_id=plan_id)
    assert applied["ok"] is True
    assert applied["data"]["journal"]["status"] == "succeeded"
    assert len(fake.table_content["rows"]) == 4
    again = await call(writer, "apply_changes", env="sandbox", plan_id=plan_id)
    assert again["error"]["code"] == "conflict"
    assert len(fake.writes()) == 1


async def test_apply_conflict_and_partial_completion_are_structured(writer, fake) -> None:
    stale = await call(
        writer,
        "plan_changes",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        specification={"operation": "delete", "from_part_id": TEXT_PART},
    )
    fake.entry_version += 1
    conflict = await call(writer, "apply_changes", env="sandbox", plan_id=stale["data"]["id"])
    assert conflict["error"]["code"] == "conflict"
    assert conflict["error"]["details"] == {"expected": "ver_1", "actual": "ver_2"}
    unknown = await call(
        writer,
        "plan_changes",
        env="sandbox",
        entry=ENTRY_ID,
        family="document",
        specification={"operation": "delete", "from_part_id": TEXT_PART},
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 500)
    outcome = await call(writer, "apply_changes", env="sandbox", plan_id=unknown["data"]["id"])
    assert outcome["error"]["code"] == "partial_or_unknown_completion"
    assert outcome["error"]["details"]["journal"]["status"] == "unknown"


async def test_apply_is_guarded_per_environment(pool, fake, monkeypatch) -> None:
    monkeypatch.setenv("BENCHLING_OTHER_API_ENDPOINT", "https://other-tenant.benchling.com")
    monkeypatch.setenv("BENCHLING_OTHER_CLIENT_ID", "client")
    monkeypatch.setenv("BENCHLING_OTHER_CLIENT_SECRET", "secret")
    other = runtime_for(fake, "other")
    both = RuntimePool(runtimes={"sandbox": pool.get("sandbox"), "other": other})
    try:
        active = Session(build_mcp_server(both, writable_envs={"sandbox"}))
        planned = await call(
            active,
            "plan_changes",
            env="other",
            entry=ENTRY_ID,
            family="document",
            specification={"operation": "delete", "from_part_id": TEXT_PART},
        )
        blocked = await call(active, "apply_changes", env="other", plan_id=planned["data"]["id"])
        assert blocked["error"]["code"] == "unsupported_capability"
        wrong_env = await call(
            active, "apply_changes", env="sandbox", plan_id=planned["data"]["id"]
        )
        assert wrong_env["error"]["code"] == "validation_error"
        assert fake.writes() == []
    finally:
        await other.close()


def test_serve_rejects_unknown_writable_env(benchling_env) -> None:
    with pytest.raises(ValidationError) as caught:
        run_mcp_server(writable_envs={"sandbox", "prod"})
    assert caught.value.details == {
        "unknown_envs": ["prod"],
        "available_envs": ["sandbox"],
    }


def test_serve_requires_at_least_one_environment(monkeypatch) -> None:
    for name in ("API_ENDPOINT", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"BENCHLING_SANDBOX_{name}", raising=False)
    with pytest.raises(AuthenticationError, match="No environments configured"):
        run_mcp_server()


def test_serve_runs_stdio_transport_with_validated_writable_envs(
    benchling_env, monkeypatch
) -> None:
    from mcp.server.mcpserver import MCPServer

    seen: list[str] = []
    monkeypatch.setattr(MCPServer, "run", lambda self, transport: seen.append(transport))
    run_mcp_server(writable_envs={"sandbox"})
    assert seen == ["stdio"]
