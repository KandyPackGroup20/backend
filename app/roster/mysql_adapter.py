"""Explicit MySQL roster reads and one atomic assignment operation.
Asia/Colombo requires system IANA timezone data .
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql
from pydantic import ValidationError

from app.core.database import get_db
from app.roster.policy import (
    AssignmentProposal, DemoV1RosterPolicy, PolicyRoster, PolicyStaff, RosterPolicyViolation,
)
from app.roster.repository import (
    AssignmentOperationResult, RosterBusinessRejection, RosterDataError, RosterWindowError,
    RosterIdempotencyConflict,
)
from app.roster.schemas import (
    Assignment, AssignmentsResponse, AuditAttempt, AuditResponse, CandidatesResponse,
    HoursResponse, MySQLMeta, MySQLWriteMeta, Route, Staff, StaffHours, Truck,
)


ROUTES_SQL = """
    SELECT route_id, station_id, route_name,
           TIME_TO_SEC(max_delivery_time) AS max_duration_seconds
    FROM delivery_route ORDER BY route_id
"""
TRUCKS_SQL = """
    SELECT truck_id, plate_number, is_active, capacity, capacity_unit
    FROM truck WHERE is_active = %s ORDER BY truck_id
"""
STAFF_SQL = """
    SELECT ds.delivery_staff_id AS staff_id, ds.user_id AS person_id,
           u.name, u.role AS staff_type
    FROM delivery_staff AS ds
    INNER JOIN `user` AS u ON u.user_id = ds.user_id
    WHERE u.is_active = %s AND u.role IN (%s, %s)
    ORDER BY ds.delivery_staff_id
"""
ASSIGNMENTS_SQL = """
    SELECT roster_id, route_id, truck_id, driver_id, assistant_id, dispatcher_id,
           start_time, end_time, status, created_at
    FROM roster_assignment
    WHERE (start_time < %s AND end_time > %s)
       OR start_time IS NULL OR end_time IS NULL OR end_time <= start_time
    ORDER BY start_time, roster_id
"""
HOURS_SQL = """
    SELECT staff_id, duty_type,
           SUM(TIMESTAMPDIFF(
               SECOND, GREATEST(start_time, %s), LEAST(end_time, %s)
           )) AS scheduled_seconds,
           MAX(CASE WHEN is_counted IS NULL THEN 1 ELSE 0 END) AS invalid_status
    FROM v_roster_duty_intervals
    WHERE start_time < %s AND end_time > %s
      AND (is_counted = 1 OR is_counted IS NULL)
    GROUP BY staff_id, duty_type
    ORDER BY staff_id, duty_type
"""
AUDIT_SQL = """
    SELECT audit.audit_id, audit.user_id AS actor_id, actor.name AS actor_name,
           audit.outcome, audit.occurred_at, ra.request_key,
           audit.entity_name, audit.entity_id, audit.roster_id AS assignment_id,
           ra.roster_id AS linked_roster_id, ra.dispatcher_id AS assignment_dispatcher_id,
           ra.route_id AS attempted_route_id, ra.truck_id AS attempted_truck_id,
           ra.driver_id AS attempted_driver_id, ra.assistant_id AS attempted_assistant_id,
           route.route_name, route.station_id, station.city AS station_name,
           truck.plate_number, driver.name AS driver_name, assistant.name AS assistant_name,
           ra.start_time AS attempted_start_time, ra.end_time AS attempted_end_time
    FROM audit_log AS audit
    LEFT JOIN roster_assignment AS ra ON ra.roster_id = audit.roster_id
    LEFT JOIN `user` AS actor ON actor.user_id = audit.user_id
    LEFT JOIN delivery_route AS route ON route.route_id = ra.route_id
    LEFT JOIN station_store AS station ON station.station_id = route.station_id
    LEFT JOIN truck AS truck ON truck.truck_id = ra.truck_id
    LEFT JOIN delivery_staff AS driver_staff ON driver_staff.delivery_staff_id = ra.driver_id
    LEFT JOIN `user` AS driver ON driver.user_id = driver_staff.user_id
    LEFT JOIN delivery_staff AS assistant_staff ON assistant_staff.delivery_staff_id = ra.assistant_id
    LEFT JOIN `user` AS assistant ON assistant.user_id = assistant_staff.user_id
    WHERE audit.action = %s AND audit.outcome = 'ACCEPTED'
      AND audit.roster_id IS NOT NULL
    ORDER BY audit.occurred_at DESC, audit.audit_id DESC
    LIMIT %s
