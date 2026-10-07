"""Focused, storage-free checks for the provisional kandypack-roster policy."""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.roster.policy import (
    ASSISTANT_WEEKLY_LIMIT_SECONDS,
    DRIVER_WEEKLY_LIMIT_SECONDS,
    AssignmentProposal,
    DemoV1RosterPolicy,
    PolicyRoster,
    RosterPolicyViolation,
    split_colombo_duration,
)
from app.roster.schemas import Assignment, Route, Staff, Truck


COLOMBO = ZoneInfo("Asia/Colombo")


def at(day: int, hour: int, minute: int = 0, *, month: int = 9, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=COLOMBO)


ROUTE = Route(route_id=1, station_id="1", route_name="Test route", max_duration_seconds=2 * 60 * 60)
TRUCKS = (Truck(truck_id=1, station_id=None, plate_number="TRUCK-1", is_active=True),
          Truck(truck_id=2, station_id=None, plate_number="TRUCK-2", is_active=True))
STAFF = (
    Staff(staff_id=1, person_id=101, name="Driver", staff_type="DRIVER"),
    Staff(staff_id=2, person_id=201, name="Assistant", staff_type="ASSISTANT"),
    Staff(staff_id=3, person_id=202, name="Wrong driver", staff_type="ASSISTANT"),
    Staff(staff_id=4, person_id=102, name="Wrong assistant", staff_type="DRIVER"),
    Staff(staff_id=5, person_id=101, name="Driver alias", staff_type="DRIVER"),
    Staff(staff_id=6, person_id=201, name="Assistant alias", staff_type="ASSISTANT"),
    Staff(staff_id=7, person_id=103, name="Other driver", staff_type="DRIVER"),
    Staff(staff_id=8, person_id=203, name="Other assistant", staff_type="ASSISTANT"),
    Staff(staff_id=9, person_id=101, name="Same person assistant", staff_type="ASSISTANT"),
)


def roster(*assignments: Assignment, routes: tuple[Route, ...] = (ROUTE,)) -> PolicyRoster:
    return PolicyRoster(routes=routes, trucks=TRUCKS, staff=STAFF, assignments=assignments)


def proposal(start=at(14, 10), end=at(14, 11), **changes) -> AssignmentProposal:
    values = {"route_id": 1, "truck_id": 1, "driver_id": 1, "assistant_id": 2, "start_time": start, "end_time": end}
    values.update(changes)
    return AssignmentProposal(**values)


def history(
    roster_id: int, start: datetime, end: datetime, *, truck_id: int = 2, driver_id: int = 7,
    assistant_id: int = 8, status: str = "SCHEDULED",
) -> Assignment:
    return Assignment(
        roster_id=roster_id, route_id=1, truck_id=truck_id, driver_id=driver_id,
        assistant_id=assistant_id, dispatcher_id=50, start_time=start, end_time=end,
        duration_seconds=int((end - start).total_seconds()), status=status, created_at=at(13, 12),
    )


