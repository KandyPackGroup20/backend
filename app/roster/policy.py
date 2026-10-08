"""Apply the provisional demo roster rules.

This module contains the calculations used to check roster assignments.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.roster.schemas import Assignment, Route, Staff, Truck


DRIVER_WEEKLY_LIMIT_SECONDS = 40 * 60 * 60
ASSISTANT_WEEKLY_LIMIT_SECONDS = 60 * 60 * 60


class RosterPolicyViolation(ValueError):
    """A stable, storage-independent reason why a proposal is not admissible."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AssignmentProposal:
    route_id: int
    truck_id: int
    driver_id: int
    assistant_id: int
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class PolicyStaff:
    """A database staff fact, including roles outside roster eligibility."""

    staff_id: int
    person_id: int
    staff_type: str
    is_active: bool = True


@dataclass(frozen=True)
class PolicyRoster:
    """The information needed to make a consistent roster policy decision.

    ``staff`` contains staff records used to check assignment rules. Include
    known staff records even if they are not currently available for assignment,
    so the rules can detect conflicts involving past records. If a staff record
    is missing, its ID is still used for comparisons.
    """

    routes: tuple[Route, ...]
    trucks: tuple[Truck, ...]
    staff: tuple[Staff | PolicyStaff, ...]
    assignments: tuple[Assignment, ...]


@dataclass(frozen=True)
class PolicyValidationResult:
    duration_seconds: int


class RosterPolicy(Protocol):
    policy_id: str

    def validate(self, roster: PolicyRoster, proposal: AssignmentProposal) -> PolicyValidationResult: ...


def _violation(code: str, message: str) -> None:
    raise RosterPolicyViolation(code, message)


def _colombo() -> ZoneInfo:
    try:
        return ZoneInfo("Asia/Colombo")
    except ZoneInfoNotFoundError as exc:
        raise RosterPolicyViolation(
            "POLICY_TIMEZONE_UNAVAILABLE",
            "Asia/Colombo timezone data is unavailable for roster policy evaluation.",
        ) from exc


def _instant(value: datetime, *, code: str = "INVALID_ASSIGNMENT_INTERVAL") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _violation(code, "Roster timestamps require an explicit timezone offset.")
    
    if value.microsecond:
        _violation(code, "Roster timestamps require whole-second precision.")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise RosterPolicyViolation(code, "Roster timestamp is outside the supported range.") from exc


def _interval(start: datetime, end: datetime, *, code: str = "INVALID_ASSIGNMENT_INTERVAL") -> tuple[datetime, datetime]:
    start_utc, end_utc = _instant(start, code=code), _instant(end, code=code)
    if end_utc <= start_utc:
        _violation(code, "Roster end_time must be later than start_time.")
    return start_utc, end_utc


def split_colombo_duration(start: datetime, end: datetime) -> dict[date, int]:
    """Allocate an aware, whole-second interval to Monday Colombo weeks exactly."""
    start_utc, end_utc = _interval(start, end)
    colombo = _colombo()
    allocations: dict[date, int] = {}
    cursor = start_utc
    while cursor < end_utc:
        local = cursor.astimezone(colombo)
        monday = local.date() - timedelta(days=local.weekday())
        following_monday = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=colombo).astimezone(timezone.utc)
        segment_end = min(end_utc, following_monday)
        seconds = int((segment_end - cursor).total_seconds())
        # Whole-second inputs make this exact; defensive check prevents a loop
        # if a timezone rule or invalid datetime fails to advance the boundary.
        if seconds <= 0:
            _violation("INVALID_ASSIGNMENT_INTERVAL", "Roster interval cannot be allocated to a Colombo week.")
        allocations[monday] = allocations.get(monday, 0) + seconds
        cursor = segment_end
    return allocations


def _person_key(staff_id: int, staff_by_id: dict[int, Staff | PolicyStaff]) -> tuple[str, int]:
    staff = staff_by_id.get(staff_id)
    return ("person", staff.person_id) if staff else ("staff", staff_id)


def _active(assignments: Iterable[Assignment]) -> tuple[Assignment, ...]:
    return tuple(row for row in assignments if row.status != "CANCELLED")


