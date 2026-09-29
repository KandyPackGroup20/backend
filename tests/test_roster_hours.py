import os
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.roster import router
from app.core.security import create_access_token
from app.roster import mysql_adapter as mysql
from app.roster.dependencies import get_roster_reporting_repository
from app.roster.repository import RosterDataError
from app.roster.schemas import HoursResponse, MySQLWriteMeta, StaffHours


PREFIX = "/api/v1/roster"


class FakeReportingRepository:
    def __init__(self):
        self.hours_calls = []
        self.error = None
        self.response = HoursResponse(
            week_start=date(2026, 9, 14), week_end=date(2026, 9, 21),
            hours=(StaffHours(
                staff_id=31, staff_type="DRIVER", scheduled_seconds=3600,
                limit_seconds=144000, remaining_seconds=140400,
            ),),
            meta=MySQLWriteMeta(),
        )

    def hours(self, week_start):
        self.hours_calls.append(week_start)
        if self.error:
            raise self.error
        return self.response


class HoursApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeReportingRepository()
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

    def get(self, value="2026-09-14", *, headers=None):
        params = {} if value is None else {"week_start": value}
        return self.client.get(PREFIX + "/hours", params=params, headers=self.auth() if headers is None else headers)

    def test_monday_is_required_and_response_keeps_mysql_policy_metadata(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.repository.hours_calls, [date(2026, 9, 14)])
        self.assertEqual(response.json()["week_end"], "2026-09-21")
        self.assertEqual(response.json()["meta"], {
            "data_source": "mysql", "policy_id": "kandypack-roster", "policy_confirmed": False,
            "timezone": "Asia/Colombo", "volatile": False, "fixture_week_start": None,
        })
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_missing_non_monday_offset_datetime_and_non_exact_dates_are_rejected(self):
        for value in (
            None, "2026-09-15", "2026-09-14T00:00:00+05:30", "2026-09-14+05:30",
            "20260914", "not-a-date", "0999-12-30", "9999-12-27",
        ):
            with self.subTest(value=value):
                response = self.get(value)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(self.repository.hours_calls, [])

    def test_authentication_roles_and_password_reset_guard_run_before_reporting(self):
        for role in ("DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR"):
            with self.subTest(role=role):
                self.assertEqual(self.get(headers=self.auth(role)).status_code, 200)
        calls = len(self.repository.hours_calls)
        for headers, expected in (
            ({}, 401), (self.auth("CUSTOMER"), 403), (self.auth("DRIVER"), 403),
            (self.auth(force_reset=True), 403),
        ):
            with self.subTest(expected=expected):
                response = self.get(headers=headers)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(len(self.repository.hours_calls), calls)

    def test_database_error_is_explicit_and_not_an_empty_success(self):
        self.repository.error = RosterDataError(
            "ROSTER_DATABASE_SCHEMA_MISMATCH", "The database does not match the roster read schema.",
        )
        with self.assertLogs("app.api.v1.roster", level="ERROR"):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_DATABASE_SCHEMA_MISMATCH")
        self.assertNotEqual(response.json(), {"hours": []})


class DutyCursor:
    def __init__(self, duties=(), *, fail=False):
        self.duties = tuple(duties)
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
        if statement == mysql.HOURS_SQL:
            if self.fail:
                raise pymysql.OperationalError(1146, "forced missing reporting view")
            week_start, week_end = parameters[0], parameters[1]
            grouped = {}
            for duty in self.duties:
                if duty["status"] == "CANCELLED":
                    continue
                if duty["start_time"] >= week_end or duty["end_time"] <= week_start:
                    continue
                key = (duty["staff_id"], duty["duty_type"])
                seconds = int((
                    min(duty["end_time"], week_end) - max(duty["start_time"], week_start)
                ).total_seconds())
                current = grouped.setdefault(key, {"seconds": 0, "invalid": 0})
                current["seconds"] += seconds
                current["invalid"] = max(
                    current["invalid"],
                    int(duty["status"] not in {"SCHEDULED", "IN_TRANSIT", "COMPLETED"}),
                )
            self.current = [
                {
                    "staff_id": staff_id, "duty_type": duty_type,
                    "scheduled_seconds": Decimal(values["seconds"]),
                    "invalid_status": values["invalid"],
                }
                for (staff_id, duty_type), values in sorted(grouped.items())
            ]

    def fetchall(self):
        return self.current


class DutyConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollbacks += 1


class DutyDatabase:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.connection.rollback()
        self.connection.closed = True


def duty(staff_id, duty_type, start, end, status="SCHEDULED"):
    return {
        "staff_id": staff_id, "duty_type": duty_type,
        "start_time": start, "end_time": end, "status": status,
    }


