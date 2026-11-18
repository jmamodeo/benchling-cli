# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jmamodeo/benchling-cli/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/jmamodeo/benchling-cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jmamodeo/benchling-cli/releases/tag/v0.1.0
