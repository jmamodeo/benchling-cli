from __future__ import annotations

import json
import os
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from mcp import Client
from typer.testing import CliRunner

from benchling_agent.auth import AuthManager
from benchling_agent.cli import app
from benchling_agent.environment import (
    EnvironmentConfigStore,
    available_envs,
    environment_secrets,
    variables,
)
from benchling_agent.errors import AuthenticationError, ValidationError
from benchling_agent.mcp_server import build_mcp_server
from benchling_agent.models import AuthMethod
from benchling_agent.runtime import Runtime, RuntimePool
from benchling_agent.tls import verified_context

VARIABLES = variables("dev")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("BENCHLING_") and name != "BENCHLING_AGENT_HOME":
            monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, env="dev", host="tenant", secret="sensitive-test-secret"):
    for key, value in zip(
        variables(env),
        (
            f"https://{host}.benchling.com/",
            f"{env}-client" if env != "dev" else "test-client",
            secret,
        ),
    ):
        monkeypatch.setenv(key, value)


def test_tls_verification_remains_enabled():
    context = verified_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_environment_layout_and_no_legacy_fallback(tmp_path, monkeypatch):
    (tmp_path / "profiles.json").write_text(
        '{"version": 1, "profiles": {"dev": {"name": "dev", "tenant_url": "https://old.benchling.com"}}}'
    )
    env = EnvironmentConfigStore(tmp_path)
    with pytest.raises(ValidationError):
        env.resolve("dev")
    configure(monkeypatch)
    with pytest.raises(ValidationError) as missing:
        env.resolve(None)
    assert missing.value.details["available_envs"] == ["dev"]
    environment = env.resolve("dev")
    assert environment.base_url == "https://tenant.benchling.com"
    assert environment.auth_method == AuthMethod.CLIENT_CREDENTIALS
    assert "sensitive-test-secret" not in json.dumps(env.metadata())
    with pytest.raises(ValidationError):
        env.resolve("sandbox")


def test_multiple_environments_discovered_with_separate_secrets(tmp_path, monkeypatch):
    configure(monkeypatch)
    configure(monkeypatch, env="test", host="other", secret="other-secret")
    monkeypatch.setenv("BENCHLING_PROD_API_ENDPOINT", "https://prod.benchling.com/")
    assert available_envs() == ["dev", "test"]
    store = EnvironmentConfigStore(tmp_path)
    assert store.resolve("test").base_url == "https://other.benchling.com"
    with pytest.raises(AuthenticationError) as partial:
        store.resolve("prod")
    assert partial.value.details["missing_variables"] == [
        "BENCHLING_PROD_CLIENT_ID",
        "BENCHLING_PROD_CLIENT_SECRET",
    ]
    assert environment_secrets() == {"dev": "sensitive-test-secret", "test": "other-secret"}


@pytest.mark.parametrize("name", ["DEV", "../dev", ""])
def test_invalid_environment_names_rejected(tmp_path, monkeypatch, name):
    configure(monkeypatch)
    with pytest.raises(ValidationError):
        EnvironmentConfigStore(tmp_path).resolve(name)


def test_partial_environment_reports_names_not_values(tmp_path, monkeypatch):
    monkeypatch.setenv(VARIABLES[2], "sensitive-test-secret")
    with pytest.raises(AuthenticationError) as caught:
        EnvironmentConfigStore(tmp_path).resolve("dev")
    assert caught.value.details["missing_variables"] == list(VARIABLES[:2])
    assert "sensitive-test-secret" not in str(caught.value.details)


@pytest.mark.parametrize(
    "url",
    [
        "http://tenant.benchling.com",
        "https://user:secret@tenant.benchling.com",
        "https://tenant.benchling.com/path",
    ],
)
def test_invalid_endpoint_redacted(tmp_path, monkeypatch, url):
    configure(monkeypatch)
    monkeypatch.setenv(VARIABLES[0], url)
    with pytest.raises(ValidationError) as caught:
        EnvironmentConfigStore(tmp_path).resolve("dev")
    assert url not in str(caught.value)


