from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import typer

from . import __version__
from .auth import AuthManager
from .environment import EnvironmentConfigStore, environment_secrets
from .errors import BenchlingAgentError, ExitCode, UpstreamError, ValidationError
from .models import Envelope, Environment, PlanFamily
from .runtime import Runtime

app = typer.Typer(
    name="benchling",
    help="Agent-friendly access to structured Benchling entry data.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(help="Inspect local authentication state.")
envs_app = typer.Typer(help="List configured environments.")
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
app.add_typer(envs_app, name="envs")
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
    def __init__(self, env: str | None) -> None:
        self.env = env


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"benchling-agent {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    env: str | None = typer.Option(
        None, "--env", "-e", help="Environment name; selects BENCHLING_<ENV>_* credentials."
    ),
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Agent-friendly access to structured Benchling entry data."""
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
        error = UpstreamError(
            "Unexpected local failure", details={"type": type(exc).__name__}
        )
        _emit(Envelope.failure(error))
        raise typer.Exit(int(ExitCode.UPSTREAM)) from None


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    state: State = ctx.obj
    try:
        config = EnvironmentConfigStore()
        environment = config.resolve(state.env)
        auth = AuthManager(environment_secrets())
        try:
            result = auth.status(environment)
        finally:
            auth.close()
        _emit(Envelope.success(result, env=environment.name, tenant_url=environment.base_url))
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None


@envs_app.command("list")
def envs_list() -> None:
    try:
        _emit(Envelope.success({"environments": EnvironmentConfigStore().metadata()}))
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None


@app.command()
def doctor(ctx: typer.Context) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.doctor())


@entry_app.command("resolve")
def entry_resolve(ctx: typer.Context, reference: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.resolve_entry(reference))


@entry_app.command("get")
def entry_get(
    ctx: typer.Context,
    reference: str,
    full: bool = typer.Option(False, help="Retrieve all available structured content."),
) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_entry(reference, full=full))


@entry_app.command("inspect")
def entry_inspect(ctx: typer.Context, reference: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.inspect_entry(reference))


@entry_fields_app.command("get")
def entry_fields_get(ctx: typer.Context, reference: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_entry_fields(reference))


@section_app.command("get")
def section_get(ctx: typer.Context, reference: str, section: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_section(reference, section))


@part_app.command("get")
def part_get(ctx: typer.Context, reference: str, part_id: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_part(reference, part_id))


@table_app.command("get")
def table_get(ctx: typer.Context, part_id: str) -> None:
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.service.get_table(part_id))


@schema_app.command("get")
def schema_get(ctx: typer.Context, schema_id: str) -> None:
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
    state: State = ctx.obj
    _execute(
        state.env,
        lambda runtime: runtime.service.list_results(
            entry=entry, schema_id=schema_id, page_size=page_size, cursor=cursor
        ),
    )


@results_app.command("get")
def results_get(ctx: typer.Context, result_id: str) -> None:
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
            details={"path": path, "reason": str(exc)},
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
    state: State = ctx.obj
    _execute(state.env, lambda runtime: runtime.planner.apply(plan_id))


@changes_app.command("status")
def changes_status(ctx: typer.Context, plan_id: str) -> None:
    state: State = ctx.obj

    async def call(runtime: Runtime) -> dict[str, Any]:
        return await runtime.planner.status(plan_id)

    _execute(state.env, call)


@mcp_app.command("serve")
def mcp_serve(
    allow_writes: list[str] = typer.Option(
        [], "--allow-writes", metavar="ENV",
        help="Allow apply_changes in this environment; repeatable.",
    ),
) -> None:
    """Run the MCP server on stdio; every tool call selects its environment."""
    from .mcp_server import run_mcp_server

    try:
        run_mcp_server(writable_envs=set(allow_writes))
    except BenchlingAgentError as exc:
        typer.echo(_json(Envelope.failure(exc)), err=True)
        raise typer.Exit(int(exc.exit_code)) from None


def main() -> None:
    app()


if __name__ == "__main__":
    main()
