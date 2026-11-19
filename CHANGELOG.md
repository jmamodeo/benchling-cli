# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `benchling export` and the MCP tool `export` write an unstructured table, a results table, or assay results to a JSON or CSV file. The response is a summary only: row count, column names, file path, and the SHA-256 checksum of the file. Pages are read internally. An existing file is left unchanged unless overwrite is set. Benchling is only read.
- `benchling results rows PART_ID` and the MCP tool `list_results_table_rows` read one page of a results table from `GET /document-part-results-table/{id}/rows/items`. The page size defaults to 50 and stops at 100. The response uses the standard JSON envelope and includes `next_token`.

### Fixed

- `export --kind assay-results` writes one column per result-schema field. V3 results keep those values in `schemaFields`, and the column name is the field's display name. Links and entity references are written as ids.
- `export --part` accepts a `strtbl_…` id from `results tables` when `--entry` is that entry and `--kind` is `results-table`. The id is resolved to the entry's V3 results-table part, or the rows are read as assay results for that entry and schema. A `strtbl_…` id without `--entry`, or with `--kind table`, is a validation error that names the `v3_document_part` id from `entry inspect`.
- `entry list --name` accepts a name that contains a comma. `name.anyOf` and `names.anyOf` are comma-separated and document no escape, so that name is sent as the V2 `name` parameter together with the other filters.
- A modified timestamp with no time zone is a validation error. `YYYY-MM-DD` is still the start or end of that UTC day.
- Entry and schema lists return `modified_at` as a UTC timestamp with a `Z` suffix. V2 `+00:00` values and other offsets are converted.

### Fixed

- During `auth login`, the keychain error's `dbus-run-session` command fills in the environment name, the normalized tenant URL, and the client ID that were passed or already saved. Each value is shell-quoted. `$KEYRING_PASSWORD` and `$BENCHLING_CLIENT_SECRET` stay variable references. Omitted values, and errors from other commands, keep the placeholders.
- The keychain error is the cause, one fix command, and a pointer to the README Headless Linux section. `dbus-run-session` also runs `benchling auth login` in that same shell. It says `DBUS_SESSION_BUS_ADDRESS` is unset only when it is unset, and says the bus has no Secret Service when the variable is set but the bus has no keyring. The MCP client `env` block and cron note is included only when `benchling mcp serve` raises the error.
- The README Headless Linux section no longer says `gnome-keyring-daemon --unlock` prints `DBUS_SESSION_BUS_ADDRESS`. It shows a long-lived `dbus-daemon --session --fork` bus that MCP, later shells, and cron can share, notes a systemd user bus, and says a wrong `KEYRING_PASSWORD` for an existing login keyring hangs the first keyring call.
- Closing MCP stdin after a tool call exits 0 and does not print `MCP server failed to start` / `Event loop is closed`.
- A token request that cannot reach the host says to check the tenant URL. The raw transport error, including SSL text, stays in `details.reason`. `details` also includes `env` and `tenant_url`.
- A 401 from token exchange says `Benchling at https://<tenant> rejected the client ID or secret.` `details` includes `env`, `tenant_url`, and `status`. `--secret-stdin` with an empty stdin says `No client secret was received on stdin`.

### Added

- `benchling entry list` and the MCP tool `list_entries` list entries by project, folder, schema, modified date range, and exact name. Filters are server-side. A folder filter uses V2 `GET /entries`, because V3 `GET /entry/items` ignores `folderId`. The other filters use V3 unless a folder is set, in which case the whole query stays on V2. Pages default to 50, cap at 100, and return `next_token`.
- `benchling schema list` and the MCP tool `list_schemas` list entry, result, and run schemas (`/entry-schema/items`, `/result-schema/items`, `/run-schema/items`), following every upstream page up to the caller's limit.
- `schema get` and `get_schema` include each dropdown field's options from `/dropdown/{id}` and `/dropdown/{id}/options/items`.
- `contracts/list-parameters.json` records the query parameters entry and schema lists may send. Tests reject a list call that adds a parameter outside that list. Write bodies are checked against the vendored OpenAPI snapshots.

