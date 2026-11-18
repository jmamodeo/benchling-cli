# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Breaking:** the concept formerly called a "profile" is now consistently an "environment". The JSON envelope key `meta.profile` is now `meta.env`, the `auth status` and `doctor` data key `profile` is now `env`, and saved change plans store `env` instead of `profile`. Plans created by 0.1.x are not readable by this version; preview them again.
- The package version is defined once, in `benchling_agent.__version__`; `pyproject.toml` reads it.
- The code base is formatted with `ruff format`, enforced in CI, and public modules, classes and functions have docstrings.

### Added

- `benchling --version`.

### Removed

- **Breaking:** the hidden `--profile`/`-p` option and the hidden `profiles` command group. `--env`/`-e` and `envs` are the only way to select and list environments.
- **Breaking:** the `login` and `logout` commands, which only printed an unsupported-capability error. Authentication is environment-variable only and is documented in the README.
- **Breaking:** `entry get --api-version`, a raw Benchling V2 diagnostic passthrough. `entry get` always returns the typed V3 snapshot.

## [0.1.2] - 2026-11-18

### Added

- Offline test coverage for the MCP server (every tool, per-environment write guard, error envelopes), document and table planning and applying (validation, version preconditions, partial completion, cell-state preservation), field validation, result planning, the CLI commands, the API client (retries, timeouts, pagination) and service read paths, all against a synthetic in-process fake of the Benchling API.
- Total branch coverage rose from 66% to 98%; the enforced CI floor is now 96%. The suite has 220 test functions (381 collected cases).

### Fixed

- `replace_text` now accepts the flattened V3 text parts (`DocumentPartText`) that current API responses return, and preserves their formatting. Previously only the legacy nested `TextDocumentContent` shape was recognized.

## [0.1.1] - 2026-11-18

### Changed

- Split the change-planning module into a `benchling_agent.plans` package with one module per change family (document, table, fields, results) plus a shared core and repository. Imports from `benchling_agent.plans` are unchanged and behavior is identical.
- `AuthManager` now takes a plain mapping of environment name to client secret instead of a credential-store object.
- Environment-only configuration is now the single authentication path in the code, matching the documentation.

### Removed

- Unused browser-login server, OS keychain storage, API-key authentication, delegated (authorization-code) flow and saved-profile persistence. None of these were reachable from the `benchling` CLI or the MCP server. The `keyring` dependency and its transitive dependencies were dropped.

### Added

- Static type checking with mypy (strict-ish configuration) in CI and the dev extra; the package now ships `py.typed`.
- Test coverage reporting with pytest-cov (branch coverage, enforced floor) in CI, and a coverage badge in the README with a script to refresh it.

### Fixed

- Type annotations throughout the package, found by the new mypy check.

## [0.1.0] - 2026-11-18

### Added

- Initial release.
- `benchling` JSON-first command-line interface and local MCP server (stdio) sharing one typed service layer.
- Read commands for entries, sections, parts, tables, schemas, entry fields, and results.
- Preview-then-apply change plans for document, table, fields, and assay-result families, with version preconditions and a local execution journal.
- Multi-environment configuration via `BENCHLING_<ENV>_API_ENDPOINT`, `_CLIENT_ID`, and `_CLIENT_SECRET`; OAuth client-credentials auth with in-memory token caching.
- Read-only MCP server by default, with per-environment `--allow-writes`.
- Versioned JSON envelope, stable exit codes, and published JSON schemas.
- Operating-system trust-store TLS verification.

[Unreleased]: https://github.com/jmamodeo/benchling-cli/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jmamodeo/benchling-cli/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jmamodeo/benchling-cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jmamodeo/benchling-cli/releases/tag/v0.1.0
