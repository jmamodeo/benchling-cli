"""Typer CLI: every command prints a JSON Envelope on stdout and exits with an ExitCode."""

from __future__ import annotations

import asyncio
import getpass
import io
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import typer

from . import __version__
from .credentials import build_environment, collect_status, login, login_fix_values, logout
from .environment import (
    ENV_NAME,
    EnvironmentConfigStore,
    check_apply_allowed,
    invalid_environment_name,
)
from .errors import (
    AuthenticationError,
    BenchlingAgentError,
    ExitCode,
    UnsupportedError,
    UpstreamError,
    ValidationError,
)
from .export import export_dataset
from .issues import REPOSITORY, build_issue_draft
from .keychain import Keychain, bind_login_fix
from .models import Envelope, Environment, PlanFamily
from .redact import redact_text
from .runtime import Runtime

app = typer.Typer(
    name="benchling",
    help="Safe, auditable tools for structured Benchling entry data.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(help="Log in, log out, and inspect saved environments.")
entry_app = typer.Typer(help="Resolve, list, fetch, and inspect entries.")
entry_fields_app = typer.Typer(help="Read entry schema fields.")
template_app = typer.Typer(help="List and inspect entry templates.")
folder_app = typer.Typer(help="Read notebook folders.")
section_app = typer.Typer(help="Read entry sections.")
part_app = typer.Typer(help="Read document parts.")
table_app = typer.Typer(help="Read unstructured tables.")
schema_app = typer.Typer(help="List and read entry, result, and run schemas.")
results_app = typer.Typer(help="Read result records and results-table rows.")
container_app = typer.Typer(help="Read and search inventory containers.")
container_schema_app = typer.Typer(help="List container schemas.")
changes_app = typer.Typer(help="Preview and explicitly apply safe changes.")
mcp_app = typer.Typer(help="Run the local MCP server.")
issue_app = typer.Typer(
    help="Draft a bug report or feature request for this toolkit. Drafts are local by default."
)

app.add_typer(auth_app, name="auth")
app.add_typer(entry_app, name="entry")
entry_app.add_typer(entry_fields_app, name="fields")
app.add_typer(template_app, name="template")
app.add_typer(folder_app, name="folder")
app.add_typer(section_app, name="section")
app.add_typer(part_app, name="part")
app.add_typer(table_app, name="table")
app.add_typer(schema_app, name="schema")
app.add_typer(results_app, name="results")
app.add_typer(container_app, name="container")
container_app.add_typer(container_schema_app, name="schema")
app.add_typer(changes_app, name="changes")
app.add_typer(mcp_app, name="mcp")
app.add_typer(issue_app, name="issue")


class State:
    """Per-invocation state stored on the Typer context; env is the --env selection.

    env_on_command_line is True only when --env was typed on the command line,
    not filled from a default or an environment variable.
    """

    def __init__(self, env: str | None, *, env_on_command_line: bool = False) -> None:
        self.env = env
        self.env_on_command_line = env_on_command_line


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"benchling-toolkit {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name."),
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Safe, auditable tools for structured Benchling entry data."""
    # Typer may vendor click, so compare the ParameterSource member by name.
    source = getattr(ctx.get_parameter_source("env"), "name", None)
    ctx.obj = State(env, env_on_command_line=bool(env) and source == "COMMANDLINE")


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _emit(envelope: Envelope[Any]) -> None:
    typer.echo(_json(envelope))


async def _run_with_runtime(
    env: str | None, call: Callable[[Runtime], Awaitable[Any]]
) -> tuple[Any, Environment]:
    runtime = Runtime(env)
    try:
        return await call(runtime), runtime.environment
    finally:
        await runtime.close()


def _execute(env: str | None, call: Callable[[Runtime], Awaitable[Any]]) -> None:
    try:
        result, runtime_env = asyncio.run(_run_with_runtime(env, call))
        _emit(
            Envelope.success(
                result,
                env=runtime_env.name,
                tenant_url=runtime_env.base_url,
            )
        )
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:
        error = UpstreamError("Unexpected local failure", details={"type": type(exc).__name__})
        _emit(Envelope.failure(error))
        raise typer.Exit(int(ExitCode.UPSTREAM)) from None


def _prompt(label: str, *, tty: bool) -> str:
    """Read one non-secret line. --secret-stdin keeps stdin free for the secret."""
    if tty:
        line = _read_terminal(label)
    else:
        typer.echo(f"{label}: ", nl=False, err=True)
        line = sys.stdin.readline()
    if not line.strip():
        raise ValidationError(f"{label} is required")
    return line.strip()


def _read_terminal(label: str) -> str:
    """Prompt on the controlling terminal so a piped secret is not consumed.

    Builtin open(..., "r+") is rejected on Linux and macOS: a terminal is not
    seekable, and Python's text wrapper tries to seek. os.open plus FileIO
    is the mode the standard library uses for /dev/tty.
    """
    prompt = f"{label}: "
    try:
        if os.name == "nt":
            with (
                open("CONOUT$", "w", encoding="utf-8") as output,
                open("CONIN$", encoding="utf-8") as incoming,
            ):
                output.write(prompt)
                output.flush()
                return incoming.readline()
        return _read_posix_tty("/dev/tty", prompt)
    except OSError as exc:
        raise ValidationError(
            "A terminal is required for the tenant URL and client ID when --secret-stdin is set"
        ) from exc


def _read_posix_tty(path: str, prompt: str) -> str:
    """Read one line from a terminal device. ``path`` is /dev/tty in production."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    stream = io.TextIOWrapper(
        io.FileIO(fd, "w+"),
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
        write_through=True,
    )
    try:
        stream.write(prompt)
        stream.flush()
        return stream.readline()
    finally:
        stream.close()


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _controlling_tty_available() -> bool:
    if os.name == "nt":
        try:
            with open("CONIN$", encoding="utf-8"):
                return True
        except OSError:
            return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _prompts_available(*, secret_on_stdin: bool) -> bool:
    """True when a human can be prompted. A piped secret must not use stdin for that."""
    if secret_on_stdin:
        return _controlling_tty_available()
    return _stdin_is_tty() or _controlling_tty_available()