### Fixed

- A document move that would not leave the entry starting with a section fails in preview with `Entry must start with a section`, before a plan is saved or anything is written.
- Apply journal events record the raw HTTP status of each write (`http_status`).

### Added

- `benchling auth login` accepts `--tenant-url` and `--client-id`. Together with `--secret-stdin`, login runs with no terminal and does not call `getpass`, so there is no echo warning. A missing value with no terminal is one error that names the flags.
- Keychain failures name the cause (no Secret Service backend, D-Bus session not reachable, or a locked collection) and the headless Linux commands (`dbus-run-session` and `gnome-keyring-daemon --unlock --components=secrets`). README has a Headless Linux section. There is still no plaintext fallback.
- `benchling mcp serve --verbose` turns request logging on. It is off by default.

### Fixed

- Unstructured table reads follow the current V3 shape: `rows` is a URL, pages come from `.../rows/items` at `pageSize` 100, cells use `inputValue.value`, and the version precondition is the entry version.
- Unstructured table writes use `update_table_cells` and `insert_table_rows` on `PATCH /entry/{id}`. A stale entry version (HTTP 409) is the same conflict as a stale plan. The full-table replace operation is removed.
- Document insert, delete, move, and text replacement send `position`, `sourceRange`, and `destinationPosition`.
- Entry field writes send field-definition IDs. Plans still accept a system name or an ID and show both.
- `entry get --full` and `part get` return unstructured-table and results-table rows. A failure to read those rows fails the request. `DocumentPartResultsTable` and `DocumentPartUnavailable` are modeled.
- HTTP 405 is reported as `api_drift`: the endpoint no longer supports the method.

### Changed

