from __future__ import annotations

import json

import pytest
from conftest import runtime_for
from fake_benchling import ENTRY_ID, TABLE_PART, TEXT_PART
from typer.testing import CliRunner

from benchling_agent import cli


@pytest.fixture
def invoke(benchling_env, fake, monkeypatch):
    monkeypatch.setattr(cli, "Runtime", lambda profile: runtime_for(fake, profile))

    def run(*args: str, input: str | None = None, env: str | None = "sandbox"):
        prefix = ["--env", env] if env else []
        result = CliRunner().invoke(cli.app, [*prefix, *args], input=input)
        return result.exit_code, json.loads(result.stdout), result

    return run


def test_envelope_shape_and_profile_metadata_on_success(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", ENTRY_ID)
    assert code == 0
    assert payload["ok"] is True and payload["error"] is None
    assert payload["meta"] == {
        "profile": "sandbox",
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


def test_entry_get_v2_returns_raw_diagnostic_payload(invoke, fake) -> None:
    code, payload, _ = invoke("entry", "get", ENTRY_ID, "--api-version", "2")
    assert code == 0
    assert payload["data"]["resolved"]["entry_id"] == ENTRY_ID
    assert payload["data"]["v2_response"]["entry"]["folderId"] == "lib_abc123"


def test_entry_get_rejects_unsupported_api_version(invoke, fake) -> None:
    result = CliRunner().invoke(cli.app, ["--env", "sandbox", "entry", "get", ENTRY_ID,
                                          "--api-version", "4"])
    assert result.exit_code == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    ("args", "key", "expected"),
    [
        (["entry", "inspect", ENTRY_ID], "version_id", "ver_1"),
        (["entry", "fields", "get", ENTRY_ID], "entry_id", ENTRY_ID),
        (["section", "get", ENTRY_ID, "Results"], "version_id", "ver_1"),
        (["part", "get", ENTRY_ID, TEXT_PART], "entry_id", ENTRY_ID),
        (["table", "get", TABLE_PART], "versionId", "tver_1"),
        (["schema", "get", "entsch_abc123"], "schema", {"id": "entsch_abc123",
                                                       "name": "Schema entsch_abc123"}),
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
    code, payload, _ = invoke("results", "list", "--entry", ENTRY_ID, "--schema",
                              "assaysch_abc123", "--page-size", "2", "--cursor", "1")
    assert code == 0
    assert [r["id"] for r in payload["data"]["items"]] == ["result_2", "result_3"]
    (call,) = [c for c in fake.calls if c.path.endswith("/result/items")]
    assert call.params == {
        "pageSize": "2", "nextToken": "1", "entry.eq": ENTRY_ID, "schema.eq": "assaysch_abc123",
    }


@pytest.mark.parametrize("size", ["0", "101"])
def test_results_list_page_size_bounds_are_enforced_by_the_cli(invoke, fake, size) -> None:
    result = CliRunner().invoke(cli.app, ["--env", "sandbox", "results", "list",
                                          "--page-size", size])
    assert result.exit_code == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    ("args", "status", "code", "error"),
    [
        (["entry", "resolve", ENTRY_ID], 401, 3, "authentication_error"),
        (["entry", "resolve", ENTRY_ID], 403, 4, "access_denied"),
        (["entry", "resolve", ENTRY_ID], 404, 4, "not_found"),
        (["entry", "resolve", ENTRY_ID], 400, 2, "validation_error"),
        (["entry", "resolve", ENTRY_ID], 409, 5, "conflict"),
        (["entry", "resolve", ENTRY_ID], 0, 7, "upstream_error"),
    ],
)
def test_error_codes_map_to_exit_codes(invoke, fake, args, status, code, error) -> None:
    fake.fail("GET", f"/entry/{ENTRY_ID}", status)
    exit_code, payload, _ = invoke(*args)
    assert exit_code == code
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == error


def test_missing_entry_by_display_id_exits_not_found(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve", "EXP-404")
    assert code == 4
    assert payload["error"]["details"] == {"reference": "EXP-404"}


def test_url_from_another_tenant_is_rejected(invoke, fake) -> None:
    code, payload, _ = invoke("entry", "resolve",
                              f"https://someone-else.benchling.com/s/x/{ENTRY_ID}")
    assert code == 2
    assert "different tenant" in payload["error"]["message"]
    assert fake.calls == []


def test_same_tenant_url_resolves(invoke) -> None:
    code, payload, _ = invoke("entry", "resolve",
                              f"https://your-tenant-dev.benchling.com/s/lib/{ENTRY_ID}-title/edit")
    assert code == 0
    assert payload["data"]["entry_id"] == ENTRY_ID


def write_spec(tmp_path, spec) -> str:
    path = tmp_path / "spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    return str(path)


DELETE_SPEC = {"operation": "delete", "from_part_id": TEXT_PART}


def test_plan_status_apply_lifecycle_through_cli(invoke, fake, tmp_path) -> None:
    code, planned, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "document",
                              "--spec", write_spec(tmp_path, DELETE_SPEC))
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
    code, planned, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "document",
                              "--spec", "-", input=json.dumps(DELETE_SPEC))
    assert code == 0
    assert planned["data"]["request"] == DELETE_SPEC


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"text"'])
def test_plan_rejects_bad_spec_content(invoke, fake, tmp_path, content) -> None:
    code, payload, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "document",
                              "--spec", write_spec(tmp_path, content))
    assert code == 2
    assert payload["error"]["code"] == "validation_error"
    assert fake.calls == []