def _noninteractive_login_error(*, url: bool, client: bool, secret: bool) -> ValidationError:
    flags: list[str] = []
    if url:
        flags.append("--tenant-url")
    if client:
        flags.append("--client-id")
    if secret:
        flags.append("--secret-stdin")
    if len(flags) == 1:
        listed = flags[0]
    elif len(flags) == 2:
        listed = f"{flags[0]} and {flags[1]}"
    else:
        listed = ", ".join(flags[:-1]) + ", and " + flags[-1]
    return ValidationError(
        f"No terminal is available to prompt for login. Pass {listed}.",
        details={"missing_flags": flags},
    )


def _read_secret(secret_stdin: bool) -> str:
    """Read the client secret from stdin or a hidden prompt. Never from argv."""
    if secret_stdin:
        value = sys.stdin.read()
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if value.strip() == "":
            raise ValidationError("No client secret was received on stdin")
        return value
    try:
        secret = getpass.getpass("Client secret (not echoed): ")
    except EOFError as exc:
        raise ValidationError("Client secret is required") from exc
    if secret == "":
        raise ValidationError("Client secret is required")
    return secret


def _auth_failure(exc: BenchlingAgentError) -> NoReturn:
    _emit(Envelope.failure(exc))
    raise typer.Exit(int(exc.exit_code)) from None