- Repository links now point at `https://github.com/jmamodeo/benchling-toolkit` after the GitHub repository rename. Older changelog text is unchanged.
- **Breaking:** `BENCHLING_<ENV>_API_ENDPOINT`, `BENCHLING_<ENV>_CLIENT_ID`, and `BENCHLING_<ENV>_CLIENT_SECRET` are no longer read. There is no fallback or alias. Configure an environment with `benchling auth login --env NAME`.
- Tenant URLs accept a bare host and a trailing `/api/v2` or `/api/v3`. A trailing slash is not shown. `http://` is rejected on its own. The tenant URL and the client ID are validated separately.
- A 401 from the token endpoint names the configured tenant URL and includes `env` and `tenant_url` in `details`.
- `benchling doctor` exits 3 with `ok: false` when a check fails. Each failed check has a hint. `auth status --check` puts the specific cause in the top-level message.
- An invalid environment name explains the lowercase-letters-and-digits rule. Logging out an environment that is not configured says so.
- MCP startup errors, including a missing keychain, are a JSON envelope on stderr and a non-zero exit. Only that `mcp serve` path adds that the MCP SDK launches stdio servers with only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`, so `DBUS_SESSION_BUS_ADDRESS` has to be set in the client `env` block. Cron needs the same long-lived bus.
- `--secret-stdin` reads the controlling terminal with `os.open` and `FileIO`. Opening `/dev/tty` in text mode `r+` is rejected on Linux and macOS, which made that flag fail even when a terminal existed.

### Removed

- **Breaking:** `benchling envs list`. `benchling auth status` is the only list of environments. There is no alias.
- The `early_access` field on saved environment metadata. V3 requests still send `EARLY-ACCESS: true`.

### Added

- `benchling auth login --env NAME` prompts for the tenant URL and client ID, reads the client secret from a hidden prompt or `--secret-stdin` (never from an argument), and saves only after a client-credentials token request succeeds. The tenant URL and client ID go in `environments.json` (mode 0600). The client secret goes in the OS keychain (service `benchling-toolkit`).
- `benchling auth logout --env NAME` removes that `environments.json` record and its keychain secret.
- `benchling auth status` lists saved environments and whether a keychain secret is present, without printing the secret. `--check` requests a token and reports the result without printing or saving it. The previous single-environment `access_token_usable` field is now `access_token_cached` on each environment (in-memory only). `credential_source` is `keyring`.

### Removed

- **Breaking:** environment-variable credentials. A missing OS keychain is an error. Client secrets are not written to a plaintext file.

## [0.3.0] - 2026-11-19

### Changed

- **Breaking:** the distribution is renamed from `benchling-agent` to `benchling-toolkit`, and the import package from `benchling_agent` to `benchling_toolkit`. Install and import the new name. The old install name and import path are not provided.
- **Breaking:** per-user state now lives in a `benchling-toolkit` directory. Override it with `BENCHLING_TOOLKIT_HOME` (previously `BENCHLING_AGENT_HOME`). Plans and journals written by 0.2.x are not read.
- **Breaking:** schema `$id` values and the installed data files under `share/benchling-toolkit/` use the new name.
- The `benchling` command is unchanged. It still runs the CLI and `benchling mcp serve`. `benchling --version` prints `benchling-toolkit 0.3.0`.
- Docs describe this project as a toolkit of safe, auditable tools — a JSON-first CLI and a read-only-by-default MCP server — that AI agents use to work with Benchling.

## [0.2.0] - 2026-11-18

### Changed

- **Breaking:** the concept formerly called a "profile" is now consistently an "environment". The JSON envelope key `meta.profile` is now `meta.env`, the `auth status` and `doctor` data key `profile` is now `env`, and saved change plans store `env` instead of `profile`. Plans created by 0.1.x are not readable by this version; preview them again.
- The package version is defined once, in `benchling_agent.__version__`; `pyproject.toml` reads it.
- The test suite was audited for redundancy: duplicated interface, CLI and MCP-serve tests, hand-rolled planner doubles that the fake-backed plan tests already covered, and overlapping service-read tests were removed or merged, and same-branch parametrizations were collapsed. Unique assertions were kept. The suite is now 199 test functions (357 collected cases) at 98% branch coverage; the CI floor stays at 96%.
- Long functions (`build_mcp_server`, snapshot assembly, the table, document and result planners, and plan apply) were split into small helpers, and plan and apply dispatch by change family through a mapping. Behavior is unchanged.
- The code base is formatted with `ruff format`, enforced in CI, and public modules, classes and functions have docstrings.

### Added

- `benchling --version`.
- `scripts/demo.py`, which runs the real CLI and MCP server against the offline fake API and produces the transcript shown in the README. A test keeps it runnable.
- A `build` job in CI that builds the sdist and wheel, runs `twine check --strict`, and smoke-tests the installed wheel.
- README sections for why the project exists, an architecture diagram, the safety model, design decisions and tradeoffs, and known limitations.

### Removed

- **Breaking:** the hidden `--profile`/`-p` option and the hidden `profiles` command group. `--env`/`-e` and `envs` are the only way to select and list environments.
- **Breaking:** the `login` and `logout` commands, which only printed an unsupported-capability error. Authentication is environment-variable only and is documented in the README.
- **Breaking:** `entry get --api-version`, a raw Benchling V2 diagnostic passthrough. `entry get` always returns the typed V3 snapshot.

## [0.1.2] - 2026-11-18

### Added

- Offline test coverage for the MCP server (every tool, per-environment write guard, error envelopes), document and table planning and applying (validation, version preconditions, partial completion, cell-state preservation), field validation, result planning, the CLI commands, the API client (retries, timeouts, pagination) and service read paths, all against a synthetic in-process fake of the Benchling API.
- Total branch coverage rose from 66% to 98%; the enforced CI floor is now 96%. The suite has 220 test functions (381 collected cases).

### Fixed

- `replace_text` now accepts the flattened V3 text parts (`DocumentPartText`) that current API responses return, and preserves their formatting. Previously only the legacy nested `TextDocumentContent` shape was recognized. This fix is verified against offline fixtures only; it has not been checked against a live Benchling tenant.

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

[Unreleased]: https://github.com/jmamodeo/benchling-toolkit/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jmamodeo/benchling-toolkit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jmamodeo/benchling-toolkit/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jmamodeo/benchling-toolkit/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jmamodeo/benchling-toolkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jmamodeo/benchling-toolkit/releases/tag/v0.1.0