def test_plan_rejects_missing_spec_file(invoke, tmp_path) -> None:
    code, payload, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "document",
                              "--spec", str(tmp_path / "missing.json"))
    assert code == 2
    assert payload["error"]["details"]["path"].endswith("missing.json")


def test_plan_family_is_validated_case_insensitively(invoke, tmp_path) -> None:
    code, _, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "DOCUMENT",
                        "--spec", write_spec(tmp_path, DELETE_SPEC))
    assert code == 0
    result = CliRunner().invoke(cli.app, ["--env", "sandbox", "changes", "plan", "--entry",
                                          ENTRY_ID, "--family", "magic", "--spec", "-"])
    assert result.exit_code == 2


def test_apply_with_unconfirmed_write_exits_with_partial_code(invoke, fake, tmp_path) -> None:
    _, planned, _ = invoke("changes", "plan", "--entry", ENTRY_ID, "--family", "document",
                           "--spec", write_spec(tmp_path, DELETE_SPEC))
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
    assert "secret" not in result.stdout.replace("client_secret", "")
    legacy_code, legacy, _ = invoke("profiles", "list", env=None)
    assert legacy_code == 0 and legacy["data"] == payload["data"]


def test_logout_explains_there_is_nothing_to_revoke(invoke) -> None:
    code, payload, _ = invoke("logout", env=None)
    assert code == 6
    assert payload["error"]["code"] == "unsupported_capability"
    assert "unset" in payload["error"]["message"]


def test_auth_status_reports_without_calling_the_api(invoke, fake) -> None:
    code, payload, _ = invoke("auth", "status")
    assert code == 0
    assert payload["meta"]["profile"] == "sandbox"
    assert "secret" not in json.dumps(payload["data"])
    assert fake.calls == []


def test_invalid_endpoint_variable_fails_validation_without_echoing_value(
    invoke, monkeypatch
) -> None:
    monkeypatch.setenv("BENCHLING_SANDBOX_API_ENDPOINT", "http://insecure.example/")
    code, payload, result = invoke("auth", "status")
    assert code == 2
    assert "insecure.example" not in result.stdout


def test_cli_without_arguments_shows_help() -> None:
    result = CliRunner().invoke(cli.app, [])
    assert "Usage" in result.stdout and "changes" in result.stdout
