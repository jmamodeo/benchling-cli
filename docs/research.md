# Benchling API research record

Research date: 2026-09-24

Live contract checks on 2026-09-29 against a development entry in a test tenant:

- V3 document part objects are flattened `DocumentPart*` unions, consistent with the [entry guide updated on September 28](https://docs.benchling.com/docs/working-with-entries). The adapter recognizes sections and retains original raw part objects.
- V2 entry, folder, and project responses wrap their resources. Task responses can be flat status objects without an ID property; validate status rather than requiring a resource ID. Successful task responses return both UUID result IDs and canonical V3 `result_*` IDs; status polling persists those in the execution journal without inferring a per-record mapping from list positions.
- The public V2 OpenAPI still declares optional `AssayResultCreate.id`, but the tested tenant rejects that property in `/assay-results:bulk-create` with HTTP 400. The CLI therefore rejects caller IDs in table upload plans, omits remote IDs, and journals separate local correlation UUIDs. These are explicitly not idempotency keys. No write is automatically retried.
- The [result upload guide](https://docs.benchling.com/docs/example-creating-results) limits notebook table bulk requests to 500 records and distinguishes submission from browser integration. The initial upload state is `submission_pending`; only task success confirms `pending_integration`.

Authentication: Benchling's [authentication contract](https://docs.benchling.com/docs/authentication) specifies form-encoded `grant_type=client_credentials` at `/oauth/token`. App access tokens last 15 minutes; this grant does not issue a refresh token. Renew by repeating the token request. The same bearer token works for V2 and V3, with the app's own permissions and audit identity. `benchling auth login` verifies that grant before saving the tenant URL and client ID in `environments.json` and the client secret in the OS keychain. Access tokens stay in process memory and are not written to the keychain or to `environments.json`.

Primary sources:

- [Authentication](https://docs.benchling.com/docs/authentication): the `client_credentials` grant and app access-token lifetime.
- [Introduction to Documents](https://docs.benchling.com/docs/introduction-to-documents): stable part identifiers, `partsMutation`, and required document versions.
- [Working with Entries](https://docs.benchling.com/docs/working-with-entries): entry and parts retrieval.
- [Working with Sections](https://docs.benchling.com/docs/working-with-sections): flat section semantics.
- [Working with Tables](https://docs.benchling.com/docs/working-with-tables): paged unstructured-table detail, complete replacement writes, and evaluation limitations.
- [Uploading Results](https://docs.benchling.com/docs/example-creating-results): V2 table IDs and pending result integration.
- [V3 unified API changelog](https://docs.benchling.com/changelog/v3-rest-api-unified-api-new-stability-guidelines-and-breaking-changes): unified `/api/v3` route and early-access header.
- [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml) and [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml): request contracts and endpoint shapes.
- [Benchling SDK 1.25.0](https://benchling.com/sdk-docs/1.25.0/index.html): SDK scope and V2 service surface.
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/): stdio server, typed tools, structured output, and in-process testing.

The checksums recorded in `contracts/openapi-manifest.json` identify the exact public specifications reviewed. Tests use small, checked-in contract fixtures rather than requiring network access.

Contract decisions confirmed from those snapshots:

- `UpdateEntryInput.schemaFields` is a dictionary of `FieldValueInput` objects, each containing exactly `value`; published examples key that dictionary by field `systemName`.
- V2 `AssayResultCreate.fields` explicitly requires system-name keys, and `/assay-results:bulk-create` returns a task ID for polling at `/tasks/{task_id}`.
- V2 entry `results_table` notes expose both the structured-table `apiId` and `assayResultSchemaId`, which are used rather than matching a visible table name or position.
- The unstructured-table PATCH contract is a complete row replacement guarded by `versionId`; supported cell state includes input formulas, formatting, merges, and selector IDs.