def test_app_tokens_cached_renewed_and_not_persisted(tmp_path, monkeypatch):
    configure(monkeypatch)
    config = EnvironmentConfigStore(tmp_path / "unused")
    environment = config.resolve("dev")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert str(request.url) == "https://tenant.benchling.com/oauth/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"],
            "client_id": ["test-client"],
            "client_secret": ["sensitive-test-secret"],
        }
        return httpx.Response(
            200, json={"access_token": f"token-{count}", "token_type": "bearer", "expires_in": 900}
        )

    auth = AuthManager(
        environment_secrets(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert (
            list(pool.map(lambda _: auth.headers(environment), range(2)))
            == [{"Authorization": "Bearer token-1"}] * 2
        )
    assert count == 1
    bundle = auth._app_tokens[(environment.name, environment.base_url, environment.client_id)]
    bundle.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert auth.headers(environment) == {"Authorization": "Bearer token-2"}
    assert not config.root.exists()


def test_token_error_cannot_echo_secret(tmp_path, monkeypatch):
    configure(monkeypatch)
    config = EnvironmentConfigStore(tmp_path)
    auth = AuthManager(
        environment_secrets(),
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, json={"error": "sensitive-test-secret"})
            )
        ),
    )
    with pytest.raises(AuthenticationError) as caught:
        auth.headers(config.resolve("dev"))
    assert caught.value.details == {"status": 401}


def test_cli_status_is_environment_only(tmp_path, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "unused"))
    result = CliRunner().invoke(app, ["--env", "dev", "auth", "status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["identity"] == "app"
    assert data["credential_source"] == "environment"
    assert data["configured"] is True
    assert "sensitive-test-secret" not in result.stdout
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_mcp_uses_environment_app_identity(tmp_path, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "unused"))
    auth_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"access_token": "access", "expires_in": 900})
        )
    )

    def read_handler(request):
        assert request.headers["Authorization"] == "Bearer access"
        key = "items" if request.url.path.startswith("/api/v3/") else "entries"
        return httpx.Response(200, json={key: [], "nextToken": ""})

    runtime = Runtime(
        "dev",
        auth_http=auth_http,
        async_http=httpx.AsyncClient(transport=httpx.MockTransport(read_handler)),
    )
    pool = RuntimePool(runtimes={"dev": runtime})
    try:
        async with Client(build_mcp_server(pool)) as client:
            result = await client.call_tool("get_capabilities", {"env": "dev"})
            assert result.structured_content["ok"] is True
            assert result.structured_content["data"]["auth_method"] == "client_credentials"
            assert "sensitive-test-secret" not in json.dumps(result.structured_content)
    finally:
        await pool.close()
        auth_http.close()


@pytest.mark.asyncio
async def test_mcp_routes_each_call_to_its_env(tmp_path, monkeypatch):
    configure(monkeypatch)
    configure(monkeypatch, env="test", host="other", secret="other-secret")
    monkeypatch.setenv("BENCHLING_AGENT_HOME", str(tmp_path / "unused"))
    hosts: list[str] = []

    def token_handler(request):
        return httpx.Response(200, json={"access_token": request.url.host, "expires_in": 900})

    def read_handler(request):
        assert request.headers["Authorization"] == f"Bearer {request.url.host}"
        hosts.append(request.url.host)
        key = "items" if request.url.path.startswith("/api/v3/") else "entries"
        return httpx.Response(200, json={key: [], "nextToken": ""})

    auth_http = httpx.Client(transport=httpx.MockTransport(token_handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(read_handler))
    pool = RuntimePool(
        runtimes={
            env: Runtime(env, auth_http=auth_http, async_http=async_http) for env in ("dev", "test")
        }
    )
    try:
        async with Client(build_mcp_server(pool, writable_envs={"dev"})) as client:
            listed = await client.call_tool("list_environments", {})
            assert listed.structured_content["data"]["environments"] == {
                "dev": {"tenant_url": "https://tenant.benchling.com/", "writes_allowed": True},
                "test": {"tenant_url": "https://other.benchling.com/", "writes_allowed": False},
            }
            for env, host in (("test", "other.benchling.com"), ("dev", "tenant.benchling.com")):
                hosts.clear()
                result = await client.call_tool("get_capabilities", {"env": env})
                assert result.structured_content["meta"]["env"] == env
                assert hosts and set(hosts) == {host}
            blocked = await client.call_tool("apply_changes", {"env": "test", "plan_id": "a" * 32})
            assert blocked.structured_content["error"]["code"] == "unsupported_capability"
            unknown = await client.call_tool("get_capabilities", {"env": "prod"})
            assert unknown.structured_content["error"]["details"]["available_envs"] == [
                "dev",
                "test",
            ]
    finally:
        await pool.close()
        auth_http.close()
