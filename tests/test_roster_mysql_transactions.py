from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

import pymysql

from app.roster import mysql_adapter as mysql
from app.roster.policy import AssignmentProposal
from app.roster.repository import RosterBusinessRejection, RosterDataError, RosterIdempotencyConflict


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


def duplicate_row():
    return assignment_row() | {
        "audit_id": 801, "actor_id": 41, "audit_roster_id": 501,
        "audit_action": "ASSIGN_ROSTER", "audit_outcome": "ACCEPTED",
        "entity_name": "roster_assignment", "entity_id": 501,
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

    def test_policy_rejection_rolls_back_without_persisting_anything(self):
        cursor = FakeCursor(route_seconds=30 * 60)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterBusinessRejection) as caught:
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="rejected-1")
        self.assertEqual(caught.exception.error_code, "ROUTE_MAX_DURATION_EXCEEDED")
        self.assertGreaterEqual(connection.rollbacks, 2)
        self.assertEqual(connection.commits, 0)
        for sql in (mysql.INSERT_ASSIGNMENT_SQL, mysql.UPDATE_WORK_HOURS_SQL, mysql.INSERT_AUDIT_SQL):
            self.assertNotIn(sql, [statement for statement, _ in cursor.executions])

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
        cursor = FakeCursor(fail_sql=mysql.INSERT_AUDIT_SQL)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterDataError):
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="audit-fail")

        self.assertEqual(connection.commits, 0)
        self.assertGreaterEqual(connection.rollbacks, 1)
        self.assertIn(mysql.INSERT_ASSIGNMENT_SQL, [sql for sql, _ in cursor.executions])

    def test_rejection_rechecks_committed_acceptance_in_a_read_only_transaction(self):
        cursor = FakeCursor(route_seconds=1)
        connection = FakeConnection(cursor)
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterBusinessRejection):
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="unreserved")
        self.assertEqual(connection.begins, 2)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(sum(sql == mysql.DUPLICATE_REQUEST_SQL for sql, _ in cursor.executions), 3)
        self.assertNotIn(mysql.INSERT_AUDIT_SQL, [sql for sql, _ in cursor.executions])

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
        duplicate = duplicate_row()
        cursor = FakeCursor(duplicate=duplicate)
        result, connection = self.run_assignment(cursor, key="same-key")

        self.assertTrue(result.replayed)
        self.assertEqual(result.assignment.roster_id, 501)
        self.assertEqual(connection.commits, 0)
        self.assertNotIn(mysql.INSERT_ASSIGNMENT_SQL, [sql for sql, _ in cursor.executions])


    def test_success_writes_one_linked_audit_and_stores_key_on_assignment(self):
        cursor = FakeCursor()
        self.run_assignment(cursor)
        audits = [params for sql, params in cursor.executions if sql == mysql.INSERT_AUDIT_SQL]
        self.assertEqual(audits, [(41, "ASSIGN_ROSTER", 501, "ACCEPTED", "roster_assignment", 501)])
        inserts = [params for sql, params in cursor.executions if sql == mysql.INSERT_ASSIGNMENT_SQL]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][-1], "request-1")

    def test_accepted_replay_returns_current_status_without_writes(self):
        cursor = FakeCursor(duplicate=duplicate_row() | {"status": "CANCELLED"})
        result, connection = self.run_assignment(cursor)
        self.assertTrue(result.replayed)
        self.assertEqual(result.assignment.status, "CANCELLED")
        self.assertEqual(connection.commits, 0)
        self.assertFalse(any(sql in (mysql.INSERT_ASSIGNMENT_SQL, mysql.UPDATE_WORK_HOURS_SQL,
                                     mysql.INSERT_AUDIT_SQL) for sql, _ in cursor.executions))

    def test_different_actor_or_each_request_field_conflicts_without_writing(self):
        for field in ("dispatcher_id", "route_id", "truck_id", "driver_id",
                      "assistant_id", "start_time", "end_time"):
            with self.subTest(field=field):
                row = duplicate_row()
                row[field] += timedelta(seconds=1) if isinstance(row[field], datetime) else 1
                cursor = FakeCursor(duplicate=row)
                with self.assertRaises(RosterIdempotencyConflict):
                    self.run_assignment(cursor)
                self.assertEqual([sql for sql, _ in cursor.executions if sql == mysql.INSERT_AUDIT_SQL], [])

    def test_equivalent_offsets_replay_the_same_request(self):
        item = proposal()
        item = replace(item, start_time=item.start_time.astimezone(timezone.utc),
                       end_time=item.end_time.astimezone(timezone.utc))
        result, _ = self.run_assignment(FakeCursor(duplicate=duplicate_row()), item=item)
        self.assertTrue(result.replayed)

    def test_missing_or_inconsistent_accepted_audit_is_data_error(self):
        for changes in ({"audit_id": None}, {"audit_roster_id": None}, {"actor_id": 99},
                        {"audit_action": "OTHER_FEATURE"}, {"audit_outcome": "REJECTED"},
                        {"entity_name": "delivery_route"}, {"entity_id": 999}):
            with self.subTest(changes=changes), self.assertRaises(RosterDataError) as caught:
                self.run_assignment(FakeCursor(duplicate=duplicate_row() | changes))
            self.assertEqual(caught.exception.error_code, "ROSTER_DATA_INVALID")

    def test_rejection_does_not_depend_on_audit_insert_availability(self):
        cursor = FakeCursor(route_seconds=1, fail_sql=mysql.INSERT_AUDIT_SQL)
        with self.assertRaises(RosterBusinessRejection) as caught:
            self.run_assignment(cursor)
        self.assertEqual(caught.exception.error_code, "ROUTE_MAX_DURATION_EXCEEDED")

    def test_key_collision_rolls_back_then_replays_in_fresh_transaction(self):
        cursor = RacingCursor(duplicate_row())
        connection = FakeConnection(cursor)
        cursor.connection = connection
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            result = mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="race")
        self.assertTrue(result.replayed)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.begins, 2)
        self.assertTrue(cursor.recovered_after_rollback)
        self.assertNotIn(mysql.UPDATE_WORK_HOURS_SQL, [sql for sql, _ in cursor.executions])

    def test_key_collision_with_different_winner_conflicts(self):
        cursor = RacingCursor(duplicate_row() | {"dispatcher_id": 99})
        with self.assertRaises(RosterIdempotencyConflict):
            self.run_assignment(cursor)

    def test_unrelated_duplicate_error_is_not_replay(self):
        cursor = RacingCursor(duplicate_row(), constraint="some_other_unique_key")
        with self.assertRaises(RosterDataError) as caught:
            self.run_assignment(cursor)
        self.assertEqual(caught.exception.error_code, "ROSTER_ASSIGNMENT_FAILED")
        self.assertFalse(cursor.recovered_after_rollback)

    def test_key_collision_without_visible_winner_is_not_success(self):
        with self.assertRaises(RosterDataError):
            self.run_assignment(RacingCursor(None))

    def test_uncertain_commit_is_not_automatically_retried(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        connection.commit = mock.Mock(side_effect=pymysql.OperationalError(2013, "lost response"))
        with mock.patch.object(mysql, "get_db", lambda: fake_database(connection)):
            with self.assertRaises(RosterDataError) as caught:
                mysql.MySQLRosterAdapter().assign(proposal(), actor_id=41, request_key="uncertain")
        self.assertEqual(caught.exception.error_code, "ROSTER_ASSIGNMENT_FAILED")
        self.assertEqual(sum(sql == mysql.INSERT_ASSIGNMENT_SQL for sql, _ in cursor.executions), 1)


class RacingCursor(FakeCursor):
    def __init__(self, winner, *, constraint="uq_roster_assignment_request_key"):
        super().__init__()
        self.winner = winner
        self.constraint = constraint
        self.collided = False
        self.connection = None
        self.rollbacks_at_collision = 0
        self.recovered_after_rollback = False

    def execute(self, sql, params=None):
        if sql == mysql.INSERT_ASSIGNMENT_SQL:
            self.executions.append((sql, params))
            self.collided = True
            if self.connection:
                self.rollbacks_at_collision = self.connection.rollbacks
            raise pymysql.IntegrityError(1062, f"Duplicate entry for key '{self.constraint}'")
        if sql == mysql.DUPLICATE_REQUEST_SQL and self.collided:
            self.duplicate = self.winner
            if self.connection:
                self.recovered_after_rollback = self.connection.rollbacks > self.rollbacks_at_collision
        return super().execute(sql, params)
