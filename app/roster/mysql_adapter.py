"""Explicit MySQL roster reads and one atomic assignment operation.

Uses the existing connection manager without altering authentication sessions.
No seed, write, stored-procedure call, or development fallback exists here.
Asia/Colombo requires system IANA timezone data (or tzdata on Windows).
"""

from contextlib import contextmanager
from datetime import datetime, timezone
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
)
from app.roster.schemas import (
    Assignment, AssignmentsResponse, CandidatesResponse, MySQLMeta, Route, Staff, Truck,
)


ROUTES_SQL = """
    SELECT route_id, station_id, route_name,
           TIME_TO_SEC(max_delivery_time) AS max_duration_seconds
    FROM delivery_route ORDER BY route_id
"""
TRUCKS_SQL = """
    SELECT truck_id, plate_number, is_active
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

DUPLICATE_REQUEST_SQL = """
    SELECT detail.outcome AS audit_outcome, detail.reason_code,
           ra.roster_id, ra.route_id, ra.truck_id, ra.driver_id, ra.assistant_id,
           ra.dispatcher_id, ra.start_time, ra.end_time, ra.status, ra.created_at
    FROM roster_assignment_audit_detail AS detail
    LEFT JOIN roster_assignment AS ra ON ra.roster_id = detail.roster_id
    WHERE detail.request_key = %s
    FOR UPDATE
"""
LOCK_ROUTE_SQL = """
    SELECT route_id, station_id, route_name,
           TIME_TO_SEC(max_delivery_time) AS max_duration_seconds
    FROM delivery_route WHERE route_id = %s FOR UPDATE
"""
LOCK_TRUCK_SQL = """
    SELECT truck_id, plate_number, is_active
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
         start_time, end_time, status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
UPDATE_WORK_HOURS_SQL = """
    UPDATE delivery_staff
    SET work_hours = COALESCE(work_hours, 0) + %s
    WHERE delivery_staff_id IN (%s, %s)
"""
INSERT_AUDIT_SQL = """
    INSERT INTO audit_log (user_id, action, entity_id, outcome, entity_name)
    VALUES (%s, %s, %s, %s, %s)
"""
INSERT_AUDIT_DETAIL_SQL = """
    INSERT INTO roster_assignment_audit_detail
        (audit_id, request_key, roster_id, route_id, truck_id, driver_id,
         assistant_id, start_time, end_time, duration_seconds, policy_id,
         outcome, reason_code)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
SELECT_ASSIGNMENT_SQL = """
    SELECT roster_id, route_id, truck_id, driver_id, assistant_id, dispatcher_id,
           start_time, end_time, status, created_at
    FROM roster_assignment WHERE roster_id = %s
"""
# Invalid intervals cannot be reliably assigned to a window. Include them so
# corrupt history raises an explicit error instead of disappearing in filtering.


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


def _text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value


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
                        duplicate = self._duplicate_result(cursor, request_key, zone)
                        if duplicate is not None:
                            connection.rollback()
                            return duplicate

                        roster = self._lock_policy_roster(cursor, proposal, actor_id, zone)
                        # The first lookup handles ordinary retries. Recheck after
                        # resource locks to observe a concurrent request that held
                        # the same resource locks when this transaction began.
                        duplicate = self._duplicate_result(cursor, request_key, zone)
                        if duplicate is not None:
                            connection.rollback()
                            return duplicate
                        validation = DemoV1RosterPolicy().validate(roster, proposal)
                        cursor.execute(INSERT_ASSIGNMENT_SQL, (
                            proposal.route_id, proposal.truck_id, proposal.driver_id, proposal.assistant_id,
                            actor_id, local_start, local_end, "SCHEDULED",
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
                        ))
                        audit_id = _positive_integer(cursor.lastrowid)
                        cursor.execute(INSERT_AUDIT_DETAIL_SQL, (
                            audit_id, request_key, roster_id, proposal.route_id, proposal.truck_id,
                            proposal.driver_id, proposal.assistant_id, local_start, local_end,
                            validation.duration_seconds, DemoV1RosterPolicy.policy_id, "ACCEPTED", None,
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
                        self._persist_rejection(
                            connection, cursor, proposal, actor_id, request_key, local_start, local_end, rejection,
                        )
                        raise RosterBusinessRejection(rejection.code, rejection.message) from rejection
                    except Exception:
                        connection.rollback()
                        raise
        except RosterBusinessRejection:
            raise
        except RosterDataError:
            raise
        except pymysql.MySQLError as exc:
            raise _write_database_error(exc) from exc
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise _invalid() from exc

    @staticmethod
    def _duplicate_result(cursor, request_key: str, zone: ZoneInfo) -> AssignmentOperationResult | None:
        cursor.execute(DUPLICATE_REQUEST_SQL, (request_key,))
        row = cursor.fetchone()
        if row is None:
            return None
        if row["audit_outcome"] == "REJECTED":
            raise RosterBusinessRejection(
                row["reason_code"] or "ROSTER_REJECTED", "This assignment request was previously rejected.",
            )
        if row["audit_outcome"] != "ACCEPTED" or row["roster_id"] is None:
            raise _invalid()
        return AssignmentOperationResult(_assignment(row, zone), replayed=True)

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

    @staticmethod
    def _persist_rejection(
        connection, cursor, proposal: AssignmentProposal, actor_id: int, request_key: str,
        local_start: datetime, local_end: datetime, rejection: RosterPolicyViolation,
    ) -> None:
        """Use a fresh transaction so rolling back business work cannot erase the audit."""
        try:
            connection.begin()
            duration_seconds = max(
                0,
                int((proposal.end_time.astimezone(timezone.utc) - proposal.start_time.astimezone(timezone.utc)).total_seconds()),
            )
            cursor.execute(INSERT_AUDIT_SQL, (
                actor_id, "ASSIGN_ROSTER", proposal.route_id, "REJECTED", "roster_assignment",
            ))
            audit_id = _positive_integer(cursor.lastrowid)
            cursor.execute(INSERT_AUDIT_DETAIL_SQL, (
                audit_id, request_key, None, proposal.route_id, proposal.truck_id,
                proposal.driver_id, proposal.assistant_id, local_start, local_end,
                duration_seconds, DemoV1RosterPolicy.policy_id, "REJECTED", rejection.code,
            ))
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, pymysql.MySQLError):
                mapped = _write_database_error(exc)
                raise RosterDataError(
                    "ROSTER_AUDIT_PERSISTENCE_FAILED", "Rejected roster decision could not be audited.",
                ) from mapped
            raise RosterDataError(
                "ROSTER_AUDIT_PERSISTENCE_FAILED", "Rejected roster decision could not be audited.",
            ) from exc
