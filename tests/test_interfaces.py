from __future__ import annotations

import json
import os

import httpx
import pytest
from mcp import Client
from typer.testing import CliRunner

from benchling_agent.cli import app
from benchling_agent.config import ConfigStore
from benchling_agent.credentials import MemoryCredentialStore
from benchling_agent.mcp_server import build_mcp_server
from benchling_agent.models import AuthMethod, Profile
from benchling_agent.runtime import Runtime, RuntimePool


def test_envs_cli_returns_json_envelope(tmp_path, monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCHLING_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "app"))
    result = CliRunner().invoke(app, ["envs", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "schema_version": "1.0",
        "ok": True,
        "data": {"environments": {}},
        "error": None,
        "meta": {},
    }


def test_browser_login_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "app"))
    result = CliRunner().invoke(app, ["login"])
    assert result.exit_code == 6
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "BENCHLING_<ENV>_CLIENT_SECRET" in payload["error"]["message"]
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_mcp_tools_use_structured_envelopes_and_gate_writes(tmp_path) -> None:
    config = ConfigStore(tmp_path / "app")
    profile = Profile(
        name="sandbox",
        tenant_url="https://tenant.benchling.com",
        auth_method=AuthMethod.API_KEY,
    )
    config.upsert(profile)
    credentials = MemoryCredentialStore()
    credentials.set("sandbox", "api_key", "sk_test")

    def handler(request: httpx.Request) -> httpx.Response:
        key = "items" if request.url.path.startswith("/api/v3/") else "entries"
        return httpx.Response(200, json={key: [], "nextToken": ""}, request=request)

    runtime = Runtime(
        "sandbox",
        config=config,
        credentials=credentials,
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    server = build_mcp_server(
        RuntimePool(config=config, credentials=credentials, runtimes={"sandbox": runtime})
    )
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {"get_entry", "plan_changes", "apply_changes", "get_change_status"} <= names
        plan_tool = next(tool for tool in listed.tools if tool.name == "plan_changes")
        assert plan_tool.annotations.read_only_hint is False
        capabilities_tool = next(
            tool for tool in listed.tools if tool.name == "get_capabilities"
        )
        assert capabilities_tool.output_schema["properties"]["schema_version"][
            "const"
        ] == "1.0"
        assert "ok" in capabilities_tool.output_schema["required"]

        capabilities = await client.call_tool("get_capabilities", {"env": "sandbox"})
        assert capabilities.is_error is False
        assert capabilities.structured_content["schema_version"] == "1.0"
        assert capabilities.structured_content["ok"] is True
        assert set(capabilities.structured_content) == {
            "schema_version",
            "ok",
            "data",
            "error",
            "meta",
        }

        blocked = await client.call_tool("apply_changes", {"env": "sandbox", "plan_id": "a" * 32})
        assert blocked.structured_content["ok"] is False
        assert blocked.structured_content["error"]["code"] == "unsupported_capability"
    await runtime.close()
