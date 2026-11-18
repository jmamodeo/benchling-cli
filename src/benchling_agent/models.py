"""Pydantic models shared by the CLI, MCP server and planner: the JSON envelope,
environment config, entry snapshots and change-plan/journal records."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .errors import BenchlingAgentError

T = TypeVar("T")


class ErrorDetail(BaseModel):
    """Serializable form of a BenchlingAgentError carried in a failed Envelope."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel, Generic[T]):
    """Stable JSON contract returned by both interfaces."""

    schema_version: Literal["1.0"] = "1.0"
    ok: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, data: T, **meta: Any) -> Envelope[T]:
        """Build an ok=True envelope wrapping data; extra kwargs become meta."""
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error: BenchlingAgentError, **meta: Any) -> Envelope[dict[str, Any]]:
        """Build an ok=False envelope from an error; no data is included."""
        return cast(
            Envelope[dict[str, Any]],
            cls(
                ok=False,
                error=ErrorDetail(
                    code=error.code,
                    message=error.message,
                    retryable=error.retryable,
                    details=error.details,
                ),
                meta=meta,
            ),
        )


class AuthMethod(str, Enum):
    """Supported ways of obtaining an API token."""

    CLIENT_CREDENTIALS = "client_credentials"


class Environment(BaseModel):
    """A named tenant configuration; holds no secrets, only the tenant URL and auth method."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    tenant_url: HttpUrl
    auth_method: AuthMethod
    client_id: str | None = None
    early_access: bool = True

    @field_validator("tenant_url")
    @classmethod
    def strip_url_path(cls, value: HttpUrl) -> HttpUrl:
        """Require a bare https://host URL; reject credentials, paths, queries and fragments."""
        if value.scheme != "https":
            raise ValueError("tenant_url must use HTTPS")
        if value.username or value.password:
            raise ValueError("tenant_url must not contain credentials")
        if value.path not in {"", "/"} or value.query or value.fragment:
            raise ValueError("tenant_url must contain only scheme and host")
        return value

    @field_validator("client_id")
    @classmethod
    def strip_client_id(cls, value: str | None) -> str | None:
        """Trim whitespace; blank values become None."""
        return value.strip() if value else None

    @property
    def base_url(self) -> str:
        """Tenant URL without a trailing slash."""
        return str(self.tenant_url).rstrip("/")


class TokenBundle(BaseModel):
    """An access token with its expiry."""

    access_token: str
    expires_at: datetime
    token_type: str = "Bearer"

    def usable(self, skew_seconds: int = 60) -> bool:
        """True if the token stays valid for more than skew_seconds; naive expiries are treated as UTC."""
        now = datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return (expires - now).total_seconds() > skew_seconds


class CompletenessIssue(BaseModel):
    """A reason part of an entry snapshot could not be retrieved."""

    scope: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class EntrySnapshot(BaseModel):
    """Structured entry content pinned to one version_id.

    completeness is "partial" when issues lists anything that was not retrieved.
    """

    model_config = ConfigDict(populate_by_name=True)

    entry: dict[str, Any]
    version_id: str
    parts: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    tables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    entry_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    fields: dict[str, Any] | None = None
    result_tables: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    result_retrieval: dict[str, Any] = Field(default_factory=dict)
    completeness: Literal["complete", "partial"] = "complete"
    issues: list[CompletenessIssue] = Field(default_factory=list)


class PlanFamily(str, Enum):
    """Kinds of change a plan can describe; each has its own planner."""

    DOCUMENT = "document"
    TABLE = "table"
    FIELDS = "fields"
    RESULTS = "results"


class PlanStatus(str, Enum):
    """Lifecycle of a plan; UNKNOWN means the outcome of an apply could not be confirmed."""

    PENDING = "pending"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ChangePlan(BaseModel):
    """Immutable preview of a change: before/after/diff plus preconditions checked at apply time.

    checksum guards against tampering with a stored plan.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str
    created_at: datetime
    env: str
    tenant_url: str
    entry_id: str
    family: PlanFamily
    request: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    diff: dict[str, Any]
    preconditions: dict[str, Any]
    guarantees: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checksum: str


class JournalEvent(BaseModel):
    """One timestamped stage transition recorded while applying a plan."""

    at: datetime
    stage: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ChangeJournal(BaseModel):
    """Append-only apply history for a plan, with its overall status and final result."""

    plan_id: str
    status: PlanStatus = PlanStatus.PENDING
    events: list[JournalEvent] = Field(default_factory=list)
    result: dict[str, Any] | None = None