"""

DUPLICATE_REQUEST_SQL = """
    SELECT audit.audit_id, audit.action AS audit_action, audit.outcome AS audit_outcome,
           audit.entity_name, audit.entity_id,
           audit.user_id AS actor_id, audit.roster_id AS audit_roster_id,
           ra.roster_id, ra.route_id, ra.truck_id, ra.driver_id, ra.assistant_id,
           ra.dispatcher_id, ra.start_time, ra.end_time, ra.status, ra.created_at
    FROM roster_assignment AS ra
    LEFT JOIN audit_log AS audit ON audit.roster_id = ra.roster_id
    WHERE ra.request_key = %s
"""
# Assignment request facts are immutable. READ COMMITTED lookups need not lock a
# replayed assignment before another request's policy/resource locks.
LOCK_ROUTE_SQL = """
    SELECT route_id, station_id, route_name,
           TIME_TO_SEC(max_delivery_time) AS max_duration_seconds
    FROM delivery_route WHERE route_id = %s FOR UPDATE
"""
LOCK_TRUCK_SQL = """
    SELECT truck_id, plate_number, is_active, capacity, capacity_unit
    FROM truck WHERE truck_id = %s FOR UPDATE
"""
LOCK_CANDIDATE_STAFF_SQL = """
    SELECT ds.delivery_staff_id AS staff_id, ds.user_id AS person_id,
           u.role AS staff_type, u.is_active
    FROM delivery_staff AS ds
    INNER JOIN `user` AS u ON u.user_id = ds.user_id
    WHERE ds.delivery_staff_id IN (%s, %s)
    ORDER BY ds.delivery_staff_id FOR UPDATE
"""
LOCK_ACTOR_SQL = """
    SELECT user_id FROM `user` WHERE user_id = %s FOR UPDATE
"""
LOCK_STAFF_DIRECTORY_SQL = """
    SELECT ds.delivery_staff_id AS staff_id, ds.user_id AS person_id,
           u.role AS staff_type, u.is_active
    FROM delivery_staff AS ds
    INNER JOIN `user` AS u ON u.user_id = ds.user_id
    ORDER BY ds.delivery_staff_id FOR UPDATE
"""
LOCK_HISTORY_SQL = """
    SELECT roster_id, route_id, truck_id, driver_id, assistant_id, dispatcher_id,
           start_time, end_time, status, created_at
    FROM roster_assignment ORDER BY start_time, roster_id FOR UPDATE
"""
INSERT_ASSIGNMENT_SQL = """
    INSERT INTO roster_assignment
        (route_id, truck_id, driver_id, assistant_id, dispatcher_id,
         start_time, end_time, status, request_key)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
UPDATE_WORK_HOURS_SQL = """
    UPDATE delivery_staff
    SET work_hours = COALESCE(work_hours, 0) + %s
    WHERE delivery_staff_id IN (%s, %s)
"""
INSERT_AUDIT_SQL = """
    INSERT INTO audit_log
        (user_id, action, entity_id, outcome, entity_name, roster_id)
    VALUES (%s, %s, %s, %s, %s, %s)
"""
SELECT_ASSIGNMENT_SQL = """
    SELECT roster_id, route_id, truck_id, driver_id, assistant_id, dispatcher_id,
           start_time, end_time, status, created_at
    FROM roster_assignment WHERE roster_id = %s
"""
# Invalid intervals cannot be reliably assigned to a window. Include them so
# corrupt history raises an explicit error instead of disappearing in filtering.


class _RequestKeyRace(Exception):
    """Only the request-key unique constraint can trigger replay recovery."""


