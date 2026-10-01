"""Explicit development fixtures, unrelated to real authentication identities."""

from app.roster.schemas import Assignment, CandidatesResponse, RosterMeta, Route, Staff, Truck, parse_instant


def make_fixtures() -> tuple[CandidatesResponse, tuple[Assignment, ...]]:
    catalog = CandidatesResponse(
        routes=(
            Route(route_id=1, station_id="CMB", route_name="Colombo city deliveries", max_duration_seconds=14400),
            Route(route_id=2, station_id="CMB", route_name="Nugegoda deliveries", max_duration_seconds=21600),
        ),
        trucks=(
            Truck(truck_id=1, station_id="CMB", plate_number="DEMO-TRUCK-01", is_active=True),
            Truck(truck_id=2, station_id="CMB", plate_number="DEMO-TRUCK-02", is_active=True),
        ),
        drivers=(
            Staff(staff_id=1, person_id=101, name="Demo driver A", staff_type="DRIVER"),
            Staff(staff_id=2, person_id=102, name="Demo driver B", staff_type="DRIVER"),
        ),
        assistants=(
            Staff(staff_id=3, person_id=103, name="Demo assistant A", staff_type="ASSISTANT"),
            Staff(staff_id=4, person_id=104, name="Demo assistant B", staff_type="ASSISTANT"),
        ),
        meta=RosterMeta(),
    )
    assignments = (
        Assignment(
            roster_id=1, route_id=1, truck_id=1, driver_id=1, assistant_id=3,
            dispatcher_id=9001, start_time=parse_instant("2026-09-14T08:00:00+05:30"),
            end_time=parse_instant("2026-09-14T10:00:00+05:30"), duration_seconds=7200,
            status="SCHEDULED", created_at=parse_instant("2026-09-13T12:00:00+05:30"),
        ),
        Assignment(
            roster_id=2, route_id=2, truck_id=2, driver_id=2, assistant_id=4,
            dispatcher_id=9001, start_time=parse_instant("2026-09-15T09:00:00+05:30"),
            end_time=parse_instant("2026-09-15T12:00:00+05:30"), duration_seconds=10800,
            status="SCHEDULED", created_at=parse_instant("2026-09-13T12:00:00+05:30"),
        ),
        Assignment(
            roster_id=3, route_id=1, truck_id=1, driver_id=1, assistant_id=3,
            dispatcher_id=9001, start_time=parse_instant("2026-09-18T08:00:00+05:30"),
            end_time=parse_instant("2026-09-18T10:00:00+05:30"), duration_seconds=7200,
            status="SCHEDULED", created_at=parse_instant("2026-09-13T12:00:00+05:30"),
        ),
    )
    return catalog, assignments