class DemoV1RosterPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DemoV1RosterPolicy()

    def assert_violation(self, code: str, current: PolicyRoster, candidate: AssignmentProposal):
        with self.assertRaises(RosterPolicyViolation) as raised:
            self.policy.validate(current, candidate)
        self.assertEqual(raised.exception.code, code)

    def test_policy_is_explicitly_provisional_demo_v1(self):
        self.assertEqual(self.policy.policy_id, "kandypack-roster")
        self.assertEqual(self.policy.validate(roster(), proposal()).duration_seconds, 3600)

    def test_resources_must_exist(self):
        for change, code in (({"route_id": 99}, "ROUTE_NOT_FOUND"), ({"truck_id": 99}, "TRUCK_NOT_FOUND"),
                             ({"driver_id": 99}, "DRIVER_NOT_FOUND"), ({"assistant_id": 99}, "ASSISTANT_NOT_FOUND")):
            with self.subTest(change=change):
                self.assert_violation(code, roster(), proposal(**change))

    def test_staff_types_and_same_person_are_checked(self):
        self.assert_violation("DRIVER_STAFF_TYPE_INVALID", roster(), proposal(driver_id=3))
        self.assert_violation("ASSISTANT_STAFF_TYPE_INVALID", roster(), proposal(assistant_id=4))
        self.assert_violation("DRIVER_ASSISTANT_SAME_PERSON", roster(), proposal(assistant_id=9))

    def test_timestamps_must_be_aware_whole_second_and_ordered(self):
        self.assert_violation("INVALID_ASSIGNMENT_INTERVAL", roster(), proposal(start=datetime(2026, 9, 14, 10), end=at(14, 11)))
        self.assert_violation("INVALID_ASSIGNMENT_INTERVAL", roster(), proposal(start=at(14, 10, 0).replace(microsecond=1), end=at(14, 11)))
        self.assert_violation("INVALID_ASSIGNMENT_INTERVAL", roster(), proposal(start=at(14, 11), end=at(14, 10)))

    def test_route_maximum_is_inclusive_at_the_exact_boundary(self):
        self.assertEqual(self.policy.validate(roster(), proposal(start=at(14, 8), end=at(14, 10))).duration_seconds, 7200)
        self.assert_violation("ROUTE_MAX_DURATION_EXCEEDED", roster(), proposal(start=at(14, 8), end=at(14, 10, 1)))

    def test_half_open_resource_overlap_checks_each_resource(self):
        interval_start, interval_end = at(14, 10), at(14, 11)
        self.assert_violation("TRUCK_OVERLAP", roster(history(1, interval_start, interval_end, truck_id=1)), proposal())
        self.assert_violation("DRIVER_OVERLAP", roster(history(1, interval_start, interval_end, driver_id=1)), proposal())
        self.assert_violation("ASSISTANT_OVERLAP", roster(history(1, interval_start, interval_end, assistant_id=2)), proposal())

    def test_touching_is_not_overlap_when_resources_are_different(self):
        existing = history(1, at(14, 9), at(14, 10), truck_id=1, driver_id=7, assistant_id=8)
        self.assertEqual(self.policy.validate(roster(existing), proposal()).duration_seconds, 3600)

    def test_driver_touching_assignments_are_consecutive_but_one_second_gap_resets(self):
        touching = history(1, at(14, 9), at(14, 10), driver_id=1)
        self.assert_violation("DRIVER_CONSECUTIVE_LIMIT", roster(touching), proposal())
        gap = history(1, at(14, 9), at(14, 10), driver_id=1)
        one_second_later = proposal(start=at(14, 10, 1), end=at(14, 11, 1))
        self.assertEqual(self.policy.validate(roster(gap), one_second_later).duration_seconds, 3600)

    def test_midnight_and_monday_boundaries_do_not_reset_a_touching_chain(self):
        midnight = history(1, at(14, 23), at(15, 0), driver_id=1)
        self.assert_violation("DRIVER_CONSECUTIVE_LIMIT", roster(midnight), proposal(start=at(15, 0), end=at(15, 1)))
        week = history(1, at(20, 23), at(21, 0), driver_id=1)
        self.assert_violation("DRIVER_CONSECUTIVE_LIMIT", roster(week), proposal(start=at(21, 0), end=at(21, 1)))

    def test_driver_checks_history_after_an_insertion_before_existing_assignment(self):
        existing = history(1, at(14, 10), at(14, 11), driver_id=1)
        inserted_before = proposal(start=at(14, 9), end=at(14, 10))
        self.assert_violation("DRIVER_CONSECUTIVE_LIMIT", roster(existing), inserted_before)

    def test_assistant_chain_insertion_between_existing_assignments_checks_full_chain(self):
        first = history(1, at(14, 8), at(14, 9), assistant_id=2)
        third = history(2, at(14, 10), at(14, 11), assistant_id=2)
        inserted_between = proposal(start=at(14, 9), end=at(14, 10))
        self.assert_violation("ASSISTANT_CONSECUTIVE_LIMIT", roster(first, third), inserted_between)

    def test_assistant_limit_is_two_touching_routes(self):
        first = history(1, at(14, 8), at(14, 9), assistant_id=2)
        second = history(2, at(14, 9), at(14, 10), assistant_id=2)
        self.assert_violation("ASSISTANT_CONSECUTIVE_LIMIT", roster(first, second), proposal())

    def test_person_level_rules_cover_staff_aliases(self):
        existing_driver_alias = history(1, at(14, 9), at(14, 10), driver_id=5)
        self.assert_violation("DRIVER_CONSECUTIVE_LIMIT", roster(existing_driver_alias), proposal())

    def test_colombo_week_splitting_handles_monday_boundary_exactly(self):
        allocation = split_colombo_duration(at(20, 23), at(21, 1))
        self.assertEqual(allocation, {
            at(14, 0).date(): 3600,
            at(21, 0).date(): 3600,
        })

    def test_driver_weekly_cap_allows_exact_limit_and_rejects_one_second_over(self):
        exact = history(1, at(14, 0), at(15, 15), driver_id=1)
        self.assertEqual(DRIVER_WEEKLY_LIMIT_SECONDS, 40 * 3600)
        self.assertEqual(self.policy.validate(roster(exact), proposal(start=at(20, 20), end=at(20, 21))).duration_seconds, 3600)
        over = history(1, at(14, 0), at(15, 15), driver_id=1)
        self.assert_violation(
            "DRIVER_WEEKLY_CAP_EXCEEDED", roster(over), proposal(start=at(20, 20), end=at(20, 21, 1)),
        )

    def test_assistant_weekly_cap_rejects_overage(self):
        sixty_hours = history(1, at(14, 0), at(16, 12), assistant_id=2)
        self.assertEqual(ASSISTANT_WEEKLY_LIMIT_SECONDS, 60 * 3600)
        self.assert_violation(
            "ASSISTANT_WEEKLY_CAP_EXCEEDED", roster(sixty_hours), proposal(start=at(20, 20), end=at(20, 21)),
        )

    def test_cancelled_history_is_excluded_from_overlap_chains_and_caps(self):
        cancelled = history(1, at(14, 0), at(17, 0), truck_id=1, driver_id=1, assistant_id=2, status="CANCELLED")
        self.assertEqual(self.policy.validate(roster(cancelled), proposal()).duration_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
