"""Small data-access boundary independent of SQL, HTTP, and business policy."""

from datetime import date, datetime
from typing import Protocol

from dataclasses import dataclass

from app.roster.policy import AssignmentProposal
from app.roster.schemas import (
    Assignment, AssignmentsResponse, AuditResponse, CandidatesResponse, HoursResponse,
)


class RosterDataError(Exception):
    """Sanitized storage failure that may be exposed through the roster API."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class RosterWindowError(ValueError):
    """An input window cannot be represented by the selected storage adapter."""


class RosterIdempotencyConflict(Exception):
    """A committed key belongs to a different request; never expose its facts."""

    error_code = "IDEMPOTENCY_KEY_CONFLICT"
    message = "Idempotency-Key was already used for a different assignment request."


class RosterBusinessRejection(Exception):
    """A transient policy rejection that made no business or audit change."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class AssignmentOperationResult:
    assignment: Assignment
    replayed: bool = False


class RosterRepository(Protocol):
    """Roster reads and the single atomic assignment operation."""

    def candidates(self) -> CandidatesResponse: ...

    def assignments(self, start: datetime, end: datetime) -> AssignmentsResponse: ...

    def assign(
        self, proposal: AssignmentProposal, *, actor_id: int, request_key: str,
    ) -> AssignmentOperationResult: ...


class RosterReportingRepository(Protocol):
    """Persistent selected-week and accepted-assignment audit reads."""

    def hours(self, week_start: date) -> HoursResponse: ...

    def audit(self, limit: int) -> AuditResponse: ...