class MySQLHoursTests(unittest.TestCase):
    def run_hours(self, week_start, duties=(), *, fail=False):
        cursor = DutyCursor(duties, fail=fail)
        connection = DutyConnection(cursor)
        with patch.object(mysql, "get_db", return_value=DutyDatabase(connection)):
            result = mysql.MySQLRosterAdapter().hours(week_start)
        return result, cursor, connection

    def test_empty_selected_week_is_a_real_empty_result(self):
        response, cursor, connection = self.run_hours(date(2026, 9, 14))
        self.assertEqual(response.hours, ())
        self.assertEqual(response.week_end, date(2026, 9, 21))
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_exact_caps_one_second_over_and_staff_in_both_duty_roles(self):
        monday = datetime(2026, 9, 14)
        rows = (
            duty(31, "DRIVER", monday, monday + timedelta(seconds=144000)),
            duty(32, "ASSISTANT", monday, monday + timedelta(seconds=216000)),
            duty(33, "DRIVER", monday, monday + timedelta(seconds=144001)),
            duty(40, "DRIVER", monday, monday + timedelta(hours=1)),
            duty(40, "ASSISTANT", monday + timedelta(days=2), monday + timedelta(days=2, hours=2)),
        )
        response, _, _ = self.run_hours(date(2026, 9, 14), rows)
        by_role = {(row.staff_id, row.staff_type): row for row in response.hours}
        self.assertEqual((by_role[(31, "DRIVER")].scheduled_seconds, by_role[(31, "DRIVER")].remaining_seconds), (144000, 0))
        self.assertEqual((by_role[(32, "ASSISTANT")].scheduled_seconds, by_role[(32, "ASSISTANT")].remaining_seconds), (216000, 0))
        self.assertEqual(by_role[(33, "DRIVER")].remaining_seconds, -1)
        self.assertEqual(by_role[(40, "DRIVER")].scheduled_seconds, 3600)
        self.assertEqual(by_role[(40, "ASSISTANT")].scheduled_seconds, 7200)

    def test_cross_midnight_week_and_year_are_clipped_at_half_open_boundaries(self):
        week_start = datetime(2026, 12, 28)
        week_end = datetime(2027, 1, 4)
        rows = (
            duty(31, "DRIVER", week_start - timedelta(seconds=1), week_start + timedelta(seconds=1)),
            duty(31, "DRIVER", datetime(2026, 12, 31, 23, 59), datetime(2027, 1, 1, 0, 1)),
            duty(31, "DRIVER", week_end - timedelta(seconds=1), week_end + timedelta(seconds=1)),
            duty(31, "DRIVER", week_start - timedelta(hours=1), week_start),
            duty(31, "DRIVER", week_end, week_end + timedelta(hours=1)),
            duty(31, "DRIVER", week_start, week_start + timedelta(hours=5), "CANCELLED"),
        )
        response, cursor, _ = self.run_hours(date(2026, 12, 28), rows)
        self.assertEqual(response.hours[0].scheduled_seconds, 122)
        parameters = next(params for sql, params in cursor.executions if sql == mysql.HOURS_SQL)
        self.assertEqual(parameters, (week_start, week_end, week_end, week_start))
        self.assertIn("GREATEST(start_time, %s)", mysql.HOURS_SQL)
        self.assertIn("LEAST(end_time, %s)", mysql.HOURS_SQL)

    def test_unknown_counted_status_is_an_explicit_data_error(self):
        monday = datetime(2026, 9, 14)
        with self.assertRaises(RosterDataError) as caught:
            self.run_hours(date(2026, 9, 14), (duty(31, "DRIVER", monday, monday + timedelta(hours=1), "UNKNOWN"),))
        self.assertEqual(caught.exception.error_code, "ROSTER_DATA_INVALID")

    def test_database_failure_maps_explicitly_and_closes_connection(self):
        cursor = DutyCursor(fail=True)
        connection = DutyConnection(cursor)
        with patch.object(mysql, "get_db", return_value=DutyDatabase(connection)):
            with self.assertRaises(RosterDataError) as caught:
                mysql.MySQLRosterAdapter().hours(date(2026, 9, 14))
        self.assertEqual(caught.exception.error_code, "ROSTER_DATABASE_SCHEMA_MISMATCH")
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        self.assertGreaterEqual(connection.rollbacks, 1)

    def test_reporting_migration_uses_one_duty_view_and_never_joins_order_rows(self):
        migration = Path(__file__).resolve().parents[2] / "database" / "10_roster_reporting.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE SQL SECURITY INVOKER VIEW v_roster_duty_intervals", sql)
        self.assertIn("UNION ALL", sql)
        self.assertEqual(sql.count("FROM roster_assignment"), 2)
        self.assertNotIn("JOIN delivery", sql)
        self.assertNotIn("FROM delivery ", sql)
        self.assertNotIn("delivery ", mysql.HOURS_SQL.lower())


if __name__ == "__main__":
    unittest.main()