def _require_env_name(name: str | None, store: EnvironmentConfigStore) -> str:
    names = store.configured_names() if store.environments_path.exists() else []
    if not name:
        raise ValidationError(
            "An environment is required; pass --env NAME",
            details={"available_envs": names},
        )
    if not ENV_NAME.match(name):
        raise invalid_environment_name(name, available=names)
    return name


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to save."),
    tenant_url: str | None = typer.Option(
        None,
        "--tenant-url",
        help="Tenant URL or bare host. https:// is added when omitted; a trailing /api/v2 or /api/v3 is removed.",
    ),
    client_id: str | None = typer.Option(None, "--client-id", help="App client ID."),
    secret_stdin: bool = typer.Option(
        False,
        "--secret-stdin",
        help="Read the client secret from stdin. It is never accepted as an argument.",
    ),
    protected: bool | None = typer.Option(
        None,
        "--protected/--unprotected",
        help=(
            "Mark the environment protected: changes apply then needs --confirm-env NAME. "
            "Omit to keep the saved setting (new environments are not protected)."
        ),
    ),
) -> None:
    """Save a verified client secret for one environment.

    With --tenant-url, --client-id, and --secret-stdin, login does not need a
    terminal. Otherwise the missing values are prompted when a terminal exists.
    The secret is never accepted as an argument. Nothing is saved unless a
    client-credentials token request succeeds.
    """
    state: State = ctx.obj
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env or state.env, store)
        url = _blank_to_none(tenant_url)
        client = _blank_to_none(client_id)
        fix_url, fix_client = login_fix_values(store, name, url, client)
        with bind_login_fix(env=name, tenant_url=fix_url, client_id=fix_client):
            Keychain().require()
            missing_url = url is None
            missing_client = client is None
            missing_secret = not secret_stdin
            if (missing_url or missing_client or missing_secret) and not _prompts_available(
                secret_on_stdin=secret_stdin
            ):
                raise _noninteractive_login_error(
                    url=missing_url, client=missing_client, secret=missing_secret
                )
            use_terminal = secret_stdin or not _stdin_is_tty()
            if missing_url:
                url = _prompt("Tenant URL", tty=use_terminal)
            if missing_client:
                client = _prompt("Client ID", tty=use_terminal)
            assert url is not None and client is not None
            build_environment(name, url, client)
            secret = _read_secret(secret_stdin)
            environment = login(
                store,
                Keychain(),
                name=name,
                tenant_url=url,
                client_id=client,
                secret=secret,
                protected=protected,
            )
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(
        Envelope.success(
            {
                "env": environment.name,
                "tenant_url": environment.base_url,
                "client_id": environment.client_id,
                "credential_source": "keyring",
                "protected": environment.protected,
                "saved": True,
            },
            env=environment.name,
            tenant_url=environment.base_url,
        )
    )


@auth_app.command("logout")
def auth_logout(
    ctx: typer.Context,
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to remove."),
) -> None:
    """Remove an environment from environments.json and delete its keychain secret."""
    state: State = ctx.obj
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env or state.env, store)
        logout(store, Keychain(), name)
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(Envelope.success({"env": name, "removed": True}, env=name))


def _set_protection(ctx: typer.Context, env: str | None, protected: bool) -> None:
    state: State = ctx.obj
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env or state.env, store)
        environment = store.set_protected(name, protected)
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(
        Envelope.success(
            {"env": environment.name, "protected": environment.protected},
            env=environment.name,
            tenant_url=environment.base_url,
        )
    )


@auth_app.command("protect")
def auth_protect(
    ctx: typer.Context,
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to protect."),
) -> None:
    """Mark a saved environment protected.

    `changes apply` then refuses it unless --env names it on the command line and
    --confirm-env repeats the name. Reads are unaffected. No secret is read.
    """
    _set_protection(ctx, env, True)


