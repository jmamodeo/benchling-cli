from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .errors import BenchlingAgentError

T = TypeVar("T")


class ErrorDetail(BaseModel):
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
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(
        cls, error: BenchlingAgentError, **meta: Any
    ) -> Envelope[dict[str, Any]]:
        return cast(Envelope[dict[str, Any]], cls(
            ok=False,
            error=ErrorDetail(
                code=error.code,
                message=error.message,
                retryable=error.retryable,
                details=error.details,
            ),
            meta=meta,
        ))


class AuthMethod(str, Enum):
    CLIENT_CREDENTIALS = "client_credentials"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    tenant_url: HttpUrl
    auth_method: AuthMethod
    client_id: str | None = None
    early_access: bool = True

    @field_validator("tenant_url")
    @classmethod
    def strip_url_path(cls, value: HttpUrl) -> HttpUrl:
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
        return value.strip() if value else None

    @property
    def base_url(self) -> str:
        return str(self.tenant_url).rstrip("/")


class TokenBundle(BaseModel):
    access_token: str
    expires_at: datetime
    token_type: str = "Bearer"

    def usable(self, skew_seconds: int = 60) -> bool:
        now = datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return (expires - now).total_seconds() > skew_seconds


class CompletenessIssue(BaseModel):
    scope: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class EntrySnapshot(BaseModel):
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
    DOCUMENT = "document"
    TABLE = "table"
    FIELDS = "fields"
    RESULTS = "results"


class PlanStatus(str, Enum):
    PENDING = "pending"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ChangePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str
    created_at: datetime
    profile: str
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
    at: datetime
    stage: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ChangeJournal(BaseModel):
    plan_id: str
    status: PlanStatus = PlanStatus.PENDING
    events: list[JournalEvent] = Field(default_factory=list)
    result: dict[str, Any] | None = None
