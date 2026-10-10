import unittest
from dataclasses import replace
from datetime import timedelta

import test_roster_policy as fixtures
from app.roster.availability import eligible_crew
from app.roster.policy import PolicyStaff


class AvailabilityTests(unittest.TestCase):
    def read(self, history=(), start=None, end=None, **changes):
        roster = fixtures.roster(*history)
        roster = replace(roster, staff=tuple(PolicyStaff(s.staff_id, s.person_id, s.staff_type) for s in roster.staff))
        roster = replace(roster, **changes.pop('roster_changes', {}))
        return eligible_crew(roster, {s.staff_id: str(s.staff_id) for s in roster.staff}, 1, 1,
            start or fixtures.at(20, 10), end or fixtures.at(20, 11), **changes)

    def test_driver_weekly_boundaries_and_over_limit(self):
        for existing in (34.9, 35, 39, 39.001):
            history = fixtures.history(1, fixtures.at(14, 0), fixtures.at(14, 0)+timedelta(hours=existing), driver_id=1)
            result = self.read([history])
            selected = [s for s in result['drivers'] if s['staff_id'] == 1]
            self.assertEqual(bool(selected), existing <= 39)
            if selected:
                week = selected[0]['weeks'][0]
                self.assertAlmostEqual(week['projected_seconds'], int(existing*3600)+3600)
                self.assertEqual(week['limit_seconds'], 144000)

    def test_assistant_weekly_cap(self):
        for existing in (52.9, 53, 59, 60):
            history = fixtures.history(1, fixtures.at(14, 0), fixtures.at(14, 0)+timedelta(hours=existing), assistant_id=2)
            result = self.read([history])
            self.assertEqual(2 in [s['staff_id'] for s in result['assistants']], existing <= 59)

    def test_every_crossing_week_is_considered(self):
        start, end = fixtures.at(20, 23), fixtures.at(21, 1)
        first = fixtures.history(1, fixtures.at(14, 0), fixtures.at(15, 15), driver_id=1)
        second = fixtures.history(2, fixtures.at(22, 0), fixtures.at(23, 15), driver_id=1)
        result = self.read([first, second], start, end)
        weeks = next(s for s in result['drivers'] if s['staff_id'] == 1)['weeks']
        self.assertEqual([w['week_start'] for w in weeks], ['2026-09-14', '2026-09-21'])
        self.assertEqual([w['projected_seconds'] for w in weeks], [144000, 144000])
        longer = second.model_copy(update={'end_time': fixtures.at(23, 16)})
        result = self.read([first, longer], start, end)
        self.assertNotIn(1, [s['staff_id'] for s in result['drivers']])

    def test_overlap_rest_type_inactive_route_and_pair(self):
        duty = fixtures.history(1, fixtures.at(20, 9), fixtures.at(20, 11), driver_id=1, assistant_id=2)
        self.assertNotIn(1, [s['staff_id'] for s in self.read([duty])['drivers']])
        self.assertNotIn(1, [s['staff_id'] for s in self.read([duty], fixtures.at(20, 11), fixtures.at(20, 12))['drivers']])
        second = fixtures.history(2, fixtures.at(20, 11), fixtures.at(20, 12), assistant_id=2)
        result = self.read([duty, second], fixtures.at(20, 12), fixtures.at(20, 13))
        self.assertNotIn(2, [s['staff_id'] for s in result['assistants']])
        result = self.read(assistant_id=9)  # alias of driver's person
        self.assertNotIn(1, [s['staff_id'] for s in result['drivers']])
        staff = (PolicyStaff(1, 101, 'DRIVER', False), PolicyStaff(2, 201, 'CUSTOMER'))
        result = self.read(roster_changes={'staff': staff})
        self.assertEqual(result['drivers'], [])
        self.assertEqual(result['assistants'], [])
        result = self.read(end=fixtures.at(20, 13))  # route max two hours
        self.assertEqual(result['drivers'], [])
        self.assertEqual(result['assistants'], [])

    def test_cancelled_duty_does_not_exclude_staff(self):
        duty = fixtures.history(1, fixtures.at(20, 9), fixtures.at(20, 11), driver_id=1, status='CANCELLED')
        self.assertIn(1, [s['staff_id'] for s in self.read([duty])['drivers']])
