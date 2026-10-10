"""Read-only eligibility using the same policy as the locked assignment write."""
from app.roster.policy import (
    AssignmentProposal, DemoV1RosterPolicy, PolicyRoster, PolicyStaff,
    RosterPolicyViolation, split_colombo_duration,
    DRIVER_WEEKLY_LIMIT_SECONDS, ASSISTANT_WEEKLY_LIMIT_SECONDS,
)
from app.roster.mysql_adapter import (
    _snapshot, _assignment, _colombo, ROUTES_SQL, TRUCKS_SQL,
    LOCK_STAFF_DIRECTORY_SQL, LOCK_HISTORY_SQL,
)
from app.roster.schemas import Route, Truck


def eligible_crew(roster, names, route_id, truck_id, start, end, driver_id=None, assistant_id=None):
    weeks = split_colombo_duration(start, end)
    policy = DemoV1RosterPolicy()
    drivers = [s for s in roster.staff if s.is_active and s.staff_type == 'DRIVER']
    assistants = [s for s in roster.staff if s.is_active and s.staff_type == 'ASSISTANT']
    result = dict(drivers=[], assistants=[], excluded=[], timezone='Asia/Colombo')
    for role, candidates, counterparts, selected in (
        ('DRIVER', drivers, assistants, assistant_id), ('ASSISTANT', assistants, drivers, driver_id),
    ):
        for staff in candidates:
            others = [s for s in counterparts if selected is None or s.staff_id == selected]
            rejection = 'No eligible counterpart is available for this interval.'
            for other in others:
                proposal = AssignmentProposal(route_id, truck_id,
                    staff.staff_id if role == 'DRIVER' else other.staff_id,
                    other.staff_id if role == 'DRIVER' else staff.staff_id, start, end)
                try:
                    policy.validate(roster, proposal)
                    break
                except RosterPolicyViolation as exc:
                    rejection = exc.message
            else:
                result['excluded'].append(dict(staff_id=staff.staff_id, message=rejection))
                continue
            limit = DRIVER_WEEKLY_LIMIT_SECONDS if role == 'DRIVER' else ASSISTANT_WEEKLY_LIMIT_SECONDS
            totals = dict.fromkeys(weeks, 0)
            person_ids = {s.staff_id for s in roster.staff if s.person_id == staff.person_id}
            for duty in roster.assignments:
                if duty.status == 'CANCELLED' or getattr(duty, 'driver_id' if role == 'DRIVER' else 'assistant_id') not in person_ids:
                    continue
                for week, seconds in split_colombo_duration(duty.start_time, duty.end_time).items():
                    if week in totals:
                        totals[week] += seconds
            result['drivers' if role == 'DRIVER' else 'assistants'].append(dict(
                staff_id=staff.staff_id, name=names[staff.staff_id], staff_type=role,
                weeks=[dict(week_start=week.isoformat(), scheduled_seconds=totals[week],
                    proposed_seconds=seconds, projected_seconds=totals[week]+seconds,
                    limit_seconds=limit, remaining_seconds=limit-totals[week]-seconds)
                    for week, seconds in weeks.items()],
            ))
    for staff in roster.staff:
        if not staff.is_active or staff.staff_type not in ('DRIVER', 'ASSISTANT'):
            result['excluded'].append(dict(staff_id=staff.staff_id, message='Staff member is inactive or has an incorrect staff type.'))
    return result


def availability(route_id, truck_id, start, end, driver_id=None, assistant_id=None):
    with _snapshot() as cursor:
        cursor.execute(ROUTES_SQL)
        routes = tuple(Route(**(r | {'station_id': str(r['station_id'])})) for r in cursor.fetchall())
        cursor.execute(TRUCKS_SQL, (1,))
        trucks = tuple(Truck(truck_id=r['truck_id'], station_id=None, plate_number=r['plate_number'], is_active=True) for r in cursor.fetchall())
        cursor.execute(LOCK_STAFF_DIRECTORY_SQL.replace(' FOR UPDATE', '').replace('u.role AS staff_type', 'u.name, u.role AS staff_type'))
        rows = cursor.fetchall()
        staff = tuple(PolicyStaff(r['staff_id'], r['person_id'], r['staff_type'], bool(r['is_active'])) for r in rows)
        cursor.execute(LOCK_HISTORY_SQL.replace(' FOR UPDATE', ''))
        history = tuple(_assignment(r, _colombo()) for r in cursor.fetchall())
        return eligible_crew(PolicyRoster(routes, trucks, staff, history),
            {r['staff_id']: r['name'] for r in rows}, route_id, truck_id, start, end, driver_id, assistant_id)
