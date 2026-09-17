"""Focused HTTP contract checks for the Session 2C roster assignment route."""

import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.roster import router
from app.core.security import create_access_token
from app.roster.dependencies import get_roster_assignment_repository
from app.roster.repository import AssignmentOperationResult, RosterBusinessRejection, RosterDataError
from app.roster.schemas import Assignment


PREFIX = "/api/v1/roster"
PAYLOAD = {
    "route_id": 11,
    "truck_id": 21,
    "driver_id": 31,
    "assistant_id": 32,
    "start_time": "2026-09-22T09:00:00+05:30",
    "end_time": "2026-09-22T10:00:00+05:30",
}
COLOMBO = timezone(timedelta(hours=5, minutes=30))


def stored_assignment() -> Assignment:
    return Assignment(
        roster_id=501, route_id=11, truck_id=21, driver_id=31, assistant_id=32,
        dispatcher_id=777,
        start_time=datetime(2026, 9, 22, 9, tzinfo=COLOMBO),
        end_time=datetime(2026, 9, 22, 10, tzinfo=COLOMBO),
        duration_seconds=3600, status="SCHEDULED",
        created_at=datetime(2026, 9, 20, 8, tzinfo=COLOMBO),
    )


class FakeAssignmentRepository:
    def __init__(self):
        self.calls = []
        self.result = AssignmentOperationResult(stored_assignment())
        self.error = None

    def assign(self, proposal, *, actor_id, request_key):
        self.calls.append((proposal, actor_id, request_key))
        if self.error:
            raise self.error
        return self.result


class RosterAssignmentApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeAssignmentRepository()
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[get_roster_assignment_repository] = lambda: self.repository
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def auth(self, role="DISPATCHER", *, force_reset=False):
        token = create_access_token({
            "sub": "777", "role": role, "force_password_reset": force_reset,
        }, expires_delta=timedelta(minutes=10))
        return {"Authorization": f"Bearer {token}"}

    def post(self, headers=None, payload=None):
        return self.client.post(
            PREFIX + "/assign", headers=self.auth() if headers is None else headers,
            json=PAYLOAD if payload is None else payload,
        )

    def assert_no_store(self, response):
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_dispatcher_and_superadmin_call_atomic_repository_with_server_actor(self):
        for role in ("DISPATCHER", "SUPERADMIN"):
            with self.subTest(role=role):
                response = self.post({**self.auth(role), "Idempotency-Key": f"api-{role}"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["result_code"], "ROSTER_ASSIGNED")
                self.assertEqual(response.json()["assignment"]["dispatcher_id"], 777)
                self.assertEqual(response.json()["assignment"]["duration_seconds"], 3600)
                self.assertEqual(response.json()["meta"]["data_source"], "mysql")
                self.assert_no_store(response)

        proposal, actor_id, key = self.repository.calls[-1]
        self.assertEqual((proposal.route_id, proposal.truck_id, proposal.driver_id, proposal.assistant_id), (11, 21, 31, 32))
        self.assertEqual(actor_id, 777)
        self.assertEqual(key, "api-SUPERADMIN")

    def test_logistics_manager_and_other_existing_guards_cannot_write(self):
        for headers, expected in (
            ({}, 401),
            (self.auth("CUSTOMER"), 403),
            (self.auth("LOGISTICS_MGR"), 403),
            (self.auth(force_reset=True), 403),
        ):
            with self.subTest(expected=expected):
                response = self.post(headers)
                self.assertEqual(response.status_code, expected)
                self.assert_no_store(response)
        self.assertEqual(self.repository.calls, [])

    def test_only_the_documented_client_fields_are_accepted(self):
        response = self.post(payload={**PAYLOAD, "dispatcher_id": 1, "duration_hours": 1})
        self.assertEqual(response.status_code, 422)
        self.assert_no_store(response)
        self.assertEqual(self.repository.calls, [])

    def test_business_rejections_preserve_code_and_http_category(self):
        cases = (
            ("ROUTE_NOT_FOUND", 404),
            ("DRIVER_STAFF_TYPE_INVALID", 422),
            ("TRUCK_OVERLAP", 409),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                self.repository.error = RosterBusinessRejection(code, f"Readable {code} message")
                response = self.post({**self.auth(), "Idempotency-Key": f"reject-{code}"})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.json()["detail"]["error_code"], code)
                self.assertIn("Readable", response.json()["detail"]["message"])
                self.assert_no_store(response)

    def test_technical_failure_is_an_explicit_service_error(self):
        self.repository.error = RosterDataError("ROSTER_DATABASE_BUSY", "Roster assignment is busy. Retry the request.")
        response = self.post({**self.auth(), "Idempotency-Key": "busy"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error_code"], "ROSTER_DATABASE_BUSY")
        self.assert_no_store(response)

    def test_idempotent_replay_is_returned_without_a_second_api_result_type(self):
        self.repository.result = AssignmentOperationResult(stored_assignment(), replayed=True)
        response = self.post({**self.auth(), "Idempotency-Key": "same-request"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["result_code"], "ROSTER_ASSIGNMENT_REPLAYED")
        self.assertEqual(len(self.repository.calls), 1)