def _insert_assignment(cursor, parameters) -> None:
    try:
        cursor.execute(INSERT_ASSIGNMENT_SQL, parameters)
    except pymysql.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062 and "uq_roster_assignment_request_key" in str(exc):
            raise _RequestKeyRace() from exc
        raise


def _invalid() -> RosterDataError:
    return RosterDataError("ROSTER_DATA_INVALID", "Stored roster data does not satisfy the read contract.")


def _positive_integer(value) -> int:
    # PyMySQL TIME_TO_SEC may return int or Decimal. Never truncate or float-cast.
    if type(value) is int:
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise _invalid()
    if not 0 < result <= 9007199254740991:
        raise _invalid()
    return result


def _nonnegative_integer(value) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise _invalid()
    if not 0 <= result <= 9007199254740991:
        raise _invalid()
    return result


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value


def _optional_text(value) -> str | None:
    return None if value is None else _text(value)


def _colombo() -> ZoneInfo:
    try:
        return ZoneInfo("Asia/Colombo")
    except ZoneInfoNotFoundError as exc:
        raise RosterDataError(
            "ROSTER_UNSUPPORTED_CONFIGURATION",
            "Asia/Colombo timezone data is unavailable on the backend.",
        ) from exc


def _stored_datetime(value, zone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None or value.microsecond or value.year < 1000:
        raise _invalid()
    aware = value.replace(tzinfo=zone)
    # DATETIME has no fold flag. Reject ambiguous or nonexistent historical
    # local times rather than inventing which instant the stored value means.
    if (aware.utcoffset().seconds % 60
            or aware.utcoffset() != value.replace(tzinfo=zone, fold=1).utcoffset()
            or aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != value):
        raise _invalid()
    return aware


def _database_error(exc: pymysql.MySQLError) -> RosterDataError:
    code = exc.args[0] if exc.args else None
    if code in {1044, 1045, 1142, 1143, 1227, 1370, 1698}:
        return RosterDataError("ROSTER_DATABASE_ACCESS_DENIED", "Database access for roster reads was denied.")
    if code in {1049, 1054, 1146, 1305, 1356}:
        return RosterDataError("ROSTER_DATABASE_SCHEMA_MISMATCH", "The database does not match the roster read schema.")
    return RosterDataError("ROSTER_DATA_UNAVAILABLE", "Roster database is unavailable. Retry later.")


def _write_database_error(exc: pymysql.MySQLError) -> RosterDataError:
    code = exc.args[0] if exc.args else None
    if code in {1205, 1213}:
        return RosterDataError("ROSTER_DATABASE_BUSY", "Roster assignment is busy. Retry the request.")
    if code in {1044, 1045, 1142, 1143, 1227, 1370, 1698}:
        return RosterDataError("ROSTER_DATABASE_ACCESS_DENIED", "Database access for roster assignment was denied.")
    if code in {1049, 1054, 1146, 1305, 1356}:
        return RosterDataError("ROSTER_DATABASE_SCHEMA_MISMATCH", "The database does not match the roster write schema.")
    return RosterDataError("ROSTER_ASSIGNMENT_FAILED", "Roster assignment could not be stored. Retry later.")


def _assignment(row: dict, zone: ZoneInfo) -> Assignment:
    begin = _stored_datetime(row["start_time"], zone)
    finish = _stored_datetime(row["end_time"], zone)
    duration = finish.astimezone(timezone.utc) - begin.astimezone(timezone.utc)
    seconds = _positive_integer(duration.days * 86400 + duration.seconds)
    return Assignment(
        **{key: _positive_integer(row[key]) for key in (
            "roster_id", "route_id", "truck_id", "driver_id", "assistant_id", "dispatcher_id",
        )},
        start_time=begin, end_time=finish, duration_seconds=seconds,
        status=row["status"], created_at=_stored_datetime(row["created_at"], zone),
    )


def _audit_attempt(row: dict, zone: ZoneInfo) -> AuditAttempt:
    assignment_id = _positive_integer(row["assignment_id"])
    actor_id = _positive_integer(row["actor_id"])
    if (row["outcome"] != "ACCEPTED"
            or row["entity_name"] != "roster_assignment"
            or row["entity_id"] != assignment_id
            or row["linked_roster_id"] != assignment_id
            or row["assignment_dispatcher_id"] != actor_id):
        raise _invalid()
    attempted_start = _stored_datetime(row["attempted_start_time"], zone)
    attempted_end = _stored_datetime(row["attempted_end_time"], zone)
    elapsed = int((attempted_end.astimezone(timezone.utc) - attempted_start.astimezone(timezone.utc)).total_seconds())
    return AuditAttempt(
        audit_id=_positive_integer(row["audit_id"]),
        actor_id=actor_id,
        actor_name=_optional_text(row["actor_name"]),
        route_name=_optional_text(row.get("route_name")),
        station_id=_positive_integer(row["station_id"]) if row.get("station_id") is not None else None,
        station_name=_optional_text(row.get("station_name")),
        plate_number=_optional_text(row.get("plate_number")),
        driver_name=_optional_text(row.get("driver_name")),
        assistant_name=_optional_text(row.get("assistant_name")),
        attempted_route_id=_positive_integer(row["attempted_route_id"]),
        attempted_truck_id=_positive_integer(row["attempted_truck_id"]),
        attempted_driver_id=_positive_integer(row["attempted_driver_id"]),
        attempted_assistant_id=_positive_integer(row["attempted_assistant_id"]),
        attempted_start_time=attempted_start,
        attempted_end_time=attempted_end,
        attempted_duration_seconds=_positive_integer(elapsed),
        outcome="ACCEPTED",
        reason_code=None,
        policy_id=None,
        request_key=_text(row["request_key"]),
        assignment_id=assignment_id,
        occurred_at=_stored_datetime(row["occurred_at"], zone),
        legacy=False,
    )


@contextmanager
def _snapshot():
    try:
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION time_zone = %s", ("+05:30",))
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                yield cursor
            # get_db rolls back exceptions and always closes the connection.
            # Explicitly end successful read transactions, without committing.
            connection.rollback()
    except pymysql.MySQLError as exc:
        raise _database_error(exc) from exc
    except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
        raise _invalid() from exc


class MySQLRosterAdapter:
    def candidates(self) -> CandidatesResponse:
        with _snapshot() as cursor:
            cursor.execute(ROUTES_SQL)
            routes = tuple(Route(
                route_id=_positive_integer(row["route_id"]),
                station_id=str(_positive_integer(row["station_id"])),
                route_name=_text(row["route_name"]),
                max_duration_seconds=_positive_integer(row["max_duration_seconds"]),
            ) for row in cursor.fetchall())
            cursor.execute(TRUCKS_SQL, (1,))
            trucks = []
            for row in cursor.fetchall():
                if type(row["is_active"]) not in (int, bool) or row["is_active"] != 1:
                    raise _invalid()
                trucks.append(Truck(
                    truck_id=_positive_integer(row["truck_id"]), station_id=None,
                    plate_number=_text(row["plate_number"]), is_active=True,
                    capacity=format(Decimal(row["capacity"]), ".2f"), capacity_unit=row["capacity_unit"],
                ))
            cursor.execute(STAFF_SQL, (1, "DRIVER", "ASSISTANT"))
            staff = tuple(Staff(
                staff_id=_positive_integer(row["staff_id"]),
                person_id=_positive_integer(row["person_id"]),
                name=_text(row["name"]), staff_type=row["staff_type"],
            ) for row in cursor.fetchall())
            return CandidatesResponse(
                routes=routes, trucks=tuple(trucks),
                drivers=tuple(person for person in staff if person.staff_type == "DRIVER"),
                assistants=tuple(person for person in staff if person.staff_type == "ASSISTANT"),
                meta=MySQLMeta(),
            )

    def assignments(self, start: datetime, end: datetime) -> AssignmentsResponse:
        zone = _colombo()
        if (start.tzinfo is None or start.utcoffset() is None
                or end.tzinfo is None or end.utcoffset() is None
                or start.microsecond or end.microsecond or end <= start):
            raise RosterWindowError("Assignments require ordered, aware whole-second bounds.")
        try:
            local_start = start.astimezone(zone).replace(tzinfo=None)
            local_end = end.astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError) as exc:
            raise RosterWindowError("Assignment bounds are outside the supported MySQL date range.") from exc
        if (local_start.year < 1000 or local_end.year < 1000
                or local_start.microsecond or local_end.microsecond):
            raise RosterWindowError("Assignment bounds are outside the supported MySQL date range or precision.")
        with _snapshot() as cursor:
            cursor.execute(ASSIGNMENTS_SQL, (local_end, local_start))
            assignments = [_assignment(row, zone) for row in cursor.fetchall()]
            return AssignmentsResponse(assignments=tuple(assignments), meta=MySQLMeta())

    def hours(self, week_start: date) -> HoursResponse:
        _colombo()
        if type(week_start) is not date or week_start.year < 1000 or week_start.weekday() != 0:
            raise RosterWindowError("week_start must be a supported Monday in Asia/Colombo.")
        try:
            week_end = week_start + timedelta(days=7)
            local_start = datetime.combine(week_start, time.min)
            local_end = datetime.combine(week_end, time.min)
        except (OverflowError, ValueError) as exc:
            raise RosterWindowError("week_start is outside the supported MySQL date range.") from exc

        with _snapshot() as cursor:
            cursor.execute(HOURS_SQL, (local_start, local_end, local_end, local_start))
            hours = []
            for row in cursor.fetchall():
                if _nonnegative_integer(row["invalid_status"]):
                    raise _invalid()
                staff_type = row["duty_type"]
                if staff_type == "DRIVER":
                    limit = 144000
                elif staff_type == "ASSISTANT":
                    limit = 216000
                else:
                    raise _invalid()
                scheduled = _nonnegative_integer(row["scheduled_seconds"])
                hours.append(StaffHours(
                    staff_id=_positive_integer(row["staff_id"]),
                    staff_type=staff_type,
                    scheduled_seconds=scheduled,
                    limit_seconds=limit,
                    remaining_seconds=limit - scheduled,
                ))
            cursor.execute(STAFF_SQL, (1, "DRIVER", "ASSISTANT"))
            staff = cursor.fetchall()
            names = {_positive_integer(row["staff_id"]): _text(row["name"]) for row in staff}
            counted = {(row.staff_id, row.staff_type) for row in hours}
            for row in staff:
                staff_id = _positive_integer(row["staff_id"])
                staff_type = row["staff_type"]
                if staff_type not in ("DRIVER", "ASSISTANT"):
                    raise _invalid()
                if (staff_id, staff_type) not in counted:
                    limit = 144000 if staff_type == "DRIVER" else 216000
                    hours.append(StaffHours(
                        staff_id=staff_id, staff_type=staff_type, scheduled_seconds=0,
                        limit_seconds=limit, remaining_seconds=limit,
                    ))
            hours = [row.model_copy(update={"staff_name": names.get(row.staff_id)}) for row in hours]
            hours.sort(key=lambda row: (row.staff_id, row.staff_type))
            return HoursResponse(
                week_start=week_start, week_end=week_end, hours=tuple(hours), meta=MySQLWriteMeta(),
            )

    def audit(self, limit: int) -> AuditResponse:
        zone = _colombo()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RosterWindowError("Audit limit must be an integer from 1 through 100.")
        with _snapshot() as cursor:
            cursor.execute(AUDIT_SQL, ("ASSIGN_ROSTER", limit))
            attempts = tuple(_audit_attempt(row, zone) for row in cursor.fetchall())
            return AuditResponse(attempts=attempts, meta=MySQLWriteMeta())

    def assign(
        self, proposal: AssignmentProposal, *, actor_id: int, request_key: str,
    ) -> AssignmentOperationResult:
        """Create, account, and audit one assignment under one locked decision."""
        zone = _colombo()
        if not request_key or len(request_key) > 128:
            raise RosterDataError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must contain 1 to 128 characters.")
        try:
            local_start = proposal.start_time.astimezone(zone).replace(tzinfo=None)
            local_end = proposal.end_time.astimezone(zone).replace(tzinfo=None)
        except (AttributeError, OverflowError, ValueError) as exc:
            raise RosterDataError("INVALID_ASSIGNMENT_INTERVAL", "Assignment timestamps are outside the supported range.") from exc
        if local_start.year < 1000 or local_end.year < 1000 or local_start.microsecond or local_end.microsecond:
            raise RosterDataError(
                "INVALID_ASSIGNMENT_INTERVAL", "Assignment timestamps are outside MySQL range or precision.",
            )

        try:
            with get_db() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION time_zone = %s", ("+05:30",))
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    connection.begin()
                    try:
                        duplicate = self._duplicate_result(cursor, request_key, zone, proposal, actor_id, local_start, local_end)
                        if duplicate is not None:
                            connection.rollback()
                            return duplicate

                        roster = self._lock_policy_roster(cursor, proposal, actor_id, zone)
                        # The first lookup handles ordinary retries. Recheck after
                        # resource locks to observe a concurrent request that held
                        # the same resource locks when this transaction began.
                        duplicate = self._duplicate_result(cursor, request_key, zone, proposal, actor_id, local_start, local_end)
                        if duplicate is not None:
                            connection.rollback()
                            return duplicate
                        validation = DemoV1RosterPolicy().validate(roster, proposal)
                        _insert_assignment(cursor, (
                            proposal.route_id, proposal.truck_id, proposal.driver_id, proposal.assistant_id,
                            actor_id, local_start, local_end, "SCHEDULED", request_key,
                        ))
                        roster_id = _positive_integer(cursor.lastrowid)

                        hours = (Decimal(validation.duration_seconds) / Decimal(3600)).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP,
                        )
                        cursor.execute(UPDATE_WORK_HOURS_SQL, (hours, proposal.driver_id, proposal.assistant_id))
                        if cursor.rowcount != 2:
                            raise RosterDataError(
                                "ROSTER_DATA_CHANGED", "Roster candidates changed while the assignment was being stored.",
                            )

                        cursor.execute(INSERT_AUDIT_SQL, (
                            actor_id, "ASSIGN_ROSTER", roster_id, "ACCEPTED", "roster_assignment",
                            roster_id,
                        ))
                        cursor.execute(SELECT_ASSIGNMENT_SQL, (roster_id,))
                        stored = cursor.fetchone()
                        if stored is None:
                            raise RosterDataError("ROSTER_DATA_CHANGED", "Stored roster assignment could not be reloaded.")
                        result = AssignmentOperationResult(_assignment(stored, zone))
                        connection.commit()
                        return result
                    except RosterPolicyViolation as rejection:
                        connection.rollback()
                        return self._replay_after_rejection(
                            connection, cursor, proposal, actor_id, request_key, local_start, local_end, rejection, zone,
                        )
                    except _RequestKeyRace:
                        return self._recover_duplicate(
                            connection, cursor, request_key, zone, proposal, actor_id, local_start, local_end,
                        )
                    except Exception:
                        connection.rollback()
                        raise
        except (RosterBusinessRejection, RosterIdempotencyConflict):
            raise
        except RosterDataError:
            raise
        except pymysql.MySQLError as exc:
            raise _write_database_error(exc) from exc
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise _invalid() from exc

    @staticmethod
    def _duplicate_result(
        cursor, request_key: str, zone: ZoneInfo, proposal: AssignmentProposal,
        actor_id: int, local_start: datetime, local_end: datetime,
    ) -> AssignmentOperationResult | None:
        cursor.execute(DUPLICATE_REQUEST_SQL, (request_key,))
        row = cursor.fetchone()
        if row is None:
            return None
        stored_request = tuple(row[key] for key in (
            "dispatcher_id", "route_id", "truck_id", "driver_id",
            "assistant_id", "start_time", "end_time",
        ))
        if stored_request != (
            actor_id, proposal.route_id, proposal.truck_id, proposal.driver_id,
            proposal.assistant_id, local_start, local_end,
        ):
            raise RosterIdempotencyConflict()
        if (row["audit_id"] is None or row["audit_action"] != "ASSIGN_ROSTER"
                or row["audit_outcome"] != "ACCEPTED"
                or row["entity_name"] != "roster_assignment"
                or row["entity_id"] != row["roster_id"]
                or row["audit_roster_id"] != row["roster_id"]
                or row["actor_id"] != row["dispatcher_id"]):
            raise _invalid()
        return AssignmentOperationResult(_assignment(row, zone), replayed=True)

    def _recover_duplicate(
        self, connection, cursor, request_key, zone, proposal, actor_id, local_start, local_end,
    ) -> AssignmentOperationResult:
        # Roll back ALL losing business work before observing the committed winner.
        # Never retry policy/writes or infer success from error 1062 alone.
        connection.rollback()
        connection.begin()
        try:
            result = self._duplicate_result(
                cursor, request_key, zone, proposal, actor_id, local_start, local_end,
            )
            if result is None:
                raise RosterDataError("ROSTER_ASSIGNMENT_FAILED", "Committed request could not be reloaded.")
            return result
        finally:
            connection.rollback()

    @staticmethod
    def _lock_policy_roster(cursor, proposal: AssignmentProposal, actor_id: int, zone: ZoneInfo) -> PolicyRoster:
        cursor.execute(LOCK_ROUTE_SQL, (proposal.route_id,))
        route_row = cursor.fetchone()
        routes = () if route_row is None else (Route(
            route_id=_positive_integer(route_row["route_id"]),
            station_id=str(_positive_integer(route_row["station_id"])),
            route_name=_text(route_row["route_name"]),
            max_duration_seconds=_positive_integer(route_row["max_duration_seconds"]),
        ),)

        cursor.execute(LOCK_TRUCK_SQL, (proposal.truck_id,))
        truck_row = cursor.fetchone()
        trucks = () if truck_row is None else (Truck(
            truck_id=_positive_integer(truck_row["truck_id"]), station_id=None,
            plate_number=_text(truck_row["plate_number"]), is_active=truck_row["is_active"] == 1,
        ),)

        first_staff, second_staff = sorted((proposal.driver_id, proposal.assistant_id))
        cursor.execute(LOCK_CANDIDATE_STAFF_SQL, (first_staff, second_staff))
        cursor.fetchall()

        cursor.execute(LOCK_ACTOR_SQL, (actor_id,))
        if cursor.fetchone() is None:
            raise RosterDataError("ROSTER_ACTOR_INVALID", "The authenticated roster actor no longer exists.")

        cursor.execute(LOCK_STAFF_DIRECTORY_SQL)
        staff = tuple(PolicyStaff(
            staff_id=_positive_integer(row["staff_id"]),
            person_id=_positive_integer(row["person_id"]),
            staff_type=_text(row["staff_type"]),
            is_active=row["is_active"] == 1,
        ) for row in cursor.fetchall())

        cursor.execute(LOCK_HISTORY_SQL)
        history = tuple(_assignment(row, zone) for row in cursor.fetchall())
        return PolicyRoster(routes=routes, trucks=trucks, staff=staff, assignments=history)

    def _replay_after_rejection(
        self, connection, cursor, proposal: AssignmentProposal, actor_id: int, request_key: str,
        local_start: datetime, local_end: datetime, rejection: RosterPolicyViolation, zone: ZoneInfo,
    ) -> AssignmentOperationResult:
        """Observe a committed acceptance after rollback; never persist a rejection."""
        connection.begin()
        try:
            duplicate = self._duplicate_result(
                cursor, request_key, zone, proposal, actor_id, local_start, local_end,
            )
            if duplicate is not None:
                return duplicate
        finally:
            connection.rollback()
        # A rejected key is unreserved. A future attempt is validated afresh.
        raise RosterBusinessRejection(rejection.code, rejection.message) from rejection
