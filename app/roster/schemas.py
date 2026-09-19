"""Immutable roster read and assignment-creation API value objects."""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, model_validator


def parse_instant(value: str) -> datetime:
    """Accept ISO timestamps with an explicit offset and whole-second precision."""
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Use an ISO timestamp with an explicit timezone offset.") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Timestamps require an explicit timezone offset.")
    if instant.microsecond:
        raise ValueError("Timestamps require whole-second precision.")
    try:
        normalized = instant.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Timestamp is outside the supported range.") from exc
    if normalized.microsecond:
        raise ValueError("Timezone offsets require whole-second precision.")
    return normalized


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RosterMeta(FrozenModel):
    data_source: Literal["dev-memory"] = "dev-memory"
    policy_id: Literal["demo-v1"] = "demo-v1"
    policy_confirmed: Literal[False] = False
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"
    volatile: Literal[True] = True
    fixture_week_start: date = date(2026, 9, 14)


class MySQLMeta(FrozenModel):
    data_source: Literal["mysql"] = "mysql"
    policy_id: Literal["pending-confirmation"] = "pending-confirmation"
    policy_confirmed: Literal[False] = False
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"
    volatile: Literal[False] = False
    fixture_week_start: None = None


class MySQLWriteMeta(FrozenModel):
    data_source: Literal["mysql"] = "mysql"
    policy_id: Literal["demo-v1"] = "demo-v1"
    policy_confirmed: Literal[False] = False
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"
    volatile: Literal[False] = False
    fixture_week_start: None = None


ReadMeta = Annotated[RosterMeta | MySQLMeta, Field(discriminator="data_source")]


class Route(FrozenModel):
    route_id: PositiveInt
    station_id: str = Field(min_length=1)
    route_name: str
    max_duration_seconds: PositiveInt


class Truck(FrozenModel):
    truck_id: PositiveInt
    station_id: str | None = Field(min_length=1)
    plate_number: str
    is_active: bool


class Staff(FrozenModel):
    staff_id: PositiveInt
    person_id: PositiveInt
    name: str
    staff_type: Literal["DRIVER", "ASSISTANT"]


class Assignment(FrozenModel):
    roster_id: PositiveInt
    route_id: PositiveInt
    truck_id: PositiveInt
    driver_id: PositiveInt
    assistant_id: PositiveInt
    dispatcher_id: PositiveInt
    start_time: AwareDatetime
    end_time: AwareDatetime
    duration_seconds: PositiveInt
    status: Literal["SCHEDULED", "IN_TRANSIT", "COMPLETED", "CANCELLED"]
    created_at: AwareDatetime


class CandidatesResponse(FrozenModel):
    routes: tuple[Route, ...]
    trucks: tuple[Truck, ...]
    drivers: tuple[Staff, ...]
    assistants: tuple[Staff, ...]
    meta: ReadMeta


class AssignmentsResponse(FrozenModel):
    assignments: tuple[Assignment, ...]
    meta: ReadMeta


class AssignmentRequest(FrozenModel):
    route_id: PositiveInt
    truck_id: PositiveInt
    driver_id: PositiveInt
    assistant_id: PositiveInt
    start_time: AwareDatetime
    end_time: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self):
        values = (self.start_time, self.end_time)
        if any(value.microsecond or value.utcoffset() is None for value in values):
            raise ValueError("Roster timestamps require explicit offsets and whole-second precision.")
        try:
            start_utc = self.start_time.astimezone(timezone.utc)
            end_utc = self.end_time.astimezone(timezone.utc)
            colombo_offset = timezone(timedelta(hours=5, minutes=30))
            local_values = tuple(value.astimezone(colombo_offset) for value in values)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Roster timestamp is outside the supported range.") from exc
        if any(value.year < 1000 for value in local_values):
            raise ValueError("Roster timestamp is outside the supported MySQL DATETIME range.")
        if end_utc <= start_utc:
            raise ValueError("end_time must be later than start_time.")
        return self


class AssignmentCreatedResponse(FrozenModel):
    status: Literal["SUCCESS"] = "SUCCESS"
    message: str = "Roster assignment created."
    result_code: Literal["ROSTER_ASSIGNED", "ROSTER_ASSIGNMENT_REPLAYED"]
    assignment: Assignment
    meta: MySQLWriteMeta = MySQLWriteMeta()


class StaffHours(FrozenModel):
    staff_id: PositiveInt
    staff_type: Literal["DRIVER", "ASSISTANT"]
    scheduled_seconds: int = Field(ge=0)
    limit_seconds: PositiveInt
    remaining_seconds: int


class HoursResponse(FrozenModel):
    week_start: date
    week_end: date
    hours: tuple[StaffHours, ...]
    meta: MySQLWriteMeta = MySQLWriteMeta()


class AuditAttempt(FrozenModel):
    audit_id: PositiveInt
    actor_id: PositiveInt
    actor_name: str | None
    attempted_route_id: PositiveInt | None
    attempted_truck_id: PositiveInt | None
    attempted_driver_id: PositiveInt | None
    attempted_assistant_id: PositiveInt | None
    attempted_start_time: AwareDatetime | None
    attempted_end_time: AwareDatetime | None
    attempted_duration_seconds: int | None = Field(default=None, ge=0)
    outcome: str = Field(min_length=1)
    reason_code: str | None
    policy_id: str | None
    request_key: str | None
    assignment_id: PositiveInt | None
    occurred_at: AwareDatetime | None
    legacy: bool


class AuditResponse(FrozenModel):
    attempts: tuple[AuditAttempt, ...]
    meta: MySQLWriteMeta = MySQLWriteMeta()
