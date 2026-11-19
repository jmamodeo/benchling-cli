from __future__ import annotations

import json
import os

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from typer.testing import CliRunner

from benchling_toolkit import cli


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda environment: runtime_for(fake, environment))

    def run(*args: str, input: str | None = None, env: str | None = "sandbox"):
        prefix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*prefix, *args], input=input)
        return result.exit_code, json.loads(result.stdout), result

    return run


def test_envelope_shape_and_env_metadata_on_success(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", ENTRY_ID)
    assert code == 0
    assert payload["ok"] is True and payload["error"] is None
    assert payload["meta"] == {
        "env": "sandbox",
        "tenant_url": "https://your-tenant-dev.benchling.com",
    }
    assert payload["data"]["entry_id"] == ENTRY_ID


def test_environment_is_required_for_api_commands(invoke, fake) -> None:
    code, payload, _ = invoke("doctor", env=None)
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["available_envs"] == ["sandbox"]
    assert fake.calls == []


def test_unknown_environment_is_rejected_before_any_request(invoke, fake) -> None:
    code, payload, _ = invoke("doctor", env="prod")
    assert code == 2
    assert payload["error"]["details"]["env"] == "prod"
    assert fake.calls == []


def test_doctor_reports_capabilities(invoke) -> None:
    code, payload, _ = invoke("doctor")
    assert code == 0
    assert payload["data"]["ok"] is True
    assert [c["name"] for c in payload["data"]["checks"]] == ["v3_entries", "v2_entries"]


def test_entry_get_full_and_summary(invoke) -> None:
    code, summary, _ = invoke("entry", "get", "EXP-1")
    assert code == 0 and summary["data"]["version_id"] == "ver_1"
    assert "parts" not in summary["data"]
    code, full, _ = invoke("entry", "get", ENTRY_ID, "--full")
    assert code == 0
    assert len(full["data"]["parts"]) == 6
    assert full["data"]["completeness"] == "partial"


def test_version_flag_matches_package_metadata(invoke) -> None:
    from importlib.metadata import version

    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"benchling-toolkit {version('benchling-toolkit')}"


@pytest.mark.parametrize(
    ("args", "key", "expected"),
    [
        (["entry", "inspect", ENTRY_ID], "version_id", "ver_1"),
        (["entry", "fields", "get", ENTRY_ID], "entry_id", ENTRY_ID),
        (["section", "get", ENTRY_ID, "Results"], "version_id", "ver_1"),
        (["part", "get", ENTRY_ID, TEXT_PART], "entry_id", ENTRY_ID),
        (["table", "get", TABLE_PART], "versionId", "tver_1"),
        (
            ["schema", "get", "entsch_abc123"],
            "schema",
            {"id": "entsch_abc123", "name": "Schema entsch_abc123"},
        ),
        (["results", "get", "result_1"], "id", "result_1"),
        (["results", "tables", ENTRY_ID], "complete", True),
    ],
)
def test_read_commands_succeed(invoke, args, key, expected) -> None:
    code, payload, _ = invoke(*args)
    assert code == 0, payload
    assert payload["data"][key] == expected


def test_results_list_forwards_filters_and_cursor(invoke, fake) -> None:
    fake.results_page_size = 2
    code, payload, _ = invoke(
        "results",
        "list",
        "--entry",
        ENTRY_ID,
        "--schema",
        "assaysch_abc123",
        "--page-size",
        "2",
        "--cursor",
        "1",
    )
    assert code == 0
    assert [r["id"] for r in payload["data"]["items"]] == ["result_2", "result_3"]
    (call,) = [c for c in fake.calls if c.path.endswith("/result/items")]
    assert call.params == {
        "pageSize": "2",
        "nextToken": "1",
        "entry.eq": ENTRY_ID,
        "schema.eq": "assaysch_abc123",
    }


@pytest.mark.parametrize("size", ["0", "101"])
def test_results_list_page_size_bounds_are_enforced_by_the_cli(invoke, fake, size) -> None:
    result = CliRunner().invoke(
        cli.app, ["--env", "sandbox", "results", "list", "--page-size", size]
    )
    assert result.exit_code == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (401, 3, "authentication_error"),
        (403, 4, "access_denied"),
        (404, 4, "not_found"),
        (400, 2, "validation_error"),
        (409, 5, "conflict"),
        (0, 7, "upstream_error"),
    ],
)
def test_error_codes_map_to_exit_codes(invoke, fake, status, code, error) -> None:
    fake.fail("GET", f"/entry/{ENTRY_ID}", status)
    exit_code, payload, _ = invoke("entry", "resolve", ENTRY_ID)
    assert exit_code == code
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == error