@auth_app.command("unprotect")
def auth_unprotect(
    ctx: typer.Context,
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to unprotect."),
) -> None:
    """Remove the protected mark from a saved environment."""
    _set_protection(ctx, env, False)


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    env: str | None = typer.Option(
        None, "--env", "-e", help="Limit the report to one environment."
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Request an access token and report the result. The token is not printed or saved.",
    ),
) -> None:
    """Show configured environments and whether a keychain secret is present.

    The secret is never printed. Without --env, every saved environment is listed.
    --check performs a live token request and does not print or save the token.
    """
    state: State = ctx.obj
    selected = env or state.env
    store = EnvironmentConfigStore()
    try:
        report = collect_status(store, Keychain(), name=selected, check=check)
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    meta: dict[str, Any] = {}
    if selected and selected in report.environments:
        meta = {
            "env": selected,
            "tenant_url": report.environments[selected]["tenant_url"],
        }
    payload = {"environments": report.environments}
    if report.failed_envs:
        messages = [
            str(report.environments[name]["token_check"]["message"]) for name in report.failed_envs
        ]
        message = (
            messages[0] if len(messages) == 1 else "Token check failed: " + "; ".join(messages)
        )
        _auth_failure(
            AuthenticationError(
                message,
                details={
                    "environments": payload["environments"],
                    "failed_envs": report.failed_envs,
                },
            )
        )
    _emit(Envelope.success(payload, **meta))


def _doctor_failure_message(result: dict[str, Any]) -> str:
    failed = [check for check in result.get("checks", []) if not check.get("ok")]
    if not failed:
        return "Doctor checks failed"
    summary = str(failed[0].get("error") or "Doctor checks failed")
    hint = str(failed[0].get("hint") or "")
    if hint and hint not in summary:
        return f"{summary}. {hint}"
    return summary


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check configuration, authentication and API connectivity for the environment."""
    state: State = ctx.obj
    try:
        result, runtime_env = asyncio.run(
            _run_with_runtime(state.env, lambda runtime: runtime.service.doctor())
        )
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:
        error = UpstreamError("Unexpected local failure", details={"type": type(exc).__name__})
        _emit(Envelope.failure(error))
        raise typer.Exit(int(ExitCode.UPSTREAM)) from None
    meta = {"env": runtime_env.name, "tenant_url": runtime_env.base_url}
    if isinstance(result, dict) and result.get("ok") is False:
        _emit(
            Envelope.failure(
                AuthenticationError(_doctor_failure_message(result), details=result),
                **meta,
            )
        )
        raise typer.Exit(int(ExitCode.AUTHENTICATION)) from None
    _emit(Envelope.success(result, **meta))


@entry_app.command("resolve")
def entry_resolve(ctx: typer.Context, reference: str) -> None:
    """Resolve an entry ID, display ID or URL to its canonical entry."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.resolve_entry(reference))


@entry_app.command("get")
def entry_get(
    ctx: typer.Context,
    reference: str,
    full: bool = typer.Option(False, help="Retrieve all available structured content."),
) -> None:
    """Fetch an entry snapshot; --full retrieves all available structured content."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_entry(reference, full=full))


@entry_app.command("bulk-get")
def entry_bulk_get(
    ctx: typer.Context,
    entry_ids: list[str] | None = typer.Option(
        None, "--id", help="Entry ID. Repeat to request multiple entries."
    ),
    display_ids: list[str] | None = typer.Option(
        None, "--display-id", help="Entry display ID. Repeat to request multiple entries."
    ),
) -> None:
    """Fetch multiple entries by ID or display ID."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.get_entries(
            entry_ids=entry_ids,
            display_ids=display_ids,
        ),
    )


@entry_app.command("inspect")
def entry_inspect(ctx: typer.Context, reference: str) -> None:
    """Summarize an entry's structure (sections, parts, tables, schemas) without full content."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.inspect_entry(reference))


@template_app.command("get")
def template_get(ctx: typer.Context, template_id: str) -> None:
    """Get an entry template and summarize its structure."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_template(template_id))


@template_app.command("list")
def template_list(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", help="Exact entry-template name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """List one page of entry templates."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_templates(name=name, limit=limit, cursor=cursor),
    )


@folder_app.command("get")
def folder_get(ctx: typer.Context, folder_id: str) -> None:
    """Get a folder by ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_folder(folder_id))


