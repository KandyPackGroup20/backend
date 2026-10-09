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
    policy_id: Literal["kandypack-roster"] = "kandypack-roster"
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
    policy_id: Literal["kandypack-roster"] = "kandypack-roster"
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
    capacity: str | None = None
    capacity_unit: Literal["KG"] | None = None


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
    staff_name: str | None = None
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
    """One accepted assignment; historical policy/rejection fields are unavailable."""

    audit_id: PositiveInt
    actor_id: PositiveInt
    actor_name: str | None
    route_name: str | None = None
    station_id: int | None = None
    station_name: str | None = None
    plate_number: str | None = None
    driver_name: str | None = None
    assistant_name: str | None = None
    attempted_route_id: PositiveInt
    attempted_truck_id: PositiveInt
    attempted_driver_id: PositiveInt
    attempted_assistant_id: PositiveInt
    attempted_start_time: AwareDatetime
    attempted_end_time: AwareDatetime
    attempted_duration_seconds: PositiveInt
    outcome: Literal["ACCEPTED"]
    reason_code: None = None
    policy_id: None = None
    request_key: str = Field(min_length=1, max_length=128)
    assignment_id: PositiveInt
    occurred_at: AwareDatetime
    legacy: Literal[False] = False


class AuditResponse(FrozenModel):
    attempts: tuple[AuditAttempt, ...]
    meta: MySQLWriteMeta = MySQLWriteMeta()


class CargoSelection(FrozenModel):
    order_ids: tuple[PositiveInt, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_orders(self):
        if len(set(self.order_ids)) != len(self.order_ids):
            raise ValueError("Select each whole order only once.")
        return self


class StationStore(FrozenModel):
    station_id: PositiveInt
    station_name: str
    address: str


class StoresResponse(FrozenModel):
    stores: tuple[StationStore, ...]
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"


class CargoItem(FrozenModel):
    order_id: PositiveInt
    order_item_id: PositiveInt
    product_id: PositiveInt
    product_name: str
    ordered_quantity: int
    allocated_quantity: int
    received_quantity: int
    wrong_destination: int
    unit_weight_kg: str | None


class CargoOrder(FrozenModel):
    order_id: PositiveInt
    delivery_date: date
    order_status: str
    route_id: PositiveInt
    station_id: PositiveInt
    station_name: str
    route_name: str
    delivery_address: str
    recipient_name: str
    recipient_phone: str
    customer_name: str
    assigned_roster_id: int | None
    delivery_id: int | None
    assigned_weight_kg: str | None
    weight_kg: str
    eligible: bool
    blocked_reasons: tuple[str, ...]
    items: tuple[CargoItem, ...]


class DemandResponse(FrozenModel):
    orders: tuple[CargoOrder, ...]
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"


class TruckSchedule(FrozenModel):
    roster_id: PositiveInt
    route_id: PositiveInt
    station_id: PositiveInt
    station_name: str
    route_name: str
    truck_id: PositiveInt
    plate_number: str
    capacity: str
    capacity_unit: Literal["KG"] | None
    is_active: bool
    driver_id: PositiveInt
    driver_name: str
    assistant_id: PositiveInt
    assistant_name: str
    start_time: AwareDatetime
    end_time: AwareDatetime
    status: str
    order_count: int
    unit_count: int
    cargo_weight_kg: str


class SchedulesResponse(FrozenModel):
    schedules: tuple[TruckSchedule, ...]
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"


class LoadingListResponse(DemandResponse):
    schedule: TruckSchedule


class CargoAssignedResponse(FrozenModel):
    status: Literal["SUCCESS"] = "SUCCESS"
    result_code: Literal["ORDERS_ASSIGNED", "ORDERS_ALREADY_ASSIGNED"]
    roster_id: PositiveInt
    order_ids: tuple[PositiveInt, ...]
    cargo_weight_kg: str
    timezone: Literal["Asia/Colombo"] = "Asia/Colombo"
