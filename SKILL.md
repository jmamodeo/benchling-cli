---
name: benchling-toolkit
description: "Use the benchling CLI and its local MCP server to list, read, and export a Benchling notebook, and to preview a change before applying it. Use when the user asks to list entries, read an entry, section, part, unstructured table, schema, or assay result, export a table or results to JSON or CSV, or plan and then apply a document, table, field, or result change. Credentials come from benchling auth login --env NAME and stay in the OS keychain. The MCP server is read-only unless it was started with --allow-writes ENV. Do not use this for browser automation, or when no environment has been saved with auth login."
license: MIT
compatibility: "Requires the benchling command from benchling-toolkit (Python 3.11+), an OS keychain, and network access to the tenant saved for that environment. The MCP server is local stdio."
metadata:
  summary: "Safe reads and previewed writes for a Benchling notebook. Lists, reads, and exports entries, tables, and assay results. Applies a change only after a local plan. The MCP server is read-only unless started with --allow-writes ENV."
  version: "0.3.0"
  docs_url: "https://github.com/jmamodeo/benchling-toolkit"
---

# benchling-toolkit

Unofficial. Not affiliated with or endorsed by Benchling, Inc.

Use this when an agent should work in a Benchling tenant through the `benchling` command or the local MCP server. The public HTTP API is the only path. Do not automate the browser.

## When to use it

Use this skill when the person asks to:

- list entries, schemas, or assay results
- read an entry, a section, a document part, an unstructured table, entry fields, or results-table rows
- export a table or assay results to a local JSON or CSV file
- preview a document, table, field, or result change, and apply it only after they accept that plan

Stop and do not improvise when:

- no environment is saved yet
- the person has not accepted a specific plan id, and the next step would write to Benchling
- the task needs the Benchling website rather than the API

## Safety defaults

The MCP server is read-only by default. `benchling mcp serve` does not enable writes. `apply_changes` is refused until that process was started with `--allow-writes ENV` for the environment in the call. Repeat the flag once per environment.

`changes plan` and the MCP tool `plan_changes` only read Benchling. They write an immutable local plan. They do not need `--allow-writes`.

`changes apply PLAN_ID` is the remote write. Run it only after the person accepts that plan id, and only with the same `--env` that created the plan. Show them the plan id, the family, and the preview in the envelope before you apply. A plan is single-use. A stale plan exits 5. Do not retry a write that exits 8 (partial or unknown).

`export` writes a local file and does not change Benchling. It does not need `--allow-writes`. Leave an existing file in place unless the person asked to replace it with `--overwrite`.

There is no default environment. Pass `--env NAME` on every API command. `-e` is the short form. `NAME` uses lowercase letters and digits, starts with a letter, and is at most 32 characters. Use a read environment for reads. Use a write environment only when the person asked for a write.

## Credentials

The person saves an environment in their own terminal:

```console
benchling auth login --env NAME
```

The client secret is read from a hidden prompt, or from stdin when they pass `--secret-stdin`. It is never a command argument. Login saves nothing until a client-credentials token request succeeds. The tenant URL and client ID go in `environments.json`. The client secret goes in the OS keychain under the service name `benchling-toolkit`, with the environment name as the keychain username.

Do not run `auth login`. Do not ask for the client secret, read it from the environment, or print it. If a command fails because the environment is missing or the keychain has no secret, stop and tell the person to run `benchling auth login --env NAME` in their own terminal.

These commands print no secret:

```console
benchling auth status
benchling --env NAME auth status --check
benchling --env NAME doctor
```

`auth status` lists saved environments and whether a keychain secret is present. `--check` requests a token and does not print or save it. `doctor` makes read-only API checks. A failed doctor check exits 3.

If `benchling` is not on `PATH`, run `uv run benchling` from a checkout of this repository. The same subcommands apply.

## How to read a response

Put `--env` before the subcommand. Stdout is one JSON object with `ok`, `data`, `error`, and `meta`. Stderr is diagnostics. A nonzero exit is a failure.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 2 | Input or validation error |
| 3 | Authentication error |
| 4 | Access denied or not found |
| 5 | Concurrent modification, or a plan already used |
| 6 | Unsupported capability |
| 7 | Upstream or transport failure |
| 8 | Partial or unknown write |

Identifiers in the examples (`etr_example`, `docpart_example`, `docpart_table1`, `docpart_rt1`, `docpart_text1`, `docpart_section1`, `strtbl_example`, `src_example`, `lib_example`, `assaysch_example`, `EXP-0001`, `RESULT_ID`, `PLAN_ID`, `NEXT_TOKEN`) are placeholders. Substitute ids from the command you just ran. Do not invent a tenant URL or an organization name.

An entry reference may be a canonical id, a display id, or a URL on the selected tenant.

## Read workflow

### List

Each list call returns one page. Pass `next_token` back as `--cursor`. A short page is not the whole result.

```console
benchling --env NAME entry list --project src_example --limit 50
benchling --env NAME entry list --folder lib_example --name "Example entry"
benchling --env NAME entry list --modified-after 2026-01-01 --cursor NEXT_TOKEN
benchling --env NAME schema list
benchling --env NAME schema list --kind result --limit 50
benchling --env NAME results list --entry etr_example --page-size 100
```

`entry list` filters are applied by Benchling. `--name` is an exact match, not a substring. `--folder` uses the V2 entry list. `--modified-after` and `--modified-before` take `YYYY-MM-DD` or an RFC3339 timestamp that includes a time zone. `--limit` is 1 to 100 and defaults to 50 on `entry list` and `schema list`. `schema list --kind` is `entry`, `result`, or `run`. Omit `--kind` to list all three. `results list` uses `--page-size` (default 100, maximum 100), plus optional `--schema` and `--cursor`.