@entry_app.command("list")
def entry_list(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project", help="Project ID."),
    folder: str | None = typer.Option(None, "--folder", help="Folder ID. Uses the V2 entry list."),
    schema: str | None = typer.Option(None, "--schema", help="Entry schema ID."),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Exact entry name. A comma in the name uses the V2 name parameter.",
    ),
    modified_after: str | None = typer.Option(
        None,
        "--modified-after",
        help="Inclusive lower bound: YYYY-MM-DD or an RFC3339 timestamp with a time zone.",
    ),
    modified_before: str | None = typer.Option(
        None,
        "--modified-before",
        help="Inclusive upper bound: YYYY-MM-DD or an RFC3339 timestamp with a time zone.",
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """List entries. A folder filter is applied by V2; the other filters are server-side too."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_entries(
            project=project,
            folder=folder,
            schema_id=schema,
            name=name,
            modified_after=modified_after,
            modified_before=modified_before,
            limit=limit,
            cursor=cursor,
        ),
    )


@entry_fields_app.command("get")
def entry_fields_get(ctx: typer.Context, reference: str) -> None:
    """Get the schema field values of an entry."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_entry_fields(reference))


@section_app.command("get")
def section_get(ctx: typer.Context, reference: str, section: str) -> None:
    """Get one section of an entry."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_section(reference, section))


@part_app.command("get")
def part_get(ctx: typer.Context, reference: str, part_id: str) -> None:
    """Get one document part of an entry by part ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_part(reference, part_id))


@table_app.command("get")
def table_get(ctx: typer.Context, part_id: str) -> None:
    """Get an unstructured table by its part ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_table(part_id))


@schema_app.command("get")
def schema_get(ctx: typer.Context, schema_id: str) -> None:
    """Get an entry, entity, result, or run schema by ID, including dropdown options."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_schema(schema_id))


@schema_app.command("find")
def schema_find(
    ctx: typer.Context,
    name: str,
    kind: str | None = typer.Option(
        None, "--kind", help="entry, entity, result, or run. Omit to search all kinds."
    ),
) -> None:
    """Find a schema by exact, case-sensitive name."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.find_schema(name, kind=kind))


@schema_app.command("list")
def schema_list(
    ctx: typer.Context,
    kind: str | None = typer.Option(
        None, "--kind", help="entry, entity, result, or run. Omit to list all four."
    ),
    name: str | None = typer.Option(None, "--name", help="Exact, case-sensitive schema name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """List schemas, optionally matching an exact name, following upstream pages up to the limit."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_schemas(
            kind=kind, name=name, limit=limit, cursor=cursor
        ),
    )


@container_app.command("get")
def container_get(ctx: typer.Context, container_id: str) -> None:
    """Get an inventory container by ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_container(container_id))


@container_app.command("find")
def container_find(
    ctx: typer.Context,
    barcode: str | None = typer.Option(None, "--barcode", help="Exact container barcode."),
    name: str | None = typer.Option(None, "--name", help="Exact container name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """Find one page of containers by exact barcode or exact name."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.find_containers(
            barcode=barcode, name=name, limit=limit, cursor=cursor
        ),
    )


@container_schema_app.command("list")
def container_schema_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """List one page of container schemas and their field definitions."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_container_schemas(limit=limit, cursor=cursor),
    )


@results_app.command("list")
def results_list(
    ctx: typer.Context,
    entry: str | None = typer.Option(None),
    schema_id: str | None = typer.Option(None, "--schema"),
    page_size: int = typer.Option(100, min=1, max=100),
    cursor: str | None = typer.Option(None),
) -> None:
    """List result records, optionally filtered by entry, schema, or linked entity."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_results(
            entry=entry,
            schema_id=schema_id,
            page_size=page_size,
            cursor=cursor,
        ),
    )