class DemoV1RosterPolicy:
    """The documented provisional touching-chain and Colombo-week rules."""

    policy_id = "kandypack-roster"

    def validate(self, roster: PolicyRoster, proposal: AssignmentProposal) -> PolicyValidationResult:
        route_by_id = {route.route_id: route for route in roster.routes}
        truck_by_id = {truck.truck_id: truck for truck in roster.trucks}
        staff_by_id = {staff.staff_id: staff for staff in roster.staff}

        route = route_by_id.get(proposal.route_id)
        if route is None:
            _violation("ROUTE_NOT_FOUND", "The selected route does not exist.")
        truck = truck_by_id.get(proposal.truck_id)
        if truck is None:
            _violation("TRUCK_NOT_FOUND", "The selected truck does not exist.")
        if not truck.is_active:
            _violation("TRUCK_INACTIVE", "The selected truck is inactive.")
        driver = staff_by_id.get(proposal.driver_id)
        if driver is None:
            _violation("DRIVER_NOT_FOUND", "The selected driver does not exist.")
        if driver.staff_type != "DRIVER":
            _violation("DRIVER_STAFF_TYPE_INVALID", "The selected driver is not a DRIVER.")
        if not getattr(driver, "is_active", True):
            _violation("DRIVER_INACTIVE", "The selected driver is inactive.")
        assistant = staff_by_id.get(proposal.assistant_id)
        if assistant is None:
            _violation("ASSISTANT_NOT_FOUND", "The selected assistant does not exist.")
        if assistant.staff_type != "ASSISTANT":
            _violation("ASSISTANT_STAFF_TYPE_INVALID", "The selected assistant is not an ASSISTANT.")
        if not getattr(assistant, "is_active", True):
            _violation("ASSISTANT_INACTIVE", "The selected assistant is inactive.")
        if driver.person_id == assistant.person_id:
            _violation("DRIVER_ASSISTANT_SAME_PERSON", "Driver and assistant must be different people.")

        start_utc, end_utc = _interval(proposal.start_time, proposal.end_time)
        duration_seconds = int((end_utc - start_utc).total_seconds())
        if duration_seconds > route.max_duration_seconds:
            _violation("ROUTE_MAX_DURATION_EXCEEDED", "Scheduled duration exceeds the route maximum.")

        active_history = _active(roster.assignments)
        self._check_overlaps(active_history, proposal, start_utc, end_utc, staff_by_id)
        self._check_consecutive(active_history, proposal, staff_by_id)
        self._check_weekly_caps(active_history, proposal, staff_by_id)
        return PolicyValidationResult(duration_seconds=duration_seconds)

    @staticmethod
    def _check_overlaps(
        history: tuple[Assignment, ...], proposal: AssignmentProposal, start_utc: datetime, end_utc: datetime,
        staff_by_id: dict[int, Staff | PolicyStaff],
    ) -> None:
        driver_person = _person_key(proposal.driver_id, staff_by_id)
        assistant_person = _person_key(proposal.assistant_id, staff_by_id)
        for assignment in history:
            existing_start, existing_end = _interval(
                assignment.start_time, assignment.end_time, code="INVALID_HISTORY_INTERVAL",
            )
            if not (existing_start < end_utc and existing_end > start_utc):
                continue
            if assignment.truck_id == proposal.truck_id:
                _violation("TRUCK_OVERLAP", "Truck is already assigned during this interval.")
            assigned_people = {
                _person_key(assignment.driver_id, staff_by_id),
                _person_key(assignment.assistant_id, staff_by_id),
            }
            if driver_person in assigned_people:
                _violation("DRIVER_OVERLAP", "Driver is already assigned during this interval.")
            if assistant_person in assigned_people:
                _violation("ASSISTANT_OVERLAP", "Assistant is already assigned during this interval.")

    def _check_consecutive(
        self, history: tuple[Assignment, ...], proposal: AssignmentProposal,
        staff_by_id: dict[int, Staff | PolicyStaff],
    ) -> None:
        self._check_chain(history, proposal, staff_by_id, role="driver", limit=1)
        self._check_chain(history, proposal, staff_by_id, role="assistant", limit=2)

    @staticmethod
    def _check_chain(
        history: tuple[Assignment, ...], proposal: AssignmentProposal,
        staff_by_id: dict[int, Staff | PolicyStaff],
        *, role: str, limit: int,
    ) -> None:
        proposal_staff_id = proposal.driver_id if role == "driver" else proposal.assistant_id
        target_person = _person_key(proposal_staff_id, staff_by_id)
        entries: list[tuple[datetime, datetime, bool]] = []
        for assignment in history:
            staff_id = assignment.driver_id if role == "driver" else assignment.assistant_id
            if _person_key(staff_id, staff_by_id) == target_person:
                start, end = _interval(assignment.start_time, assignment.end_time, code="INVALID_HISTORY_INTERVAL")
                entries.append((start, end, False))
        start, end = _interval(proposal.start_time, proposal.end_time)
        entries.append((start, end, True))
        entries.sort(key=lambda item: (item[0], item[1], not item[2]))

        chain: list[tuple[datetime, datetime, bool]] = []
        for entry in entries:
            if chain and chain[-1][1] == entry[0]:
                chain.append(entry)
            else:
                chain = [entry]
            if any(item[2] for item in chain) and len(chain) > limit:
                if role == "driver":
                    _violation("DRIVER_CONSECUTIVE_LIMIT", "A driver cannot perform consecutive deliveries.")
                _violation("ASSISTANT_CONSECUTIVE_LIMIT", "An assistant can perform at most two consecutive routes.")

    @staticmethod
    def _check_weekly_caps(
        history: tuple[Assignment, ...], proposal: AssignmentProposal,
        staff_by_id: dict[int, Staff | PolicyStaff],
    ) -> None:
        DemoV1RosterPolicy._check_role_weekly_cap(
            history, proposal, staff_by_id, role="driver", limit=DRIVER_WEEKLY_LIMIT_SECONDS,
        )
        DemoV1RosterPolicy._check_role_weekly_cap(
            history, proposal, staff_by_id, role="assistant", limit=ASSISTANT_WEEKLY_LIMIT_SECONDS,
        )

    @staticmethod
    def _check_role_weekly_cap(
        history: tuple[Assignment, ...], proposal: AssignmentProposal,
        staff_by_id: dict[int, Staff | PolicyStaff],
        *, role: str, limit: int,
    ) -> None:
        proposal_staff_id = proposal.driver_id if role == "driver" else proposal.assistant_id
        target_person = _person_key(proposal_staff_id, staff_by_id)
        weekly_seconds = split_colombo_duration(proposal.start_time, proposal.end_time)
        for assignment in history:
            staff_id = assignment.driver_id if role == "driver" else assignment.assistant_id
            if _person_key(staff_id, staff_by_id) != target_person:
                continue
            for week, seconds in split_colombo_duration(assignment.start_time, assignment.end_time).items():
                if week in weekly_seconds:
                    weekly_seconds[week] += seconds
        if any(seconds > limit for seconds in weekly_seconds.values()):
            if role == "driver":
                _violation("DRIVER_WEEKLY_CAP_EXCEEDED", "Driver scheduled time exceeds the 40-hour weekly limit.")
            _violation("ASSISTANT_WEEKLY_CAP_EXCEEDED", "Assistant scheduled time exceeds the 60-hour weekly limit.")