def test_missing_entry_by_display_id_exits_not_found(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", "EXP-404")
    assert code == 4
    assert payload["error"]["details"] == {"reference": "EXP-404"}


def test_url_from_another_tenant_is_rejected(invoke, fake) -> None:
    code, payload, _ = invoke(
        "entry", "resolve", f"https://someone-else.benchling.com/s/x/{ENTRY_ID}"
    )
    assert code == 2
    assert "different tenant" in payload["error"]["message"]
    assert fake.calls == []


def test_same_tenant_url_resolves(invoke) -> None:
    code, payload, _ = invoke(
        "entry", "resolve", f"https://your-tenant-dev.benchling.com/s/lib/{ENTRY_ID}-title/edit"
    )
    assert code == 0
    assert payload["data"]["entry_id"] == ENTRY_ID


def write_spec(tmp_path, spec) -> str:
    path = tmp_path / "spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    return str(path)


DELETE_SPEC = {"operation": "delete", "from_part_id": TEXT_PART}


def test_plan_status_apply_lifecycle_through_cli(invoke, fake, tmp_path) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    assert code == 0
    plan_id = planned["data"]["id"]
    assert fake.writes() == []
    _, status, _ = invoke("changes", "status", plan_id)
    assert status["data"]["journal"]["status"] == "pending"
    code, applied, _ = invoke("changes", "apply", plan_id)
    assert code == 0 and applied["data"]["journal"]["status"] == "succeeded"
    assert TEXT_PART not in fake.part_ids()
    code, again, _ = invoke("changes", "apply", plan_id)
    assert code == 5 and again["error"]["code"] == "conflict"


def test_plan_reads_spec_from_stdin(invoke) -> None:
    code, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        "-",
        input=json.dumps(DELETE_SPEC),
    )
    assert code == 0
    assert planned["data"]["request"] == DELETE_SPEC


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"text"'])
def test_plan_rejects_bad_spec_content(invoke, fake, tmp_path, content) -> None:
    code, payload, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, content),
    )
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert fake.calls == []


def test_plan_rejects_missing_spec_file(invoke, tmp_path) -> None:
    code, payload, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        str(tmp_path / "missing.json"),
    )
    assert code == 2
    assert payload["error"]["details"]["path"].endswith("missing.json")


def test_plan_family_is_validated_case_insensitively(invoke, tmp_path) -> None:
    code, _, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "DOCUMENT",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    assert code == 0
    result = CliRunner().invoke(
        cli.app,
        [
            "--env",
            "sandbox",
            "changes",
            "plan",
            "--entry",
            ENTRY_ID,
            "--family",
            "magic",
            "--spec",
            "-",
        ],
    )
    assert result.exit_code == 2


def test_apply_with_unconfirmed_write_exits_with_partial_code(invoke, fake, tmp_path) -> None:
    _, planned, _ = invoke(
        "changes",
        "plan",
        "--entry",
        ENTRY_ID,
        "--family",
        "document",
        "--spec",
        write_spec(tmp_path, DELETE_SPEC),
    )
    fake.fail("PATCH", f"/entry/{ENTRY_ID}", 500)
    code, payload, _ = invoke("changes", "apply", planned["data"]["id"])
    assert code == 8
    assert payload["error"]["code"] == "partial_or_unknown_completion"
    assert payload["error"]["details"]["journal"]["status"] == "unknown"


def test_apply_rejects_malformed_plan_id(invoke) -> None:
    code, payload, _ = invoke("changes", "apply", "../etc/passwd")
    assert code == 2
    assert payload["error"]["code"] == "validation_error"


def test_envs_list_shows_configured_environment_without_secrets(invoke) -> None:
    code, payload, result = invoke("envs", "list", env=None)
    assert code == 0
    assert payload["data"]["environments"]["sandbox"]["tenant_url"].startswith(
        "https://your-tenant-dev.benchling.com"
    )
    assert "sandbox-client-secret" not in result.stdout


def test_envs_list_is_an_empty_envelope_when_nothing_is_configured(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCHLING_") and name != "BENCHLING_TOOLKIT_HOME":
            monkeypatch.delenv(name)
    result = CliRunner().invoke(cli.app, ["envs", "list"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema_version": "1.0",
        "ok": True,
        "data": {"environments": {}},
        "error": None,
        "meta": {},
    }


def test_mcp_serve_reports_startup_errors_as_json_on_stderr(invoke, monkeypatch) -> None:
    from benchling_toolkit.keychain import Keychain

    unknown = CliRunner().invoke(cli.app, ["mcp", "serve", "--allow-writes", "prod"])
    assert unknown.exit_code == 2
    assert json.loads(unknown.stderr)["error"]["details"]["unknown_envs"] == ["prod"]

    Keychain().delete("sandbox")
    missing = CliRunner().invoke(cli.app, ["mcp", "serve"])
    assert missing.exit_code == 3
    assert missing.stdout == ""
    assert json.loads(missing.stderr)["error"]["code"] == "authentication_error"
    assert "sandbox-client-secret" not in missing.stderr


def test_auth_status_reports_without_calling_the_api(invoke, fake) -> None:
    code, payload, result = invoke("auth", "status")
    assert code == 0
    assert payload["meta"]["env"] == "sandbox"
    row = payload["data"]["environments"]["sandbox"]
    assert row["secret_present"] is True
    assert row["credential_source"] == "keyring"
    assert "sandbox-client-secret" not in result.stdout
    assert fake.calls == []


def test_invalid_saved_endpoint_fails_validation_without_echoing_value(invoke) -> None:
    from benchling_toolkit.environment import EnvironmentConfigStore

    path = EnvironmentConfigStore().environments_path
    saved = json.loads(path.read_text())
    saved["environments"]["sandbox"]["tenant_url"] = "http://insecure.example/"
    path.write_text(json.dumps(saved))
    code, payload, result = invoke("auth", "status")
    assert code == 2
    assert "insecure.example" not in result.stdout


def test_cli_without_arguments_shows_help() -> None:
    result = CliRunner().invoke(cli.app, [])
    assert "Usage" in result.stdout and "changes" in result.stdout
