"""Opt-in real InnoDB/API tests; never connects to the application's configured DB.

ROSTER_MYSQL_TEST_PORT must identify the isolated instance documented in database/README.md.
Only the connection factory is redirected; SQL, commits, policy, JWT guards and routes are real.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pymysql

from app.core import database
from app.core.security import create_access_token
from app.main import app
from app.roster.mysql_adapter import (
    INSERT_ASSIGNMENT_SQL, INSERT_AUDIT_SQL, UPDATE_WORK_HOURS_SQL, MySQLRosterAdapter,
    _insert_assignment, _RequestKeyRace,
)
from app.roster.policy import AssignmentProposal
from app.roster.repository import RosterBusinessRejection, RosterDataError, RosterIdempotencyConflict


spec = importlib.util.spec_from_file_location(
    "roster_schema_tests", Path(__file__).resolve().parents[2] / "database/test_roster_audit_schema.py",
)
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)
ZONE = ZoneInfo("Asia/Colombo")
PAYLOAD = dict(route_id=1, truck_id=1, driver_id=1, assistant_id=4,
               start_time="2026-09-22T09:00:00+05:30", end_time="2026-09-22T10:00:00+05:30")


def proposal():
    return AssignmentProposal(route_id=1, truck_id=1, driver_id=1, assistant_id=4,
                              start_time=datetime(2026, 9, 22, 9, tzinfo=ZONE),
                              end_time=datetime(2026, 9, 22, 10, tzinfo=ZONE))


@unittest.skipUnless(os.environ.get("ROSTER_MYSQL_TEST_PORT"), "isolated MySQL opt-in is not set")
class LiveRosterAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema.rebuild("standalone")

    def setUp(self):
        self.factory = patch.object(database, "get_db_connection", schema.isolated_connection)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.mode = patch.dict(os.environ, {"ROSTER_DATA_MODE": "mysql"})
        self.mode.start()
        self.addCleanup(self.mode.stop)
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER IF EXISTS test_fail_roster_audit")
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                for table in ("audit_log", "delivery", "roster_assignment"):
                    cursor.execute("TRUNCATE TABLE " + table)  # fixed test-table allowlist
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                cursor.execute("UPDATE delivery_staff SET work_hours=0")
            connection.commit()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    @staticmethod
    def auth(actor=3, role="DISPATCHER", reset=False):
        token = create_access_token({"sub": str(actor), "role": role, "force_password_reset": reset},
                                    expires_delta=timedelta(minutes=10))
        return {"Authorization": "Bearer " + token}

    def post(self, key, payload=None, *, actor=3):
        return self.client.post("/api/v1/roster/assign", json=payload or PAYLOAD,
                                headers=self.auth(actor) | {"Idempotency-Key": key})

    def state(self):
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS n FROM roster_assignment")
                assignments = cursor.fetchone()["n"]
                cursor.execute("SELECT COUNT(*) AS n FROM audit_log")
                audits = cursor.fetchone()["n"]
                cursor.execute("SELECT delivery_staff_id,work_hours FROM delivery_staff ORDER BY delivery_staff_id")
                hours = {row["delivery_staff_id"]: row["work_hours"] for row in cursor.fetchall()}
        return assignments, audits, hours

    def assert_counts(self, assignments, audits, increments):
        actual_assignments, actual_audits, hours = self.state()
        self.assertEqual((actual_assignments, actual_audits), (assignments, audits))
        self.assertEqual(hours, {staff: Decimal(increments.get(staff, 0)) for staff in hours})

    def test_accepted_rejected_replays_conflicts_and_reporting(self):
        accepted = self.post("accepted")
        self.assertEqual(accepted.status_code, 201, accepted.text)
        roster_id = accepted.json()["assignment"]["roster_id"]
        for payload in (PAYLOAD, PAYLOAD | {"start_time": "2026-09-22T03:30:00Z", "end_time": "2026-09-22T04:30:00Z"}):
            replay = self.post("accepted", payload)
            self.assertEqual(replay.status_code, 201, replay.text)
            self.assertEqual(replay.json()["result_code"], "ROSTER_ASSIGNMENT_REPLAYED")
            self.assertEqual(replay.json()["assignment"]["roster_id"], roster_id)
        rejected = self.post("rejected")
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["error_code"], "TRUCK_OVERLAP")
        self.assertEqual(self.post("rejected").json()["detail"]["error_code"], "TRUCK_OVERLAP")
        for key in ("accepted",):
            for actor, payload in ((3, PAYLOAD | {"truck_id": 2}), (1, PAYLOAD)):
                conflict = self.post(key, payload, actor=actor)
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["detail"]["error_code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.assert_counts(1, 1, {1: 1, 4: 1})
        audit = self.client.get("/api/v1/roster/audit", headers=self.auth())
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertEqual(audit.headers["cache-control"], "no-store")
        attempts = audit.json()["attempts"]
        self.assertEqual([row["outcome"] for row in attempts], ["ACCEPTED"])
        self.assertIsNone(attempts[0]["reason_code"])
        self.assertIsNone(attempts[0]["policy_id"])
        self.assertEqual(attempts[0]["assignment_id"], roster_id)
        self.assertEqual(attempts[0]["attempted_duration_seconds"], 3600)
        self.assertEqual(attempts[0]["request_key"], "accepted")
        hours = self.client.get("/api/v1/roster/hours?week_start=2026-09-21", headers=self.auth())
        self.assertEqual(hours.status_code, 200, hours.text)
        self.assertEqual([row["scheduled_seconds"] for row in hours.json()["hours"]], [3600, 3600])
        report = self.client.get("/api/v1/reports/audit-logs")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(len(report.json()["audit_logs"]), 1)
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT entity_name,entity_id,roster_id FROM audit_log ORDER BY audit_id")
                self.assertEqual(cursor.fetchall(), [
                    {"entity_name": "roster_assignment", "entity_id": roster_id, "roster_id": roster_id},
                ])
                with self.assertRaises(pymysql.IntegrityError):
                    cursor.execute("DELETE FROM roster_assignment WHERE roster_id=%s", (roster_id,))

    def test_missing_resources_return_errors_without_persistence(self):
        for field, code in (("route_id", "ROUTE_NOT_FOUND"), ("truck_id", "TRUCK_NOT_FOUND"),
                            ("driver_id", "DRIVER_NOT_FOUND"), ("assistant_id", "ASSISTANT_NOT_FOUND")):
            response = self.post(field, PAYLOAD | {field: 99999})
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["detail"]["error_code"], code)
        self.assert_counts(0, 0, {})
        self.assertEqual(self.client.get("/api/v1/roster/audit", headers=self.auth()).status_code, 200)

    def test_api_validation_and_authentication_do_not_write_attempts(self):
        for changes in ({"end_time": PAYLOAD["start_time"]}, {"end_time": "2026-09-22T08:00:00+05:30"},
                        {"start_time": "bad"}, {"start_time": "2026-09-22T09:00:00"},
                        {"start_time": "2026-09-22T09:00:00.001+05:30"}):
            self.assertEqual(self.post("invalid", PAYLOAD | changes).status_code, 422)
        for headers, expected in (({}, 401), (self.auth(role="CUSTOMER"), 403), (self.auth(reset=True), 403)):
            response = self.client.post("/api/v1/roster/assign", json=PAYLOAD, headers=headers)
            self.assertEqual(response.status_code, expected)
        self.assert_counts(0, 0, {})

    def test_shared_and_legacy_rows_are_compatible_and_filtered_by_action(self):
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                for action, outcome, entity in (("OTHER_FEATURE", "CUSTOM", "roster_assignment"),
                                                 ("ASSIGN_ROSTER", "SUCCESS", "roster_assignment"),
                                                 ("REJECTED_CHECK_A_OVERLAP", "FAILURE", "roster_assignment")):
                    cursor.execute("INSERT INTO audit_log(user_id,action,entity_id,outcome,entity_name) VALUES (3,%s,1,%s,%s)",
                                   (action, outcome, entity))
            connection.commit()
        response = self.client.get("/api/v1/roster/audit?limit=100", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        attempts = response.json()["attempts"]
        self.assertEqual(attempts, [])
        self.assertEqual(len(self.client.get("/api/v1/reports/audit-logs").json()["audit_logs"]), 3)

    def test_real_audit_insert_failure_rolls_back_acceptance_but_does_not_mask_rejection(self):
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TRIGGER test_fail_roster_audit BEFORE INSERT ON audit_log FOR EACH ROW "
                               "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled audit failure'")
        try:
            accepted_failure = self.post("fail-accepted")
            self.assertEqual(accepted_failure.status_code, 503)
            self.assertEqual(accepted_failure.json()["detail"]["error_code"], "ROSTER_ASSIGNMENT_FAILED")
            self.assert_counts(0, 0, {})
            rejected_failure = self.post("fail-rejected", PAYLOAD | {"route_id": 99999})
            self.assertEqual(rejected_failure.status_code, 404)
            self.assertEqual(rejected_failure.json()["detail"]["error_code"], "ROUTE_NOT_FOUND")
            self.assert_counts(0, 0, {})
        finally:
            with schema.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DROP TRIGGER test_fail_roster_audit")
        self.assertEqual(self.post("fail-accepted").status_code, 201)
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def parallel(self, items):
        barrier = Barrier(len(items))
        def run(key, item, actor):
            barrier.wait(timeout=10)
            try:
                result = MySQLRosterAdapter().assign(item, actor_id=actor, request_key=key)
                return "replay" if result.replayed else "accepted"
            except (RosterBusinessRejection, RosterDataError, RosterIdempotencyConflict) as exc:
                return exc.error_code
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futures = [pool.submit(run, *item) for item in items]
            return [future.result(timeout=20) for future in futures]

    def test_simultaneous_same_key_accepts_once(self):
        results = self.parallel([("same", proposal(), 3), ("same", proposal(), 3)])
        self.assertCountEqual(results, ["accepted", "replay"])
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_simultaneous_same_key_rejections_store_nothing(self):
        item = replace(proposal(), route_id=99999)
        self.assertEqual(self.parallel([("reject", item, 3), ("reject", item, 3)]),
                         ["ROUTE_NOT_FOUND", "ROUTE_NOT_FOUND"])
        self.assert_counts(0, 0, {})

    def test_real_assignment_key_collision_recovers_committed_acceptance(self):
        # Exercise the real unique constraint/helper on independent connections.
        # Do not place an insertion barrier behind the policy's shared row locks.
        insert_started = Event()
        item = proposal()
        local_start = item.start_time.replace(tzinfo=None)
        local_end = item.end_time.replace(tzinfo=None)
        params = (1, 1, 1, 4, 3, local_start, local_end, "SCHEDULED", "collision")
        def loser():
            with schema.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    connection.begin()
                    insert_started.set()
                    try:
                        _insert_assignment(cursor, params)
                    except _RequestKeyRace:
                        return MySQLRosterAdapter()._recover_duplicate(
                            connection, cursor, "collision", ZONE, item, 3, local_start, local_end,
                        )
                    self.fail("Expected the real assignment-key unique constraint")
        with schema.isolated_connection() as winner:
            with winner.cursor() as cursor:
                winner.begin()
                _insert_assignment(cursor, params)
                roster_id = cursor.lastrowid
                cursor.execute(UPDATE_WORK_HOURS_SQL, (Decimal("1.00"), 1, 4))
                cursor.execute(INSERT_AUDIT_SQL, (3, "ASSIGN_ROSTER", roster_id, "ACCEPTED", "roster_assignment", roster_id))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(loser)
                self.assertTrue(insert_started.wait(10))
                winner.commit()
                result = future.result(timeout=15)
                self.assertTrue(result.replayed)
                self.assertEqual(result.assignment.roster_id, roster_id)
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_simultaneous_distinct_keys_conflict_on_resources(self):
        self.assertCountEqual(self.parallel([("one", proposal(), 3), ("two", proposal(), 3)]),
                              ["accepted", "TRUCK_OVERLAP"])
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_simultaneous_same_key_different_data_conflicts(self):
        self.assertCountEqual(self.parallel([("one", proposal(), 3), ("one", replace(proposal(), truck_id=2), 3)]),
                              ["accepted", "IDEMPOTENCY_KEY_CONFLICT"])
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_rejection_rollback_gap_observes_committed_acceptance(self):
        reached_gap, winner_done = Event(), Event()
        class PausedRejection(MySQLRosterAdapter):
            def _replay_after_rejection(inner, *args):
                reached_gap.set()
                if not winner_done.wait(10):
                    raise AssertionError("Winner did not commit during rollback gap")
                return super()._replay_after_rejection(*args)
        item = replace(proposal(), route_id=99999)
        def loser():
            with self.assertRaises(RosterIdempotencyConflict) as caught:
                PausedRejection().assign(item, actor_id=3, request_key="gap")
            return caught.exception.error_code
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(loser)
            self.assertTrue(reached_gap.wait(10))
            try:
                MySQLRosterAdapter().assign(proposal(), actor_id=3, request_key="gap")
            finally:
                winner_done.set()
            self.assertEqual(future.result(timeout=15), "IDEMPOTENCY_KEY_CONFLICT")
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_lost_commit_response_is_resolved_by_explicit_replay(self):
        real_factory = schema.isolated_connection
        class LostResponse:
            def __init__(inner):
                inner.real = real_factory()
            def __getattr__(inner, name):
                return getattr(inner.real, name)
            def commit(inner):
                inner.real.commit()
                raise pymysql.OperationalError(2013, "controlled lost commit response")
        with patch.object(database, "get_db_connection", LostResponse):
            response = self.post("lost")
        self.assertEqual(response.status_code, 503)
        self.assert_counts(1, 1, {1: 1, 4: 1})
        self.assertEqual(self.post("lost").json()["result_code"], "ROSTER_ASSIGNMENT_REPLAYED")
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_rejected_key_can_succeed_after_the_blocking_condition_changes(self):
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE truck SET is_active=0 WHERE truck_id=1")
            connection.commit()
        response = self.post("retry")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["error_code"], "TRUCK_INACTIVE")
        self.assert_counts(0, 0, {})
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE truck SET is_active=1 WHERE truck_id=1")
            connection.commit()
        self.assertEqual(self.post("retry").status_code, 201)
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_rejected_key_can_be_reused_with_changed_request_data(self):
        self.assertEqual(self.post("changed", PAYLOAD | {"route_id": 99999}).status_code, 404)
        self.assert_counts(0, 0, {})
        self.assertEqual(self.post("changed").status_code, 201)
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_each_changed_request_field_conflicts_after_acceptance(self):
        self.assertEqual(self.post("identity").status_code, 201)
        changes = {"route_id": 2, "truck_id": 2, "driver_id": 2, "assistant_id": 5,
                   "start_time": "2026-09-22T09:01:00+05:30",
                   "end_time": "2026-09-22T10:01:00+05:30"}
        for field, value in changes.items():
            with self.subTest(field=field):
                response = self.post("identity", PAYLOAD | {field: value})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["error_code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_status_change_preserves_replay_and_audit_request_facts(self):
        self.assertEqual(self.post("status").status_code, 201)
        original = self.client.get("/api/v1/roster/audit", headers=self.auth()).json()["attempts"]
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE roster_assignment SET status='CANCELLED' WHERE request_key='status'")
            connection.commit()
        replay = self.post("status")
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["result_code"], "ROSTER_ASSIGNMENT_REPLAYED")
        self.assertEqual(replay.json()["assignment"]["status"], "CANCELLED")
        self.assertEqual(self.client.get("/api/v1/roster/audit", headers=self.auth()).json()["attempts"], original)
        self.assert_counts(1, 1, {1: 1, 4: 1})

    def test_real_hours_update_failure_rolls_back_assignment_and_key(self):
        with schema.isolated_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TRIGGER test_fail_roster_hours BEFORE UPDATE ON delivery_staff FOR EACH ROW "
                               "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled hours failure'")
        try:
            self.assertEqual(self.post("hours-fail").status_code, 503)
            self.assert_counts(0, 0, {})
        finally:
            with schema.isolated_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DROP TRIGGER test_fail_roster_hours")
        self.assertEqual(self.post("hours-fail").status_code, 201)
        self.assert_counts(1, 1, {1: 1, 4: 1})
