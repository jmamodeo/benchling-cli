from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    VALIDATION = 2
    AUTHENTICATION = 3
    ACCESS_OR_NOT_FOUND = 4
    CONFLICT = 5
    UNSUPPORTED = 6
    UPSTREAM = 7
    PARTIAL_OR_UNKNOWN = 8


class BenchlingAgentError(Exception):
    code = "benchling_error"
    exit_code = ExitCode.UPSTREAM

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = retryable


class ValidationError(BenchlingAgentError):
    code = "validation_error"
    exit_code = ExitCode.VALIDATION


class AuthenticationError(BenchlingAgentError):
    code = "authentication_error"
    exit_code = ExitCode.AUTHENTICATION


class AccessError(BenchlingAgentError):
    code = "access_denied"
    exit_code = ExitCode.ACCESS_OR_NOT_FOUND


class NotFoundError(BenchlingAgentError):
    code = "not_found"
    exit_code = ExitCode.ACCESS_OR_NOT_FOUND


class ConflictError(BenchlingAgentError):
    code = "conflict"
    exit_code = ExitCode.CONFLICT


class UnsupportedError(BenchlingAgentError):
    code = "unsupported_capability"
    exit_code = ExitCode.UNSUPPORTED


class UpstreamError(BenchlingAgentError):
    code = "upstream_error"
    exit_code = ExitCode.UPSTREAM


class PartialCompletionError(BenchlingAgentError):
    code = "partial_or_unknown_completion"
    exit_code = ExitCode.PARTIAL_OR_UNKNOWN