### Read entries, tables, and results

Start with `inspect` for structure. Use `entry get --full` when you need the content. For a large table, export it instead of pulling every row into context.

```console
benchling --env NAME entry resolve EXP-0001
benchling --env NAME entry inspect etr_example
benchling --env NAME entry get etr_example --full
benchling --env NAME entry fields get etr_example
benchling --env NAME section get etr_example docpart_section1
benchling --env NAME part get etr_example docpart_text1
benchling --env NAME table get docpart_table1
benchling --env NAME schema get assaysch_example
benchling --env NAME results get RESULT_ID
benchling --env NAME results tables etr_example
benchling --env NAME results rows docpart_example --limit 50 --cursor NEXT_TOKEN
```

`table get` reads an unstructured table. Its argument is a `docpart_…` id from `entry inspect`. `results tables` returns `table_id` values that are `strtbl_…` ids. `results rows` reads one page of a results-table part (`docpart_…`) from that part's rows URL. `schema get` includes each dropdown field's options.

### Export

Export reads every page and writes a local file. The envelope is a summary only: `row_count`, `columns`, `path`, and `checksum`. The rows stay in the file.

```console
benchling --env NAME export --kind table --part docpart_table1 --format csv --output measurements.csv
benchling --env NAME export --kind results-table --part docpart_rt1 --format json --output results-table.json
benchling --env NAME export --kind results-table --part strtbl_example --entry etr_example --format csv --output listed-table.csv
benchling --env NAME export --kind assay-results --entry etr_example --schema assaysch_example --format csv --output assay.csv
```

`--kind` is `table`, `results-table`, or `assay-results`. `--format` is `json` or `csv`. `--output` is required (`-o` is the short form). `table` and `results-table` need `--part`. A `docpart_…` id comes from `entry inspect`. A `strtbl_…` id from `results tables` needs `--kind results-table` and `--entry` for that same entry. `--kind table` rejects a `strtbl_…` id. `assay-results` takes `--entry` and `--schema`, the same filters as `results list`. Add `--overwrite` only when the person asked to replace that file.

## Preview, then apply

One plan covers one entry and one family: `document`, `table`, `fields`, or `results`. Write the JSON specification to a file, then plan. `--spec -` reads that JSON from stdin. Do not combine `--spec` with `--csv`.

```console
benchling --env NAME changes plan --entry etr_example --family table --spec set-cells.json
benchling --env NAME changes apply PLAN_ID
benchling --env NAME changes status PLAN_ID
```

`changes plan` does not write to Benchling. `changes apply` re-checks the plan's preconditions, then writes. `changes status` reads the local journal and does not write.

Table, set cells. `examples/table-set-cells.json` is this shape. `table_part_id` is a `docpart_…` id.

```json
{
  "operation": "set_cells",
  "table_part_id": "docpart_example",
  "updates": [{ "row": 0, "column": 1, "value": "1.25" }]
}
```

Table, append rows:

```json
{
  "operation": "append_rows",
  "table_part_id": "docpart_example",
  "rows": [["D", "4"]]
}
```

Document. `position` is `beginning_of_document`, `end_of_document`, or a part id. A part id inserts or moves immediately before that part. Each inserted part needs `contentType`.

```json
{
  "operation": "insert",
  "position": "end_of_document",
  "parts": [{ "contentType": "text", "text": "Note" }]
}
```

```json
{ "operation": "delete", "from_part_id": "docpart_text1" }
```

```json
{
  "operation": "move",
  "from_part_id": "docpart_text1",
  "position": "end_of_document"
}
```

```json
{ "operation": "replace_text", "part_id": "docpart_text1", "text": "Edited" }
```

A move that would not leave the entry starting with a section fails in preview. No plan is saved.

Fields. Each key is a V3 system name or a field-definition id. Each value is an object with `value`.

```json
{ "schema_fields": { "temperature": { "value": 21 } } }
```

Result upload. `table_id` is an `strtbl_…` id from `results tables`. Field keys stay system names. The submission stays pending until a person integrates it in Benchling. Read that state with `changes status PLAN_ID`.

```json
{
  "table_id": "strtbl_example",
  "assay_results": [
    { "schemaId": "assaysch_example", "fields": { "ct": { "value": 12 } } }
  ]
}
```

CSV update of existing results. `--id-column` names the result id. Each `--map` is `CSV_COLUMN=FIELD`. `FIELD` is the export display name, the system name, or the field-definition id. Unmapped columns are not written.

```console
benchling --env NAME changes plan --entry etr_example --family results --csv assay.csv --id-column id --map Ct=Ct --map Note=Note
```

## MCP server

If the benchling MCP tools are already connected, call them with the same environment name. Do not put a client secret in the client config. The server reads the OS keychain.

Reads, `export`, and `plan_changes` work when the server was started as:

```console
benchling mcp serve
```

`apply_changes` works only for an environment named at startup:

```console
benchling mcp serve --allow-writes ENV
```

Do not add `--allow-writes` unless the person asked to allow writes for that environment. `export` takes `kind`, `path`, `format`, and optional `part_id`, `entry`, and `schema_id`. It returns the same summary as the CLI and writes a local file only. `plan_changes` takes `env`, `entry`, `family` (`document`, `table`, `fields`, or `results`), and `specification` (the same JSON shapes as `--spec`). `get_change_status` reads the journal and does not need `--allow-writes`.

Tools for this workflow: `list_entries`, `list_schemas`, `resolve_entry`, `inspect_entry`, `get_entry`, `get_entry_fields`, `get_section`, `get_part`, `get_table`, `get_schema`, `list_results`, `get_result`, `list_result_tables`, `list_results_table_rows`, `export`, `plan_changes`, `apply_changes`, `get_change_status`. `list_environments` takes no environment.
