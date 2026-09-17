"""Small data-access boundary independent of SQL, HTTP, and business policy."""

from datetime import datetime
from typing import Protocol

from dataclasses import dataclass

from app.roster.policy import AssignmentProposal
from app.roster.schemas import Assignment, AssignmentsResponse, CandidatesResponse


class RosterDataError(Exception):
    """Sanitized storage failure that may be exposed through the roster API."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class RosterWindowError(ValueError):
    """An input window cannot be represented by the selected storage adapter."""


class RosterBusinessRejection(Exception):
    """A policy decision that was durably audited and made no business change."""

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