@results_app.command("get")
def results_get(ctx: typer.Context, result_id: str) -> None:
    """Get one result record by ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_result(result_id))


@results_app.command("tables")
def results_tables(ctx: typer.Context, reference: str) -> None:
    """Discover explicit V2 result-table IDs and assigned schemas on one entry.

    Each table_id is a v2_structured_table id (strtbl_…). Export it with
    ``export --kind results-table --part TABLE_ID --entry ENTRY``.
    """
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.list_result_tables(reference))


@results_app.command("rows")
def results_rows(
    ctx: typer.Context,
    part_id: str,
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Page size, from 1 to 100."),
    cursor: str | None = typer.Option(None, "--cursor", help="next_token from the previous page."),
) -> None:
    """Read one page of rows from a results table.

    Calls GET /document-part-results-table/{id}/rows/items. Pass the previous
    next_token with --cursor.
    """
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_results_table_rows(
            part_id, limit=limit, cursor=cursor
        ),
    )


@app.command("export")
def export_data(
    ctx: typer.Context,
    kind: str = typer.Option(..., "--kind", help="table, results-table, or assay-results."),
    output: str = typer.Option(..., "--output", "-o", help="File path to write."),
    file_format: str = typer.Option(..., "--format", help="json or csv."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace the file if it already exists."
    ),
    part_id: str | None = typer.Option(
        None,
        "--part",
        help=(
            "Table id. A v3_document_part id (docpart_…) comes from `entry inspect`. "
            "A v2_structured_table id (strtbl_…) comes from `results tables ENTRY` "
            "and needs `--entry ENTRY` with `--kind results-table`."
        ),
    ),
    entry: str | None = typer.Option(
        None,
        "--entry",
        help=(
            "Entry ID, display ID, or URL. Assay-results filter. "
            "Also required with a strtbl_ id from `results tables`."
        ),
    ),
    schema_id: str | None = typer.Option(
        None, "--schema", help="Result schema ID. Assay results only."
    ),
) -> None:
    """Write a table or assay results to a file and print a summary only.

    Pages are read internally. The response is the row count, columns, path,
    and SHA-256 checksum, not the rows. An existing file is kept unless
    --overwrite is set. Benchling is only read.
    """
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: export_dataset(
            runtime.service,
            kind=kind,
            path=output,
            file_format=file_format,
            overwrite=overwrite,
            part_id=part_id,
            entry=entry,
            schema_id=schema_id,
        ),
    )


def _read_spec(path: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text("utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "Could not read change specification",
            details={"path": path, "reason": redact_text(str(exc))},
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Change specification must be a JSON object")
    return parsed


def _parse_column_maps(column_map: list[str]) -> dict[str, str]:
    """Turn repeated ``CSV_COLUMN=FIELD`` options into the planner's column map."""
    mapped: dict[str, str] = {}
    for item in column_map:
        column, separator, field = item.partition("=")
        if not separator or not column.strip() or not field.strip():
            raise ValidationError(
                "Each --map must be CSV_COLUMN=FIELD",
                details={"map": item},
            )
        key = column.strip()
        if key in mapped:
            raise ValidationError(
                "A CSV column was mapped more than once",
                details={"column": key},
            )
        mapped[key] = field.strip()
    return mapped


def _plan_request(
    family: PlanFamily,
    spec: str | None,
    csv_path: str | None,
    id_column: str | None,
    column_map: list[str],
) -> dict[str, Any]:
    """Choose the JSON spec or the CSV result-update flags."""
    if csv_path is not None and spec is not None:
        raise ValidationError("Pass either --spec or --csv, not both")
    if csv_path is not None:
        if family != PlanFamily.RESULTS:
            raise ValidationError("CSV result updates require the results family")
        return {
            "operation": "update",
            "csv_path": csv_path,
            "id_column": id_column,
            "column_map": _parse_column_maps(column_map),
        }
    if column_map or id_column is not None:
        raise ValidationError("--id-column and --map require --csv")
    if spec is None:
        raise ValidationError("A change plan needs --spec, or --csv with --id-column and --map")
    return _read_spec(spec)


