import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.roster import router
from app.core.security import create_access_token
from app.roster import dependencies
from app.roster import mysql_adapter as mysql
from app.roster.dependencies import get_roster_reporting_repository
from app.roster.repository import RosterDataError, RosterWindowError
from app.roster.schemas import AuditAttempt, AuditResponse, MySQLWriteMeta


PREFIX = "/api/v1/roster"


def api_attempt(audit_id=3):
    zone = timedelta(hours=5, minutes=30)
    return AuditAttempt(
        audit_id=audit_id, actor_id=777, actor_name="Dispatcher",
        attempted_route_id=11, attempted_truck_id=21, attempted_driver_id=31,
        attempted_assistant_id=32,
        attempted_start_time=datetime(2026, 9, 22, 9, tzinfo=timezone(zone)),
        attempted_end_time=datetime(2026, 9, 22, 10, tzinfo=timezone(zone)),
        attempted_duration_seconds=3600, outcome="ACCEPTED", reason_code=None,
        policy_id="demo-v1", request_key=f"request-{audit_id}", assignment_id=501,
        occurred_at=datetime(2026, 9, 20, 8, tzinfo=timezone(zone)), legacy=False,
    )


class FakeAuditRepository:
    def __init__(self):
        self.audit_calls = []
        self.error = None
        self.response = AuditResponse(attempts=(api_attempt(),), meta=MySQLWriteMeta())

    def audit(self, limit):
        self.audit_calls.append(limit)
        if self.error:
            raise self.error
        return self.response


class AuditApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeAuditRepository()
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[get_roster_reporting_repository] = lambda: self.repository
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    @staticmethod
    def auth(role="DISPATCHER", *, force_reset=False):
        token = create_access_token({
            "sub": "777", "role": role, "force_password_reset": force_reset,
        }, expires_delta=timedelta(minutes=10))
        return {"Authorization": f"Bearer {token}"}

    def get(self, limit=None, *, headers=None):
        params = {} if limit is None else {"limit": limit}
        return self.client.get(PREFIX + "/audit", params=params, headers=self.auth() if headers is None else headers)

    def test_default_and_maximum_limits_are_forwarded(self):
        default = self.get()
        maximum = self.get(100)
        self.assertEqual((default.status_code, maximum.status_code), (200, 200))
        self.assertEqual(self.repository.audit_calls, [50, 100])
        self.assertEqual(default.headers.get("cache-control"), "no-store")
        self.assertEqual(default.json()["meta"]["policy_id"], "demo-v1")

    def test_zero_over_maximum_and_non_integer_limits_are_rejected(self):
        for value in (0, -1, 101, "1.5", "many"):
            with self.subTest(value=value):
                response = self.get(value)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(self.repository.audit_calls, [])

    def test_authentication_roles_and_reset_guards_precede_audit_access(self):
        for role in ("DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR"):
            with self.subTest(role=role):
                self.assertEqual(self.get(headers=self.auth(role)).status_code, 200)
        calls = len(self.repository.audit_calls)
        for headers, expected in (
            ({}, 401), (self.auth("CUSTOMER"), 403), (self.auth("ASSISTANT"), 403),
            (self.auth(force_reset=True), 403),
        ):
            with self.subTest(expected=expected):
                response = self.get(headers=headers)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(len(self.repository.audit_calls), calls)

    def test_database_failure_is_explicit(self):
        self.repository.error = RosterDataError(
            "ROSTER_DATABASE_ACCESS_DENIED", "Database access for roster reads was denied.",
        )
        with self.assertLogs("app.api.v1.roster", level="ERROR"):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_DATABASE_ACCESS_DENIED")


def audit_row(
    audit_id, occurred_at, *, outcome="ACCEPTED", detail=True, reason=None,
    assignment_id=501, request_key=None,
):
    row = {
        "audit_id": audit_id, "actor_id": 777, "actor_name": "Dispatcher",
        "base_outcome": outcome, "occurred_at": occurred_at,
        "detail_audit_id": audit_id if detail else None,
        "request_key": request_key or f"request-{audit_id}",
        "assignment_id": assignment_id,
        "attempted_route_id": 11, "attempted_truck_id": 21,
        "attempted_driver_id": 31, "attempted_assistant_id": 32,
        "attempted_start_time": datetime(2026, 9, 22, 9),
        "attempted_end_time": datetime(2026, 9, 22, 10),
        "attempted_duration_seconds": 3600,
        "policy_id": "demo-v1", "detail_outcome": outcome,
        "reason_code": reason,
    }
    if not detail:
        row.update({
            "request_key": None, "assignment_id": None,
            "attempted_route_id": None, "attempted_truck_id": None,
            "attempted_driver_id": None, "attempted_assistant_id": None,
            "attempted_start_time": None, "attempted_end_time": None,
            "attempted_duration_seconds": None, "policy_id": None,
            "detail_outcome": None, "reason_code": None,
        })
    return row


class AuditCursor:
    def __init__(self, rows=(), *, fail=False):
        self.rows = tuple(rows)
        self.fail = fail
        self.executions = []
        self.current = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))
        if statement == mysql.AUDIT_SQL:
            if self.fail:
                raise pymysql.OperationalError(1045, "secret credentials")
            limit = parameters[-1]
            self.current = sorted(
                self.rows,
                key=lambda row: (row["occurred_at"] is not None, row["occurred_at"] or datetime.min, row["audit_id"]),
                reverse=True,
            )[:limit]

    def fetchall(self):
        return self.current


class AuditConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollbacks += 1


class AuditDatabase:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.connection.rollback()
        self.connection.closed = True


class MySQLAuditTests(unittest.TestCase):
    def run_audit(self, rows=(), *, limit=50, fail=False):
        cursor = AuditCursor(rows, fail=fail)
        connection = AuditConnection(cursor)
        with patch.object(mysql, "get_db", return_value=AuditDatabase(connection)):
            result = mysql.MySQLRosterAdapter().audit(limit)
        return result, cursor, connection

    def test_empty_persistent_history_is_a_real_empty_result(self):
        response, cursor, connection = self.run_audit()
        self.assertEqual(response.attempts, ())
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_accepted_rejected_and_legacy_records_preserve_only_stored_details(self):
        rows = (
            audit_row(1, datetime(2026, 9, 20, 8), detail=False, outcome="FAILURE"),
            audit_row(2, datetime(2026, 9, 20, 9), outcome="REJECTED", reason="TRUCK_OVERLAP", assignment_id=None),
            audit_row(3, datetime(2026, 9, 20, 10), outcome="ACCEPTED", assignment_id=503),
        )
        response, _, _ = self.run_audit(rows)
        accepted, rejected, legacy = response.attempts
        self.assertEqual([accepted.audit_id, rejected.audit_id, legacy.audit_id], [3, 2, 1])
        self.assertEqual((accepted.outcome, accepted.assignment_id, accepted.request_key), ("ACCEPTED", 503, "request-3"))
        self.assertEqual((rejected.outcome, rejected.reason_code, rejected.assignment_id), ("REJECTED", "TRUCK_OVERLAP", None))
        self.assertTrue(legacy.legacy)
        self.assertEqual(legacy.outcome, "FAILURE")
        self.assertEqual((legacy.request_key, legacy.policy_id, legacy.assignment_id), (None, None, None))
        self.assertIsNone(legacy.attempted_route_id)

    def test_newest_first_uses_audit_id_as_deterministic_tie_breaker(self):
        same_time = datetime(2026, 9, 20, 10)
        response, cursor, _ = self.run_audit((
            audit_row(8, same_time, assignment_id=508),
            audit_row(10, same_time, assignment_id=510),
            audit_row(9, same_time, assignment_id=509),
        ), limit=2)
        self.assertEqual([row.audit_id for row in response.attempts], [10, 9])
        self.assertIn("ORDER BY audit.occurred_at DESC, audit.audit_id DESC", mysql.AUDIT_SQL)
        params = next(params for sql, params in cursor.executions if sql == mysql.AUDIT_SQL)
        self.assertEqual(params, ("roster_assignment", 2))
        self.assertIn("LEFT JOIN roster_assignment_audit_detail", mysql.AUDIT_SQL)

    def test_inconsistent_persisted_detail_is_an_explicit_data_error(self):
        row = audit_row(1, datetime(2026, 9, 20, 8), outcome="ACCEPTED")
        row["detail_outcome"] = "REJECTED"
        with self.assertRaises(RosterDataError) as caught:
            self.run_audit((row,))
        self.assertEqual(caught.exception.error_code, "ROSTER_DATA_INVALID")

    def test_database_failure_maps_explicitly_and_closes_connection(self):
        cursor = AuditCursor(fail=True)
        connection = AuditConnection(cursor)
        with patch.object(mysql, "get_db", return_value=AuditDatabase(connection)):
            with self.assertRaises(RosterDataError) as caught:
                mysql.MySQLRosterAdapter().audit(50)
        self.assertEqual(caught.exception.error_code, "ROSTER_DATABASE_ACCESS_DENIED")
        self.assertNotIn("secret", caught.exception.message)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        self.assertGreaterEqual(connection.rollbacks, 1)

    def test_direct_invalid_limits_are_rejected_before_connection(self):
        with patch.object(mysql, "get_db") as connect:
            for value in (0, 101, True, 1.5):
                with self.subTest(value=value), self.assertRaises(RosterWindowError):
                    mysql.MySQLRosterAdapter().audit(value)
            connect.assert_not_called()


class ReportingModeTests(unittest.TestCase):
    @staticmethod
    def auth():
        token = create_access_token({
            "sub": "777", "role": "DISPATCHER", "force_password_reset": False,
        }, expires_delta=timedelta(minutes=10))
        return {"Authorization": f"Bearer {token}"}

    def test_development_memory_mode_never_invents_persistent_audit_records(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        with TestClient(app) as client, patch.dict(os.environ, {
            "APP_ENV": "development", "ROSTER_DATA_MODE": "dev-memory", "WEB_CONCURRENCY": "1",
        }), patch.object(dependencies, "make_fixtures") as fixtures:
            for path in ("/hours?week_start=2026-09-14", "/audit"):
                response = client.get(PREFIX + path, headers=self.auth())
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_REPORTING_REQUIRES_MYSQL")
                self.assertEqual(response.headers.get("cache-control"), "no-store")
            fixtures.assert_not_called()


if __name__ == "__main__":
    unittest.main()
