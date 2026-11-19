"""Offline checks for the public V3 OpenAPI drift script."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://example.com/specs/v3/openapi.yaml"
ROUTES = ["/widgets/items", "/widgets/{id}", "/other"]


def _load_drift() -> Any:
    path = ROOT / "scripts" / "check_openapi_drift.py"
    spec = importlib.util.spec_from_file_location("check_openapi_drift", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = _load_drift()


def _ban_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("openapi drift tests must not use the network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _ban_network)


def _operation(operation_id: str, schema_ref: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"description": "ok"}
    if schema_ref is not None:
        response["content"] = {"application/json": {"schema": {"$ref": schema_ref}}}
    return {"get": {"operationId": operation_id, "responses": {"200": response}}}


def _base_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "fixture", "version": "1"},
        "paths": {
            "/widgets/items": _operation("listWidgets", "#/components/schemas/WidgetPage"),
            "/widgets/{widget_id}": _operation("getWidget", "#/components/schemas/Widget"),
            "/other": _operation("other"),
        },
        "components": {
            "schemas": {
                "Widget": {"type": "object", "properties": {"name": {"type": "string"}}},
                "WidgetPage": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Widget"},
                        }
                    },
                },
                "Unused": {"type": "object", "description": "untouched"},
            }
        },
    }


def _dump(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False)


def _setup(
    tmp_path: Path,
    snapshot: dict[str, Any] | str,
    public: dict[str, Any] | str | None = None,
    *,
    routes: list[str] | None = None,
    url: str = DEFAULT_URL,
) -> Path:
    root = tmp_path / "repo"
    contracts = root / "contracts"
    contracts.mkdir(parents=True)
    snapshot_text = snapshot if isinstance(snapshot, str) else _dump(snapshot)
    snapshot_bytes = snapshot_text.encode()
    (contracts / "benchling-v3-openapi.yaml").write_bytes(snapshot_bytes)
    manifest = {
        "contracts": [
            {
                "name": "benchling-v3",
                "url": url,
                "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                "snapshot": "contracts/benchling-v3-openapi.yaml",
                "routes_used": list(ROUTES if routes is None else routes),
            }
        ]
    }
    (contracts / "openapi-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if public is not None:
        public_text = public if isinstance(public, str) else _dump(public)
        (root / "public.yaml").write_text(public_text, encoding="utf-8")
    return root


def _run(root: Path) -> int:
    return drift.main(["--root", str(root), "--spec-file", str(root / "public.yaml")])


def test_matching_spec_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document = _base_document()
    root = _setup(tmp_path, document, document)
    assert _run(root) == 0
    out = capsys.readouterr().out
    assert "benchling-v3 matches contracts/openapi-manifest.json" in out
    assert f"source: {DEFAULT_URL}" in out


def test_difference_outside_used_paths_names_no_changed_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["info"]["version"] = "2"
    public["components"]["schemas"]["Unused"]["description"] = "edited"
    root = _setup(tmp_path, snapshot, public)
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "sha256 differs from contracts/openapi-manifest.json" in err
    assert "None of the paths the toolkit uses changed:" in err
    for route in ROUTES:
        assert f"  {route}" in err
    assert "Changed paths the toolkit uses" not in err
    assert "differs outside those paths" in err


def test_schema_change_names_the_routes_that_reference_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["components"]["schemas"]["Widget"]["properties"]["id"] = {"type": "string"}
    root = _setup(tmp_path, snapshot, public)
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "Changed paths the toolkit uses (2 of 3):" in err
    assert "  /widgets/items" in err
    assert "  /widgets/{id}" in err
    assert "\n  /other\n" not in err
    assert "changed referenced definition #/components/schemas/Widget" in err
    assert "changed referenced definition #/components/schemas/WidgetPage" not in err


def test_path_item_change_names_that_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["paths"]["/widgets/items"]["get"]["operationId"] = "listWidgetsV2"
    root = _setup(tmp_path, snapshot, public)
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "Changed paths the toolkit uses (1 of 3):" in err
    assert "  /widgets/items" in err
    assert "path item changed" in err
    assert "/widgets/{id}" not in err
    assert "/other" not in err


def test_removed_path_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    del public["paths"]["/other"]
    root = _setup(tmp_path, snapshot, public)
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "  /other" in err
    assert "removed from the public spec (vendored path /other)" in err
    assert "/widgets/items" not in err


def test_parameter_rename_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["paths"]["/widgets/{id}"] = public["paths"].pop("/widgets/{widget_id}")
    root = _setup(tmp_path, snapshot, public)
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "  /widgets/{id}" in err
    assert "path template /widgets/{widget_id} is /widgets/{id} in the public spec" in err
    assert "path item changed" not in err


def test_duplicate_routes_are_reported_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["paths"]["/widgets/items"]["get"]["operationId"] = "listWidgetsV2"
    root = _setup(
        tmp_path,
        snapshot,
        public,
        routes=["/widgets/items", "/widgets/items"],
    )
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "Changed paths the toolkit uses (1 of 1):" in err
    assert err.count("  /widgets/items") == 1


def test_ambiguous_route_shape_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = {
        "openapi": "3.1.0",
        "info": {"title": "fixture", "version": "1"},
        "paths": {
            "/widgets/{widget_id}": _operation("byLongName"),
            "/widgets/{id}": _operation("byShortName"),
        },
    }
    public = copy.deepcopy(snapshot)
    public["info"]["version"] = "2"
    root = _setup(tmp_path, snapshot, public, routes=["/widgets/{id}"])
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "  /widgets/{id}" in err
    assert "route shape is ambiguous" in err
    assert "/widgets/{widget_id}" in err
    assert "/widgets/{id}" in err


def test_many_referenced_definitions_are_capped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schemas = {f"S{index:02d}": {"type": "object", "description": "ok"} for index in range(10)}
    snapshot = {
        "openapi": "3.1.0",
        "info": {"title": "fixture", "version": "1"},
        "paths": {
            "/widgets/items": {
                "get": {
                    "operationId": "listWidgets",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [
                                            {"$ref": f"#/components/schemas/S{index:02d}"}
                                            for index in range(10)
                                        ]
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {"schemas": schemas},
    }
    public = copy.deepcopy(snapshot)
    for index in range(10):
        public["components"]["schemas"][f"S{index:02d}"]["description"] = "changed"
    root = _setup(tmp_path, snapshot, public, routes=["/widgets/items"])
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "  /widgets/items" in err
    assert "changed referenced definition #/components/schemas/S00" in err
    assert "changed referenced definition #/components/schemas/S07" in err
    assert "changed referenced definition #/components/schemas/S08" not in err
    assert "and 2 more referenced definitions" in err


def test_unquoted_status_codes_still_compare(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = """\
openapi: "3.1.0"
info:
  title: fixture
  version: "1"