@changes_app.command("plan")
def changes_plan(
    ctx: typer.Context,
    entry: str | None = typer.Option(
        None, "--entry", help="Entry ID, display ID, or URL. Omit for entry or container plans."
    ),
    family: PlanFamily = typer.Option(..., case_sensitive=False),
    spec: str | None = typer.Option(None, "--spec", help="JSON file path, or '-' for stdin."),
    csv_path: str | None = typer.Option(
        None,
        "--csv",
        help="CSV file of result updates. Use with --id-column and --map instead of --spec.",
    ),
    id_column: str | None = typer.Option(
        None,
        "--id-column",
        help="CSV column that holds the result id. Required with --csv.",
    ),
    column_map: list[str] = typer.Option(
        [],
        "--map",
        help="CSV column mapped to a field, as CSV_COLUMN=FIELD. Repeat for each field.",
    ),
) -> None:
    """Create a change plan; nothing is written until it is applied.

    ``--spec`` is a JSON change specification. Result updates can instead read a
    CSV file: ``--id-column`` names the result id, and each ``--map`` names a
    field by display name, system name, or field-definition id.
    """
    state: State = ctx.obj
    try:
        if family == PlanFamily.ENTRY and entry is not None:
            raise ValidationError("Entry creation plans must not specify --entry")
        if family not in (PlanFamily.ENTRY, PlanFamily.CONTAINERS) and entry is None:
            raise ValidationError("This change family requires --entry")
        request = _plan_request(family, spec, csv_path, id_column, column_map)
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    _execute(
        state.env,
        lambda runtime: runtime.planner.plan(entry, family, request),
    )


@changes_app.command("apply")
def changes_apply(
    ctx: typer.Context,
    plan_id: str,
    confirm_env: str | None = typer.Option(
        None,
        "--confirm-env",
        help="Repeat the --env name. Required to apply to a protected environment.",
    ),
) -> None:
    """Apply a previously created plan, re-checking its preconditions first.

    A protected environment is refused unless --env names it on the command line
    and --confirm-env repeats the same name.
    """
    state: State = ctx.obj
    try:
        # Refuse before the keychain or the network is touched.
        environment = EnvironmentConfigStore().resolve(state.env)
        check_apply_allowed(
            environment,
            env_on_command_line=state.env_on_command_line,
            confirm_env=confirm_env,
        )
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None

    async def call(runtime: Runtime) -> dict[str, Any]:
        check_apply_allowed(
            runtime.environment,
            env_on_command_line=state.env_on_command_line,
            confirm_env=confirm_env,
        )
        return await runtime.planner.apply(plan_id)

    _execute(state.env, call)


@changes_app.command("status")
def changes_status(
    ctx: typer.Context,
    plan_id: str,
    wait: bool = typer.Option(
        False, "--wait", help="Poll result tasks until completion or timeout."
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", min=0.1, max=3600, help="Maximum wait, in seconds (0.1-3600)."
    ),
) -> None:
    """Show a plan journal; optionally wait for submitted result tasks."""
    state: State = ctx.obj

    async def call(runtime: Runtime) -> dict[str, Any]:
        return await runtime.planner.status(plan_id, wait=wait, timeout=timeout)

    _execute(state.env, call)


def _issue_spec(
    spec: str | None,
    options: dict[str, Any],
    cite_urls: list[str],
    cite_quotes: list[str],
) -> dict[str, Any]:
    """Merge a JSON spec with flags. A flag overrides the same key in the spec."""
    merged: dict[str, Any] = _read_spec(spec) if spec is not None else {}
    for key, value in options.items():
        if value is not None:
            merged[key] = value
    if len(cite_urls) != len(cite_quotes):
        raise ValidationError(
            "Pass --cite-url and --cite-quote the same number of times, in matching order"
        )
    if cite_urls:
        merged["citations"] = [
            {"url": url, "quote": quote} for url, quote in zip(cite_urls, cite_quotes)
        ]
    return merged


