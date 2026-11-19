# Benchling API research record

Research date: 2026-09-24

Live contract checks on 2026-09-29 against a development entry in a test tenant:

- V3 document part objects are flattened `DocumentPart*` unions, consistent with the [entry guide updated on September 28](https://docs.benchling.com/docs/working-with-entries). The adapter recognizes sections and retains original raw part objects.
- V2 entry, folder, and project responses wrap their resources. Task responses can be flat status objects without an ID property; validate status rather than requiring a resource ID. Successful task responses return both UUID result IDs and canonical V3 `result_*` IDs; status polling persists those in the execution journal without inferring a per-record mapping from list positions.
- The public V2 OpenAPI still declares optional `AssayResultCreate.id`, but the tested tenant rejects that property in `/assay-results:bulk-create` with HTTP 400. The CLI therefore rejects caller IDs in table upload plans, omits remote IDs, and journals separate local correlation UUIDs. These are explicitly not idempotency keys. No write is automatically retried.
- The [result upload guide](https://docs.benchling.com/docs/example-creating-results) limits notebook table bulk requests to 500 records and distinguishes submission from browser integration. The initial upload state is `submission_pending`; only task success confirms `pending_integration`.

Authentication: Benchling's [authentication contract](https://docs.benchling.com/docs/authentication) specifies form-encoded `grant_type=client_credentials` at `/oauth/token`. App access tokens last 15 minutes; this grant does not issue a refresh token. Renew by repeating the token request. The same bearer token works for V2 and V3, with the app's own permissions and audit identity. `benchling auth login` verifies that grant before saving the tenant URL and client ID in `environments.json` and the client secret in the OS keychain. A tenant URL may be a bare host or end in `/api/v2` or `/api/v3`; only `https` is accepted. Access tokens stay in process memory and are not written to the keychain or to `environments.json`. On a headless Linux host the keychain is an unlocked Secret Service reached by `DBUS_SESSION_BUS_ADDRESS`; there is no plaintext file.

Primary sources:

- [Authentication](https://docs.benchling.com/docs/authentication): the `client_credentials` grant and app access-token lifetime.
- [Introduction to Documents](https://docs.benchling.com/docs/introduction-to-documents): stable part identifiers, `partsMutation`, and required document versions.
- [Working with Entries](https://docs.benchling.com/docs/working-with-entries): entry and parts retrieval.
- [Working with Sections](https://docs.benchling.com/docs/working-with-sections): flat section semantics.
- [Working with Tables](https://docs.benchling.com/docs/working-with-tables): paged unstructured-table rows, cell updates, row inserts, and evaluation limitations.
- [Uploading Results](https://docs.benchling.com/docs/example-creating-results): V2 table IDs and pending result integration.
- [V3 unified API changelog](https://docs.benchling.com/changelog/v3-rest-api-unified-api-new-stability-guidelines-and-breaking-changes): unified `/api/v3` route and early-access header.
- [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml) and [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml): request contracts and endpoint shapes.
- [Benchling SDK 1.25.0](https://benchling.com/sdk-docs/1.25.0/index.html): SDK scope and V2 service surface.
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/): stdio server, typed tools, structured output, and in-process testing.

The checksums recorded in `contracts/openapi-manifest.json` identify the exact public specifications reviewed. The YAML snapshots next to that manifest are those files. Tests validate the client's request bodies against them and do not need network access. List query parameters are also checked against `contracts/list-parameters.json`.

A parameter check on 2026-11-19 against those snapshots confirmed:

- V3 `GET /entry/items` documents `project.eq`, `schema.eq`, `name.anyOf`, and `modifiedAt.gt` / `gte` / `lt` / `lte`. It does not document `folderId`. Live tenants ignore `folderId` on that route.
- V2 `GET /entries` documents `projectId`, `schemaId`, `name`, `names.anyOf`, and one `modifiedAt` comparison string using `>` and `<`. The `listEntries` operation text does not repeat `folderId`, but the V2 components still define `folderIdFilter` (`folderId`, "Restricts results to those in the folder") and the historical SDK sends it. This client sends `folderId` only on V2, and uses `>=` / `<=` so a modified range stays inclusive. `name` is a single entry name (type string). `name.anyOf` and `names.anyOf` are comma-separated lists and document no escape, so a name that contains a comma is sent as V2 `name`. The V2 text describes `name` as the specified name and does not say the match is case-insensitive. `names.anyOf` does say case-insensitive, and that is what a name without a comma uses on V2.
- `GET /document-part-results-table/{document_part_results_table_id}` returns `rows` as a URL, plus `rowCount`, `columnDefinitions`, and `resultSchema`. `GET /document-part-results-table/{document_part_results_table_id}/rows/items` lists `StructuredTableRow` items. Its query parameters are `pageSize` (described as default 50, maximum 100) and `nextToken`. This client reads the part first and requires the rows URL to be that `/rows/items` path on the same tenant. `benchling export` does not call a separate export endpoint. It pages these row reads, unstructured-table row reads, and `GET /result/items`, and writes the rows to a local JSON or CSV file.
- V3 schema lists are `GET /entry-schema/items`, `/result-schema/items`, and `/run-schema/items`, each paged with `pageSize` (default 50, maximum 100) and `nextToken`.
- Dropdowns are `GET /dropdown/{dropdown_id}` and `GET /dropdown/{dropdown_id}/options/items` (unpaginated). A dropdown field points at the dropdown through `dropdownId` or `linkDefinition.id`.

The same day's check of the V3 spec `d2594d47…` and a live tenant set the write contract:

- `GET /document-part-unstructured-table/{id}` returns `rows` as a URL plus `rowCount`. Row pages are `GET .../rows/items` with `pageSize` at most 100. The only version is `versionMetadata.versionId`, and it is the entry version. Cell values use `inputValue.value` with `__typename` `TextValue`.
- `PATCH /document-part-unstructured-table/{id}` is gone (HTTP 405). Cell edits are `partsMutation.operationType` `update_table_cells`; appended rows are `insert_table_rows`. Both are `PATCH /entry/{id}` and both apply to unstructured tables. A stale entry version is HTTP 409 `Version mismatch`.
- Document insert uses `position`. Delete uses `sourceRange` `{start, end}`. Move uses `sourceRange` and `destinationPosition`. `coordinates`, `sourceCoordinates`, and `destinationCoordinates` are rejected as additional properties.
- `UpdateEntryInput.schemaFields` keys are field-definition IDs (`tsf_…`). A system name is HTTP 400 `schemaFields: Invalid field`. Result-record fields are unchanged and still use system names.
- V3 `GET /entry/items` ignores a `folderId` query parameter. It is not in that operation's parameter list. V2 `GET /entries` still accepts `folderId`.

Contract decisions that still hold:

- `UpdateEntryInput.schemaFields` values are `FieldValueInput` objects, each containing exactly `value`.
- V2 `AssayResultCreate.fields` explicitly requires system-name keys, and `/assay-results:bulk-create` returns a task ID for polling at `/tasks/{task_id}`.
- V2 entry `results_table` notes expose both the structured-table `apiId` and `assayResultSchemaId`, which are used rather than matching a visible table name or position.
- `DocumentPartResultsTable` exposes `columnDefinitions` and a rows URL. `DocumentPartUnavailable` returns only the fields shared by every document part.
