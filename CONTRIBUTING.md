# Contributing

Thanks for helping improve benchling-agent. This project is unofficial and not affiliated with Benchling, Inc.

## Development setup

```console
git clone https://github.com/jmamodeo/benchling-cli.git
cd benchling-cli
uv sync --extra dev
```

## Tests and linting

```console
uv run pytest        # offline; no credentials needed
uv run ruff check .  # must pass; use --fix for mechanical fixes
```

Add or update tests for behavior you change. Tests must stay offline and use mocked HTTP.

## Pull requests

- Branch from `main` and keep each PR focused.
- Describe what changed and why; link related issues.
- Update the README, docs, and `CHANGELOG.md` (under "Unreleased") when behavior changes.
- CI (ruff and pytest on Python 3.11–3.13) must pass.
- Preserve the safety model: preview-then-apply, version preconditions, read-only MCP by default.

## No secrets or real tenant data

Never include tokens, client secrets, real tenant URLs, real entry or record IDs, or sample data from a real lab in issues, pull requests, tests, or fixtures. Use placeholders such as `https://your-tenant-dev.benchling.com/` and `EXP-0001`.

## Licensing

Contributions are licensed under the project's [MIT License](LICENSE). No DCO sign-off or CLA is required.

## Conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