def _file_with_gh(draft: dict[str, Any]) -> dict[str, Any]:
    """Create the issue with the GitHub CLI. Called only for an explicit --file."""
    import shutil
    import subprocess

    gh = shutil.which("gh")
    if gh is None:
        raise UnsupportedError(
            "The GitHub CLI (gh) is not installed. Open new_issue_url instead.",
            details={"new_issue_url": draft["new_issue_url"]},
        )
    command = [gh, "issue", "create", "--repo", REPOSITORY, "--title", draft["title"]]
    for label in draft["labels"]:
        command += ["--label", label]
    command += ["--body-file", "-"]
    completed = subprocess.run(
        command, input=draft["body"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise UpstreamError(
            "gh issue create failed; nothing was filed. Open new_issue_url instead.",
            details={
                "reason": redact_text(completed.stderr.strip())[-500:],
                "new_issue_url": draft["new_issue_url"],
            },
        )
    lines = completed.stdout.strip().splitlines()
    return {**draft, "filed": True, "issue_url": lines[-1] if lines else None, "next_step": None}


@issue_app.command("draft")
def issue_draft(
    spec: str | None = typer.Option(
        None, "--spec", help="JSON file with the draft fields, or '-' for stdin."
    ),
    kind: str | None = typer.Option(None, "--kind", help="bug or feature."),
    summary: str | None = typer.Option(None, "--summary", help="One-line issue title."),
    attempted: str | None = typer.Option(
        None, "--attempted", help="What the user asked the agent to do."
    ),
    expected: str | None = typer.Option(None, "--expected", help="What should have happened."),
    actual: str | None = typer.Option(None, "--actual", help="What the toolkit did instead."),
    interface: str | None = typer.Option(None, "--interface", help="cli or mcp."),
    command_or_tool: str | None = typer.Option(
        None, "--command", help="The benchling command or MCP tool that was used."
    ),
    description: str | None = typer.Option(
        None, "--description", help="Detailed description of the limitation."
    ),
    solution: str | None = typer.Option(None, "--solution", help="Proposed solution."),
    cite_url: list[str] = typer.Option(
        [],
        "--cite-url",
        help="Benchling docs URL that supports the solution. Repeat with --cite-quote.",
    ),
    cite_quote: list[str] = typer.Option(
        [], "--cite-quote", help="The quoted line from the matching --cite-url page."
    ),
    file: bool = typer.Option(
        False,
        "--file",
        help=(
            "File the issue on GitHub with `gh issue create`. Use only after the user "
            "said yes to this exact draft."
        ),
    ),
) -> None:
    """Build a ready-to-file issue for something the toolkit cannot do.

    Prints the title, body, labels, and a prefilled GitHub new-issue URL. Nothing
    is sent unless --file is passed. The proposed solution must cite official
    Benchling docs, and drafts that look like they hold tenant URLs, record IDs,
    secrets, or email addresses are refused. Benchling is not contacted.
    """
    options = {
        "kind": kind,
        "summary": summary,
        "attempted_action": attempted,
        "expected": expected,
        "actual": actual,
        "interface": interface,
        "command_or_tool": command_or_tool,
        "description": description,
        "proposed_solution": solution,
    }
    try:
        draft = build_issue_draft(_issue_spec(spec, options, cite_url, cite_quote))
        if file:
            draft = _file_with_gh(draft)
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    _emit(Envelope.success(draft, repository=REPOSITORY))


def _write_stderr_envelope(envelope: Envelope[Any]) -> None:
    """Startup failures belong on stderr. Stdout is the MCP stdio protocol."""
    sys.stderr.write(_json(envelope) + "\n")
    sys.stderr.flush()


@mcp_app.command("serve")
def mcp_serve(
    allow_writes: list[str] = typer.Option(
        [],
        "--allow-writes",
        metavar="ENV",
        help="Allow apply_changes in this environment; repeatable.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Log MCP requests to stderr. Quiet by default so stdio clients stay readable.",
    ),
) -> None:
    """Run the MCP server on stdio; every tool call selects its environment."""
    from .mcp_server import run_mcp_server
    from .redact import redact_text

    try:
        run_mcp_server(writable_envs=set(allow_writes), verbose=verbose)
    except BenchlingAgentError as exc:
        _write_stderr_envelope(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:
        error = UpstreamError(
            "MCP server failed to start",
            details={"type": type(exc).__name__, "reason": redact_text(str(exc))},
        )
        _write_stderr_envelope(Envelope.failure(error))
        raise typer.Exit(int(error.exit_code)) from None


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
