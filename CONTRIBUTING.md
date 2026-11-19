# Contributing

Thanks for helping improve benchling-toolkit. This project is unofficial and not affiliated with Benchling, Inc.

## Development setup

```console
git clone https://github.com/jmamodeo/benchling-toolkit.git
cd benchling-toolkit
uv sync --extra dev
```

## Tests and linting

```console
uv run pytest --cov      # offline; no credentials needed; reports coverage
uv run ruff format .     # formatting is enforced in CI (ruff format --check)
uv run ruff check .      # must pass; use --fix for mechanical fixes
uv run mypy              # must pass; strict-ish config in pyproject.toml
```

After changing code or tests, refresh the README coverage badge from the latest `pytest --cov` run:

```console
uv run python scripts/update_coverage_badge.py
```

Add or update tests for behavior you change. Tests must stay offline and use mocked HTTP.

## OpenAPI drift check

`contracts/openapi-manifest.json` records the SHA-256 of each vendored public spec, the URL that file came from, and the `routes_used` paths this toolkit calls.

`.github/workflows/openapi-drift.yml` runs every Monday at 12:00 UTC and when someone starts it with `workflow_dispatch`. The job downloads the public V3 spec from the V3 contract URL in the manifest and compares the SHA-256. The public spec needs no tenant and no credentials. The scheduled check covers the `benchling-v3` contract. The V2 snapshot stays in the manifest for offline request validation.

When the hash differs, the job fails. The log names each `routes_used` path whose operation or referenced definition changed. When the public file differs only outside those paths, the log says so and still fails.

Run the same check locally. Omit `--spec-file` to download the public spec:

```console
uv run python scripts/check_openapi_drift.py
uv run python scripts/check_openapi_drift.py --spec-file path/to/openapi.yaml
```

Unit tests for the script use fixture specs and do not download anything.

## Issues

Use the bug or feature form. Both ask what you tried, the expected and actual behaviour, the version and command or MCP tool, a description, a proposed solution, and at least one Benchling documentation link (docs.benchling.com, help.benchling.com, or the API reference) with the quoted line that shows the solution is possible. `benchling issue draft` builds the same body and refuses drafts that look like they hold tenant data or secrets.

## Pull requests

- Branch from `main` and keep each PR focused.
- Describe what changed and why; link related issues.
- Update the README, docs, and `CHANGELOG.md` (under "Unreleased") when behavior changes.
- CI (ruff format and lint, mypy, pytest with coverage on Python 3.11–3.13, `agentskills validate` for `SKILL.md`, and the package build) must pass.
- Preserve the safety model: preview-then-apply, version preconditions, read-only MCP by default.

## Releasing

Only the maintainer releases. Set the same version in `src/benchling_toolkit/__init__.py`, `CITATION.cff`, and `SKILL.md`, move the `CHANGELOG.md` entries under the new version, and merge that pull request. Then publish a GitHub release from `main` whose tag is `v` plus the version. The `Publish to PyPI` workflow builds the package and uploads it with PyPI trusted publishing (GitHub environment `pypi`); no token is stored in the repository.

## No secrets or real tenant data

Never include tokens, client secrets, real tenant URLs, real entry or record IDs, or sample data from a real lab in issues, pull requests, tests, or fixtures. Use placeholders such as `https://your-tenant-dev.benchling.com/` and `EXP-0001`.

## Licensing

Contributions are licensed under the project's [MIT License](LICENSE). No DCO sign-off or CLA is required.

## Conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
