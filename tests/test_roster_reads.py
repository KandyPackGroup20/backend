"""Session 1 checks: real JWT guards and isolated roster data; never MySQL.

Run from backend: python -B -m unittest discover -s tests -p 'test_roster_reads.py' -v
The API client uses httpx in the test environment; no server is started.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pymysql
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.roster import router
from app.core.security import create_access_token
from app.roster import dependencies
from app.roster.dependencies import get_roster_repository
from app.roster.dev_adapter import DevelopmentRosterAdapter
from app.roster.fixtures import make_fixtures
from app.roster.mysql_adapter import MySQLRosterAdapter
from app.roster.repository import RosterDataError
from app.roster.schemas import AssignmentsResponse, CandidatesResponse, MySQLMeta, RosterMeta, parse_instant


PREFIX = "/api/v1/roster"
WEEK = {"from": "2026-09-14T00:00:00+05:30", "to": "2026-09-21T00:00:00+05:30"}


class RosterReadsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "APP_ENV": "development", "ROSTER_DATA_MODE": "dev-memory", "WEB_CONCURRENCY": "1"
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        adapter_patch = patch.object(dependencies, "_adapter", None)
        failure_patch = patch.object(dependencies, "_initialization_failed", False)
        adapter_patch.start()
        failure_patch.start()
        self.addCleanup(adapter_patch.stop)
        self.addCleanup(failure_patch.stop)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def auth(self, role="DISPATCHER", *, cookie=False, force_reset=False, expired=False):
        token = create_access_token(
            {"sub": "777", "role": role, "force_password_reset": force_reset},
            expires_delta=timedelta(minutes=-1 if expired else 10),
        )
        return {"Cookie": f"kandypack_session={token}"} if cookie else {"Authorization": f"Bearer {token}"}

    def get(self, path="/candidates", headers=None, params=None):
        return self.client.get(PREFIX + path, headers=headers or self.auth(), params=params)

    def assert_no_store(self, response):
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_anonymous_invalid_and_expired_sessions_cannot_read_or_write(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}, self.auth(expired=True)):
            for path, method in (("/candidates", "GET"), ("/assignments", "GET"), ("/assign", "POST")):
                with self.subTest(headers=bool(headers), path=path):
                    response = self.client.request(method, PREFIX + path, headers=headers, params=WEEK)
                    self.assertEqual(response.status_code, 401)
                    self.assert_no_store(response)
        self.assertIsNone(dependencies._adapter)

    def test_cookie_and_bearer_auth_reuse_existing_guards(self):
        for cookie in (False, True):
            response = self.get(headers=self.auth(cookie=cookie))
            self.assertEqual(response.status_code, 200)
            self.assert_no_store(response)
            payload = response.json()
            self.assertEqual(payload["meta"], {
                "data_source": "dev-memory", "policy_id": "kandypack-roster", "policy_confirmed": False,
                "timezone": "Asia/Colombo", "volatile": True, "fixture_week_start": "2026-09-14",
            })
            self.assertEqual(payload["routes"][0]["station_id"], "CMB")
            self.assertEqual(payload["drivers"][0]["staff_type"], "DRIVER")
            self.assertEqual(payload["assistants"][0]["staff_type"], "ASSISTANT")

    def test_only_roster_reader_roles_can_read(self):
        for role in ("DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR"):
            with self.subTest(role=role):
                self.assertEqual(self.get(headers=self.auth(role)).status_code, 200)
                self.assertEqual(self.get("/assignments", headers=self.auth(role), params=WEEK).status_code, 200)
        for role in ("CUSTOMER", "DRIVER", "ASSISTANT", "STORE_MGR", "WAREHOUSE_STAFF", "UNKNOWN"):
            with self.subTest(role=role):
                response = self.get(headers=self.auth(role))
                self.assertEqual(response.status_code, 403)
                self.assertIsInstance(response.json()["detail"], str)
                self.assert_no_store(response)
                self.assertEqual(self.get("/assignments", headers=self.auth(role), params=WEEK).status_code, 403)

    def test_force_reset_blocks_all_roster_paths_before_adapter_access(self):
        for path, method in (("/candidates", "GET"), ("/assignments", "GET"), ("/assign", "POST")):
            response = self.client.request(method, PREFIX + path, headers=self.auth(force_reset=True), params=WEEK)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"]["error_code"], "PASSWORD_RESET_REQUIRED")
            self.assert_no_store(response)
        self.assertIsNone(dependencies._adapter)

    def test_data_mode_requires_explicit_development_configuration(self):
        for config in (
            {"APP_ENV": "", "ROSTER_DATA_MODE": ""},
            {"APP_ENV": "production"}, {"ROSTER_DATA_MODE": "sql"},
            {"APP_ENV": "development", "ROSTER_DATA_MODE": ""},
        ):
            with self.subTest(config=config), patch.dict(os.environ, config):
                response = self.get()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_DATA_DISABLED")
                self.assert_no_store(response)
        self.assertIsNone(dependencies._adapter)

    def test_single_worker_configuration_is_required(self):
        for value in ("2", "0", "invalid", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"WEB_CONCURRENCY": value}):
                response = self.get()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_UNSUPPORTED_CONFIGURATION")
        self.assertIsNone(dependencies._adapter)

    def test_assignment_listing_is_sorted_and_uses_half_open_overlap(self):
        response = self.get("/assignments", params=WEEK)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["roster_id"] for row in response.json()["assignments"]], [1, 2, 3])
        self.assert_no_store(response)
        for start, end, expected in (
            ("2026-09-14T07:00:00+05:30", "2026-09-14T08:00:00+05:30", []),
            ("2026-09-14T10:00:00+05:30", "2026-09-14T11:00:00+05:30", []),
            ("2026-09-14T09:00:00+05:30", "2026-09-14T09:15:00+05:30", [1]),
            ("2026-09-14T02:30:00Z", "2026-09-14T04:30:00Z", [1]),
        ):
            with self.subTest(start=start):
                response = self.get("/assignments", params={"from": start, "to": end})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["roster_id"] for row in response.json()["assignments"]], expected)

    def test_required_aware_whole_second_ordered_windows(self):
        for params in (
            {}, {"from": WEEK["from"]}, {"to": WEEK["to"]},
            {"from": "2026-09-14T08:00:00", "to": WEEK["to"]},
            {"from": "2026-09-14T08:00:00.001+05:30", "to": WEEK["to"]},
            {"from": "not-a-date", "to": WEEK["to"]},
            {"from": "0001-01-01T00:00:00+14:00", "to": WEEK["to"]},
            {"from": WEEK["to"], "to": WEEK["from"]},
            {"from": WEEK["from"], "to": WEEK["from"]},
        ):
            with self.subTest(params=params):
                response = self.get("/assignments", params=params)
                self.assertEqual(response.status_code, 422)
                self.assert_no_store(response)

    def test_empty_injected_data_stays_empty(self):
        empty = DevelopmentRosterAdapter(
            CandidatesResponse(routes=(), trucks=(), drivers=(), assistants=(), meta=RosterMeta()), ()
        )
        self.app.dependency_overrides[get_roster_repository] = lambda: empty
        with patch.object(dependencies, "make_fixtures") as seeder:
            payload = self.get().json()
            self.assertEqual(payload["routes"], [])
            self.assertEqual(payload["drivers"], [])
            self.assertEqual(self.get("/assignments", params=WEEK).json()["assignments"], [])
            seeder.assert_not_called()

    def test_adapter_is_shared_and_returns_immutable_consistent_snapshots(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            adapters = list(executor.map(lambda _: get_roster_repository(), range(16)))
        self.assertTrue(all(item is adapters[0] for item in adapters))
        snapshot = adapters[0].candidates()
        with self.assertRaises(ValidationError):
            snapshot.drivers[0].name = "Changed"
        with self.assertRaises(ValidationError):
            snapshot.drivers = ()
        self.assertEqual(adapters[0].candidates().drivers[0].name, "Demo driver A")

    def test_initialization_failure_is_reported_without_automatic_reseed(self):
        with patch.object(dependencies, "make_fixtures", side_effect=RuntimeError("fixture failure")) as seeder:
            with self.assertLogs("app.roster.dependencies", level="ERROR"):
                first = self.get()
            second = self.get()
            self.assertEqual(first.status_code, 503)
            self.assertEqual(second.status_code, 503)
            self.assertEqual(second.json()["detail"]["error_code"], "ROSTER_DATA_UNAVAILABLE")
            self.assertEqual(seeder.call_count, 1)

    def test_read_failure_is_reported_without_replacing_the_adapter(self):
        adapter = get_roster_repository()
        for method, path, params in (("candidates", "/candidates", None), ("assignments", "/assignments", WEEK)):
            with self.subTest(method=method), patch.object(adapter, method, side_effect=RuntimeError("storage failure")):
                with patch.object(dependencies, "make_fixtures") as seeder, self.assertLogs("app.api.v1.roster", level="ERROR"):
                    first, second = self.get(path, params=params), self.get(path, params=params)
                for response in (first, second):
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_DATA_UNAVAILABLE")
                    self.assert_no_store(response)
                seeder.assert_not_called()
                self.assertIs(get_roster_repository(), adapter)

    def test_assignment_creation_remains_guarded_and_unavailable(self):
        before = self.get("/assignments", params=WEEK).json()
        for role in ("DISPATCHER", "SUPERADMIN"):
            response = self.client.post(PREFIX + "/assign", headers=self.auth(role), json={
                "route_id": 1, "truck_id": 1, "driver_id": 1, "assistant_id": 3,
                "dispatcher_id": 1234, "duration_hours": 1,
                "start_time": WEEK["from"], "end_time": WEEK["to"],
            })
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_ASSIGNMENT_NOT_IMPLEMENTED")
            self.assert_no_store(response)
        for role in ("LOGISTICS_MGR", "CUSTOMER"):
            self.assertEqual(self.client.post(PREFIX + "/assign", headers=self.auth(role), json={}).status_code, 403)
        self.assertEqual(self.get("/assignments", params=WEEK).json(), before)
        for path in ("/hours?week_start=2026-09-14", "/audit"):
            response = self.get(path)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_REPORTING_REQUIRES_MYSQL")
            self.assert_no_store(response)

    def test_roster_paths_never_attempt_a_mysql_connection(self):
        # The application database helper uses pymysql.connect. A regression to
        # the starter SQL path must fail this check before it touches a database.
        with patch("pymysql.connect", side_effect=AssertionError("MySQL is forbidden in roster read tests")) as connect:
            self.assertEqual(self.get().status_code, 200)
            self.assertEqual(self.get("/assignments", params=WEEK).status_code, 200)
            self.assertEqual(self.client.post(PREFIX + "/assign", headers=self.auth(), json={}).status_code, 503)
            connect.assert_not_called()

    def test_fixture_assignments_reference_catalog_and_have_consistent_durations(self):
        catalog, rows = make_fixtures()
        routes = {route.route_id: route for route in catalog.routes}
        for row in rows:
            self.assertIn(row.truck_id, {truck.truck_id for truck in catalog.trucks})
            self.assertIn(row.driver_id, {staff.staff_id for staff in catalog.drivers})
            self.assertIn(row.assistant_id, {staff.staff_id for staff in catalog.assistants})
            self.assertEqual(row.duration_seconds, int((row.end_time - row.start_time).total_seconds()))
            self.assertLessEqual(row.duration_seconds, routes[row.route_id].max_duration_seconds)
        reversed_adapter = DevelopmentRosterAdapter(catalog, tuple(reversed(rows)))
        result = reversed_adapter.assignments(parse_instant(WEEK["from"]), parse_instant(WEEK["to"]))
        self.assertEqual([row.roster_id for row in result.assignments], [1, 2, 3])

    def test_mysql_mode_allows_production_and_multiple_workers_without_fixtures(self):
        empty = CandidatesResponse(routes=(), trucks=(), drivers=(), assistants=(), meta=MySQLMeta())
        with patch.dict(os.environ, {"APP_ENV": "production", "ROSTER_DATA_MODE": "mysql", "WEB_CONCURRENCY": "4"}):
            with patch.object(dependencies, "make_fixtures") as fixtures:
                self.assertIsInstance(get_roster_repository(), MySQLRosterAdapter)
                with patch.object(MySQLRosterAdapter, "candidates", return_value=empty):
                    response = self.get()
                with patch.object(MySQLRosterAdapter, "assignments", return_value=AssignmentsResponse(assignments=(), meta=MySQLMeta())):
                    listing = self.get("/assignments", params=WEEK)
                fixtures.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["data_source"], "mysql")
        self.assertIsNone(response.json()["meta"]["fixture_week_start"])
        self.assertEqual(listing.json()["assignments"], [])
        self.assert_no_store(response)
        self.assert_no_store(listing)
        self.assertIsNone(dependencies._adapter)

    def test_mysql_failures_have_sanitized_distinct_errors_and_never_fall_back(self):
        for errno, code in ((1045, "ROSTER_DATABASE_ACCESS_DENIED"), (1142, "ROSTER_DATABASE_ACCESS_DENIED"),
                            (1054, "ROSTER_DATABASE_SCHEMA_MISMATCH"), (1146, "ROSTER_DATABASE_SCHEMA_MISMATCH"),
                            (2003, "ROSTER_DATA_UNAVAILABLE")):
            with self.subTest(errno=errno), patch.dict(os.environ, {"ROSTER_DATA_MODE": "mysql"}):
                with patch("pymysql.connect", side_effect=pymysql.OperationalError(errno, "secret SQL host password")) as connect:
                    with patch.object(dependencies, "make_fixtures") as fixtures, self.assertLogs("app.api.v1.roster", level="ERROR"):
                        for path, params in (("/candidates", None), ("/assignments", WEEK)):
                            response = self.get(path, params=params)
                            self.assertEqual(response.status_code, 503)
                            self.assertEqual(response.json()["detail"]["error_code"], code)
                            self.assertNotIn("secret", response.text)
                            self.assert_no_store(response)
                    self.assertEqual(connect.call_count, 2)
                    fixtures.assert_not_called()

    def test_mysql_guarded_requests_and_invalid_write_do_not_connect(self):
        with patch.dict(os.environ, {"ROSTER_DATA_MODE": "mysql"}):
            with patch("pymysql.connect", side_effect=AssertionError("Guarded request connected")) as connect:
                self.assertEqual(self.get(headers=self.auth("CUSTOMER")).status_code, 403)
                self.assertEqual(self.get(headers=self.auth(force_reset=True)).status_code, 403)
                self.assertEqual(self.client.get(PREFIX + "/candidates").status_code, 401)
                # Session 2 enables MySQL writes; malformed input is rejected before database access.
                response = self.client.post(PREFIX + "/assign", headers=self.auth(), json={})
                self.assertEqual(response.status_code, 422)
                connect.assert_not_called()

    def test_mysql_unrepresentable_query_bounds_are_input_errors(self):
        with patch.dict(os.environ, {"ROSTER_DATA_MODE": "mysql"}):
            with patch("pymysql.connect") as connect:
                for start, end in (("0999-12-01T00:00:00Z", "0999-12-02T00:00:00Z"),
                                   ("9999-12-31T20:00:00Z", "9999-12-31T21:00:00Z")):
                    response = self.get("/assignments", params={"from": start, "to": end})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"]["error_code"], "INVALID_ROSTER_WINDOW")
                    self.assert_no_store(response)
                connect.assert_not_called()

    def test_mysql_adapter_maps_verified_catalog_rows_and_closes_read_snapshot(self):
        cursor = _RosterCursor([
            [{"route_id": 1, "station_id": 17, "route_name": "Colombo", "max_duration_seconds": 90061}],
            [{"truck_id": 1, "plate_number": "WP-CA-1234", "is_active": 1, "capacity": "3500.00", "capacity_unit": None}],
            [
                {"staff_id": 2, "person_id": 20, "name": "Driver", "staff_type": "DRIVER"},
                {"staff_id": 3, "person_id": 21, "name": "Assistant", "staff_type": "ASSISTANT"},
            ],
        ])
        connection = _RosterConnection(cursor)
        with patch("app.roster.mysql_adapter.get_db", return_value=_RosterDatabaseContext(connection)):
            response = MySQLRosterAdapter().candidates()
        self.assertEqual(response.routes[0].station_id, "17")
        self.assertEqual(response.routes[0].max_duration_seconds, 90061)
        self.assertIsNone(response.trucks[0].station_id)
        self.assertEqual([staff.staff_id for staff in response.drivers], [2])
        self.assertEqual([staff.staff_id for staff in response.assistants], [3])
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        sql = "\n".join(statement for statement, _ in cursor.executions)
        self.assertIn("INNER JOIN `user`", sql)
        self.assertNotIn("work_hours", sql)
        self.assertIn(("+05:30",), [params for _, params in cursor.executions])

    def test_mysql_adapter_returns_explicit_data_error_and_closes_on_malformed_rows(self):
        cursor = _RosterCursor([
            [{"route_id": 1, "station_id": 17, "route_name": "Colombo", "max_duration_seconds": 3600}],
            [],
            [{"staff_id": 2, "person_id": 20, "name": "Not eligible", "staff_type": "UNKNOWN"}],
        ])
        connection = _RosterConnection(cursor)
        with patch("app.roster.mysql_adapter.get_db", return_value=_RosterDatabaseContext(connection)):
            with self.assertRaisesRegex(RosterDataError, "Stored roster data") as raised:
                MySQLRosterAdapter().candidates()
        self.assertEqual(raised.exception.error_code, "ROSTER_DATA_INVALID")
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.rollbacks, 1)

    def test_mysql_assignment_history_uses_no_staff_join_and_preserves_inactive_ids(self):
        cursor = _RosterCursor([[
            {
                "roster_id": 5, "route_id": 1, "truck_id": 1, "driver_id": 98, "assistant_id": 99,
                "dispatcher_id": 20, "start_time": datetime(2026, 9, 14, 8),
                "end_time": datetime(2026, 9, 14, 9), "status": "SCHEDULED",
                "created_at": datetime(2026, 9, 13, 10),
            },
        ]])
        connection = _RosterConnection(cursor)
        start = datetime(2026, 9, 14, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        end = start + timedelta(days=7)
        with patch("app.roster.mysql_adapter.get_db", return_value=_RosterDatabaseContext(connection)):
            response = MySQLRosterAdapter().assignments(start, end)
        self.assertEqual((response.assignments[0].driver_id, response.assignments[0].assistant_id), (98, 99))
        query, parameters = cursor.executions[-1]
        self.assertIn("start_time < %s AND end_time > %s", query)
        self.assertNotIn("JOIN", query)
        self.assertNotIn("delivery", query.lower())
        self.assertEqual(parameters, (datetime(2026, 9, 21), datetime(2026, 9, 14)))
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)


class _RosterCursor:
    def __init__(self, result_sets):
        self.result_sets = iter(result_sets)
        self.current = []
        self.executions = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))
        if statement.lstrip().upper().startswith("SELECT"):
            self.current = next(self.result_sets)

    def fetchall(self):
        return self.current


class _RosterConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollbacks += 1


class _RosterDatabaseContext:
    """Matches get_db cleanup behavior without opening a live database connection."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            self.connection.rollback()
        self.connection.closed = True


if __name__ == "__main__":
    unittest.main()
