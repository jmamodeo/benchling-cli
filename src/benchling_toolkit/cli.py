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
from .credentials import build_environment, collect_status, login, logout
from .environment import ENV_NAME, EnvironmentConfigStore, invalid_environment_name
from .errors import (
    AuthenticationError,
    BenchlingAgentError,
    ExitCode,
    UpstreamError,
    ValidationError,
)
from .keychain import Keychain
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
entry_app = typer.Typer(help="Resolve, fetch, and inspect entries.")
entry_fields_app = typer.Typer(help="Read entry schema fields.")
section_app = typer.Typer(help="Read entry sections.")
part_app = typer.Typer(help="Read document parts.")
table_app = typer.Typer(help="Read unstructured tables.")
schema_app = typer.Typer(help="Read entry and result schemas.")
results_app = typer.Typer(help="Read and submit result records.")
changes_app = typer.Typer(help="Preview and explicitly apply safe changes.")
mcp_app = typer.Typer(help="Run the local MCP server.")

app.add_typer(auth_app, name="auth")
app.add_typer(entry_app, name="entry")
entry_app.add_typer(entry_fields_app, name="fields")
app.add_typer(section_app, name="section")
app.add_typer(part_app, name="part")
app.add_typer(table_app, name="table")
app.add_typer(schema_app, name="schema")
app.add_typer(results_app, name="results")
app.add_typer(changes_app, name="changes")
app.add_typer(mcp_app, name="mcp")


class State:
    """Per-invocation state stored on the Typer context; env is the --env selection."""

    def __init__(self, env: str | None) -> None:
        self.env = env


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
    ctx.obj = State(env)


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
        Keychain().require()
        url = _blank_to_none(tenant_url)
        client = _blank_to_none(client_id)
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


@entry_app.command("inspect")
def entry_inspect(ctx: typer.Context, reference: str) -> None:
    """Summarize an entry's structure (sections, parts, tables, schemas) without full content."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.inspect_entry(reference))


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
    """Get an entry or result schema by ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_schema(schema_id))


@results_app.command("list")
def results_list(
    ctx: typer.Context,
    entry: str | None = typer.Option(None),
    schema_id: str | None = typer.Option(None, "--schema"),
    page_size: int = typer.Option(100, min=1, max=100),
    cursor: str | None = typer.Option(None),
) -> None:
    """List result records, optionally filtered by entry or schema, one page at a time."""
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_results(
            entry=entry, schema_id=schema_id, page_size=page_size, cursor=cursor
        ),
    )


@results_app.command("get")
def results_get(ctx: typer.Context, result_id: str) -> None:
    """Get one result record by ID."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_result(result_id))


@results_app.command("tables")
def results_tables(ctx: typer.Context, reference: str) -> None:
    """Discover explicit V2 result-table IDs and assigned schemas on one entry."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.list_result_tables(reference))


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


@changes_app.command("plan")
def changes_plan(
    ctx: typer.Context,
    entry: str = typer.Option(..., help="Entry ID, display ID, or URL."),
    family: PlanFamily = typer.Option(..., case_sensitive=False),
    spec: str = typer.Option(..., help="JSON file path, or '-' for stdin."),
) -> None:
    """Create a change plan from a JSON spec; nothing is written until it is applied."""
    state: State = ctx.obj
    try:
        request = _read_spec(spec)
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    _execute(
        state.env,
        lambda runtime: runtime.planner.plan(entry, family, request),
    )


@changes_app.command("apply")
def changes_apply(ctx: typer.Context, plan_id: str) -> None:
    """Apply a previously created plan, re-checking its preconditions first."""
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.planner.apply(plan_id))


@changes_app.command("status")
def changes_status(ctx: typer.Context, plan_id: str) -> None:
    """Show the journal and status of a plan."""
    state: State = ctx.obj

    async def call(runtime: Runtime) -> dict[str, Any]:
        return await runtime.planner.status(plan_id)

    _execute(state.env, call)


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
