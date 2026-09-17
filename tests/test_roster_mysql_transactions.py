from contextlib import contextmanager
from datetime import datetime
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

import pymysql

from app.roster import mysql_adapter as mysql
from app.roster.policy import AssignmentProposal
from app.roster.repository import RosterBusinessRejection, RosterDataError


ZONE = ZoneInfo("Asia/Colombo")


def proposal(*, hours: int = 1) -> AssignmentProposal:
    start = datetime(2026, 9, 22, 9, 0, tzinfo=ZONE)
    return AssignmentProposal(
        route_id=11, truck_id=21, driver_id=31, assistant_id=32,
        start_time=start, end_time=start.replace(hour=start.hour + hours),
    )


def assignment_row() -> dict:
    return {
        "roster_id": 501, "route_id": 11, "truck_id": 21,
        "driver_id": 31, "assistant_id": 32, "dispatcher_id": 41,
        "start_time": datetime(2026, 9, 22, 9, 0),
        "end_time": datetime(2026, 9, 22, 10, 0),
        "status": "SCHEDULED", "created_at": datetime(2026, 9, 20, 8, 0),
    }


class FakeCursor:
    def __init__(self, *, fail_sql=None, duplicate=None, route_seconds=8 * 3600):
        self.fail_sql = fail_sql
        self.duplicate = duplicate
        self.route_seconds = route_seconds
        self.executions = []
        self.lastrowid = None
        self.rowcount = 0
        self._one = None
        self._all = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self._one, self._all, self.rowcount = None, [], 0
        if self.fail_sql == sql:
            raise pymysql.err.OperationalError(1213 if sql == mysql.LOCK_HISTORY_SQL else 1064, "forced")
        if sql == mysql.DUPLICATE_REQUEST_SQL:
            self._one = self.duplicate
        elif sql == mysql.LOCK_ROUTE_SQL:
            self._one = {
                "route_id": 11, "station_id": 1, "route_name": "Central",
                "max_duration_seconds": self.route_seconds,
            }
        elif sql == mysql.LOCK_TRUCK_SQL:
            self._one = {"truck_id": 21, "plate_number": "WP-1001", "is_active": 1}
        elif sql == mysql.LOCK_CANDIDATE_STAFF_SQL:
            self._all = [
                {"staff_id": 31, "person_id": 131, "staff_type": "DRIVER", "is_active": 1},
                {"staff_id": 32, "person_id": 132, "staff_type": "ASSISTANT", "is_active": 1},
            ]
        elif sql == mysql.LOCK_ACTOR_SQL:
            self._one = {"user_id": 41}
        elif sql == mysql.LOCK_STAFF_DIRECTORY_SQL:
            self._all = [
                {"staff_id": 31, "person_id": 131, "staff_type": "DRIVER", "is_active": 1},
                {"staff_id": 32, "person_id": 132, "staff_type": "ASSISTANT", "is_active": 1},
            ]
        elif sql == mysql.LOCK_HISTORY_SQL:
            self._all = []
        elif sql == mysql.INSERT_ASSIGNMENT_SQL:
            self.lastrowid, self.rowcount = 501, 1
        elif sql == mysql.UPDATE_WORK_HOURS_SQL:
            self.rowcount = 2
        elif sql == mysql.INSERT_AUDIT_SQL:
            self.lastrowid, self.rowcount = 801, 1
        elif sql == mysql.INSERT_AUDIT_DETAIL_SQL:
            self.lastrowid, self.rowcount = 901, 1
        elif sql == mysql.SELECT_ASSIGNMENT_SQL:
            self._one = assignment_row()
        return self.rowcount

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def begin(self):
        self.begins += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@contextmanager
def fake_database(connection):
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class MySQLRosterTransactionTests(TestCase):
    def run_assignment(self, cursor, *, item=None, key="request-1"):
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            result = mysql.MySQLRosterAdapter().assign(
                item or proposal(), actor_id=41, request_key=key,
            )
        return result, connection

    def test_success_commits_one_assignment_and_derived_hours(self):
        cursor = FakeCursor()
        result, connection = self.run_assignment(cursor)

        self.assertEqual(result.assignment.roster_id, 501)
        self.assertFalse(result.replayed)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(sum(sql == mysql.INSERT_ASSIGNMENT_SQL for sql, _ in cursor.executions), 1)
        hours_params = next(params for sql, params in cursor.executions if sql == mysql.UPDATE_WORK_HOURS_SQL)
        self.assertEqual(str(hours_params[0]), "1.00")

    def test_policy_rejection_rolls_back_business_work_and_persists_rejected_audit(self):
        cursor = FakeCursor(route_seconds=30 * 60)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterBusinessRejection) as caught:
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="rejected-1")

        self.assertEqual(caught.exception.error_code, "ROUTE_MAX_DURATION_EXCEEDED")
        self.assertGreaterEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 1)
        self.assertNotIn(mysql.INSERT_ASSIGNMENT_SQL, [sql for sql, _ in cursor.executions])
        detail = next(params for sql, params in cursor.executions if sql == mysql.INSERT_AUDIT_DETAIL_SQL)
        self.assertEqual(detail[-2:], ("REJECTED", "ROUTE_MAX_DURATION_EXCEEDED"))

    def test_insert_failure_rolls_back_without_business_rejection(self):
        cursor = FakeCursor(fail_sql=mysql.INSERT_ASSIGNMENT_SQL)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterDataError) as caught:
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="insert-fail")

        self.assertEqual(caught.exception.error_code, "ROSTER_ASSIGNMENT_FAILED")
        self.assertGreaterEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)
        self.assertNotIn(mysql.INSERT_AUDIT_SQL, [sql for sql, _ in cursor.executions])

    def test_accepted_audit_failure_rolls_back_assignment_and_hours(self):
        cursor = FakeCursor(fail_sql=mysql.INSERT_AUDIT_DETAIL_SQL)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterDataError):
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="audit-fail")

        self.assertEqual(connection.commits, 0)
        self.assertGreaterEqual(connection.rollbacks, 1)
        self.assertIn(mysql.INSERT_ASSIGNMENT_SQL, [sql for sql, _ in cursor.executions])

    def test_rejected_audit_uses_a_fresh_transaction(self):
        cursor = FakeCursor(route_seconds=1)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterBusinessRejection):
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="fresh-audit")

        self.assertEqual(connection.begins, 2)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(sum(sql == mysql.INSERT_AUDIT_SQL for sql, _ in cursor.executions), 1)

    def test_cursor_and_connection_are_closed(self):
        cursor = FakeCursor()
        _, connection = self.run_assignment(cursor, key="cleanup")
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_deadlock_is_an_explicit_retryable_service_error(self):
        cursor = FakeCursor(fail_sql=mysql.LOCK_HISTORY_SQL)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterDataError) as caught:
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="deadlock")

        self.assertEqual(caught.exception.error_code, "ROSTER_DATABASE_BUSY")
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_duplicate_accepted_request_returns_existing_assignment_without_writes(self):
        duplicate = assignment_row() | {"audit_outcome": "ACCEPTED", "reason_code": None}
        cursor = FakeCursor(duplicate=duplicate)
        result, connection = self.run_assignment(cursor, key="same-key")

        self.assertTrue(result.replayed)
        self.assertEqual(result.assignment.roster_id, 501)
        self.assertEqual(connection.commits, 0)
        self.assertNotIn(mysql.INSERT_ASSIGNMENT_SQL, [sql for sql, _ in cursor.executions])