paths:
  /widgets/items:
    get:
      description: 2026-01-02
      responses:
        200:
          description: ok
"""
    root = _setup(
        tmp_path,
        snapshot,
        snapshot.replace('version: "1"', 'version: "2"', 1),
        routes=["/widgets/items"],
    )
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "None of the paths the toolkit uses changed:" in err
    assert "OpenAPI drift check failed" not in err


def test_invalid_public_yaml_fails_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _setup(tmp_path, _base_document(), ":\n  - [\n")
    assert _run(root) == 1
    assert "Could not parse" in capsys.readouterr().err


def test_snapshot_checksum_mismatch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _base_document()
    root = _setup(tmp_path, document, document)
    manifest_path = root / "contracts" / "openapi-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contracts"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _run(root) == 1
    assert "does not match" in capsys.readouterr().err


def test_http_manifest_url_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document = _base_document()
    root = _setup(tmp_path, document, document, url="http://example.com/openapi.yaml")
    assert _run(root) == 1
    assert "https" in capsys.readouterr().err


def test_download_uses_the_manifest_url_and_sends_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["paths"]["/other"]["get"]["operationId"] = "otherV2"
    root = _setup(tmp_path, snapshot)
    public_bytes = _dump(public).encode()
    captured: dict[str, Any] = {}

    class _Response:
        def __init__(self) -> None:
            self.offset = 0

        def read(self, amount: int = -1) -> bytes:
            if amount < 0:
                amount = len(public_bytes) - self.offset
            chunk = public_bytes[self.offset : self.offset + amount]
            self.offset += len(chunk)
            return chunk

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = list(request.header_items())
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    code = drift.main(["--root", str(root)])
    err = capsys.readouterr().err
    assert code == 1
    assert captured["url"] == DEFAULT_URL
    assert captured["timeout"] == 60.0
    assert all("authorization" not in name.lower() for name, _value in captured["headers"])
    assert "  /other" in err
    assert "path item changed" in err


def test_download_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: urllib.request.Request, timeout: float = 0) -> None:
        raise urllib.error.HTTPError(request.full_url, 404, "missing", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(drift.DriftCheckError, match="HTTP 404"):
        drift.download_spec(DEFAULT_URL)


def test_download_rejects_an_empty_or_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.offset = 0

        def read(self, amount: int = -1) -> bytes:
            if amount < 0:
                amount = len(self.payload) - self.offset
            chunk = self.payload[self.offset : self.offset + amount]
            self.offset += len(chunk)
            return chunk

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(b""))
    with pytest.raises(drift.DriftCheckError, match="empty"):
        drift.download_spec(DEFAULT_URL)

    oversized = b"x" * (drift.MAX_SPEC_BYTES + 1)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(oversized))
    with pytest.raises(drift.DriftCheckError, match="exceeds"):
        drift.download_spec(DEFAULT_URL)


def test_github_actions_annotation_names_changed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _base_document()
    public = copy.deepcopy(snapshot)
    public["paths"]["/widgets/items"]["get"]["operationId"] = "listWidgetsV2"
    root = _setup(tmp_path, snapshot, public)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "::error::" in err
    assert "changed paths the toolkit uses: /widgets/items" in err


def test_parameter_suffix_is_a_different_route() -> None:
    plain = drift.route_key("/result-schema/{result_schema_id}")
    action = drift.route_key("/result-schema/{result_schema_id}:set-field-definitions")
    assert plain != action
    assert plain == drift.route_key("/result-schema/{id}")


def test_script_reads_the_spec_url_from_the_manifest() -> None:
    source = (ROOT / "scripts" / "check_openapi_drift.py").read_text(encoding="utf-8")
    assert "benchling.com" not in source
    workflow = (ROOT / ".github" / "workflows" / "openapi-drift.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert 'cron: "0 12 * * 1"' in workflow
    assert "scripts/check_openapi_drift.py" in workflow
    assert "https://" not in workflow
    assert "secrets." not in workflow
    assert "pull_request:" not in workflow


def test_vendored_v3_snapshot_matches_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    manifest = drift.load_manifest(ROOT / "contracts" / "openapi-manifest.json")
    contract = drift.v3_contract(manifest)
    code = drift.main(
        [
            "--root",
            str(ROOT),
            "--spec-file",
            str(ROOT / str(contract["snapshot"])),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert f"benchling-v3 matches contracts/openapi-manifest.json ({contract['sha256']})" in out
    assert f"source: {contract['url']}" in out


def test_every_v3_route_matches_one_vendored_path() -> None:
    manifest = drift.load_manifest(ROOT / "contracts" / "openapi-manifest.json")
    contract = drift.v3_contract(manifest)
    spec = drift.parse_spec(
        (ROOT / str(contract["snapshot"])).read_text(encoding="utf-8"),
        str(contract["snapshot"]),
    )
    for route in contract["routes_used"]:
        matches = drift.matching_paths(spec, route)
        assert len(matches) == 1, (route, matches)
