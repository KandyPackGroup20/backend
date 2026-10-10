"""Real MySQL/FastAPI Phase 3 regression, using only a uniquely created schema.

Includes the inherited identity and rail regressions to verify the handoff.
"""
import os
import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import test_phase2_rail as rail
import test_phase1_workflow as identity
from app.core.database import get_db
from app.core.security import create_access_token
from app.roster.cargo import CargoRepository


class PhaseThreeRoster(rail.PhaseTwoRail):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(__file__).resolve().parents[2] / 'database'
        with get_db() as conn:
            for name in ('09_roster_assignment.sql', '10_roster_reporting.sql'):
                identity.execute_sql(conn, re.sub(r'(?im)^USE kandypack_db;\s*', '', (root / name).read_text(encoding='utf-8')))
            conn.commit()

    def setUp(self):
        super().setUp()
        mode = patch.dict(os.environ, {'ROSTER_DATA_MODE': 'mysql'})
        mode.start()
        self.addCleanup(mode.stop)

    def run_fixture(self, attach=True):
        order, trips, _, station = self.fixture()
        self.assertEqual(self.allocate(order).status_code, 200)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE train_trip SET status='ARRIVED',departure_datetime=%s,arrival_datetime=%s WHERE trip_id=%s", (self.now-timedelta(hours=2), self.now-timedelta(hours=1), trips[0]))
            conn.commit()
        receipt = self.client.post('/api/v1/inventory/manifests/receive', json={'station_id': station, 'trip_id': trips[0]})
        self.assertEqual(receipt.status_code, 200, receipt.text)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT delivery_route_id FROM customer_order WHERE order_id=%s', (order,))
            route = cur.fetchone()['delivery_route_id']
            cur.execute("INSERT INTO truck (plate_number,capacity,capacity_unit) VALUES (%s,1000,'KG')", (uuid4().hex,))
            truck = cur.lastrowid
            crew = []
            for role in ('DRIVER', 'ASSISTANT'):
                cur.execute("INSERT INTO user (name,role,email,password_hash,force_password_reset) VALUES (%s,%s,%s,'unused',0)", (role, role, uuid4().hex+'@kandypack.lk'))
                user = cur.lastrowid
                cur.execute("INSERT INTO delivery_staff (user_id,license_number) VALUES (%s,'TEST')", (user,))
                crew.append(cur.lastrowid)
            conn.commit()
        payload = dict(route_id=route, truck_id=truck, driver_id=crew[0], assistant_id=crew[1],
            start_time='2026-11-02T10:00:00+05:30', end_time='2026-11-02T11:00:00+05:30')
        created = self.client.post('/api/v1/roster/assign', json=payload, headers={'Idempotency-Key': uuid4().hex})
        self.assertEqual(created.status_code, 201, created.text)
        run = created.json()['assignment']['roster_id']
        self.assertEqual(self.state(order)[0], 'ARRIVED_AT_STATION_STORE')
        if attach:
            attached = self.client.put(f'/api/v1/roster/schedules/{run}/orders', json={'order_ids': [order]})
            self.assertEqual(attached.status_code, 200, attached.text)
            self.assertEqual(self.state(order)[0], 'ARRIVED_AT_STATION_STORE')
        return run, order, payload

    def start(self, run):
        return self.client.post(f'/api/v1/roster/schedules/{run}/start')

    def run_state(self, run):
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT status FROM roster_assignment WHERE roster_id=%s', (run,))
            status = cur.fetchone()['status']
            cur.execute('SELECT delivery_status FROM delivery WHERE roster_id=%s', (run,))
            deliveries = [r['delivery_status'] for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) n FROM audit_log WHERE action='START_DELIVERY' AND entity_id=%s", (run,))
            return status, deliveries, cur.fetchone()['n']

    def test_roster_start_replay_and_inventory_unchanged(self):
        run, order, _ = self.run_fixture()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM inventory ORDER BY inventory_id')
            before = cur.fetchall()
        self.assertEqual(self.start(run).status_code, 200)
        self.assertEqual(self.start(run).json()['result_code'], 'DELIVERY_ALREADY_STARTED')
        self.assertEqual(self.run_state(run), ('IN_TRANSIT', ['IN_TRANSIT'], 1))
        self.assertEqual(self.state(order)[0], 'OUT_FOR_DELIVERY')
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM inventory ORDER BY inventory_id')
            self.assertEqual(cur.fetchall(), before)
            cur.execute("SELECT COUNT(*) n FROM order_status_history WHERE order_id=%s AND status='OUT_FOR_DELIVERY'", (order,))
            self.assertEqual(cur.fetchone()['n'], 1)
        loading = self.client.get(f'/api/v1/roster/schedules/{run}/loading-list')
        self.assertEqual(loading.status_code, 200, loading.text)
        self.assertEqual(loading.json()['orders'][0]['order_status'], 'OUT_FOR_DELIVERY')

    def test_roster_concurrent_start_and_dispatcher(self):
        run, order, _ = self.run_fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: CargoRepository().start(run, self.admin_id), range(2)))
        self.assertEqual(sorted(r['result_code'] for r in results), ['DELIVERY_ALREADY_STARTED', 'DELIVERY_STARTED'])
        self.assertEqual(self.run_state(run), ('IN_TRANSIT', ['IN_TRANSIT'], 1))
        run, order, _ = self.run_fixture()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO user (name,role,email,password_hash,force_password_reset) VALUES ('Dispatcher','DISPATCHER',%s,'unused',0)", (uuid4().hex+'@kandypack.lk',))
            dispatcher = cur.lastrowid
            cur.execute('UPDATE user SET force_password_reset=0 WHERE user_id=%s', (dispatcher,))
            conn.commit()
        self.client.cookies.clear()
        response = self.client.post(f'/api/v1/roster/schedules/{run}/start', headers={'Authorization': 'Bearer '+create_access_token({'sub': str(dispatcher), 'role': 'DISPATCHER'})})
        self.assertEqual(response.status_code, 200, response.text)

    def test_roster_empty_invalid_and_no_failed_audit(self):
        run, _, _ = self.run_fixture(attach=False)
        self.assertEqual(self.start(run).status_code, 409)
        run, order, _ = self.run_fixture()
        for status in ('CANCELLED', 'COMPLETED'):
            with get_db() as conn, conn.cursor() as cur:
                cur.execute('UPDATE roster_assignment SET status=%s WHERE roster_id=%s', (status, run))
                conn.commit()
            self.assertEqual(self.start(run).status_code, 409)
            self.assertEqual(self.run_state(run), (status, ['ASSIGNED'], 0))
            self.assertEqual(self.state(order)[0], 'ARRIVED_AT_STATION_STORE')

    def test_roster_late_failure_rolls_back_every_write(self):
        run, order, _ = self.run_fixture()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("CREATE TRIGGER phase3_fail_start BEFORE INSERT ON audit_log FOR EACH ROW BEGIN IF NEW.action='START_DELIVERY' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected test failure'; END IF; END")
            conn.commit()
        try:
            self.assertEqual(self.start(run).status_code, 503)
            self.assertEqual(self.run_state(run), ('SCHEDULED', ['ASSIGNED'], 0))
            self.assertEqual(self.state(order)[0], 'ARRIVED_AT_STATION_STORE')
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM order_status_history WHERE order_id=%s AND status='OUT_FOR_DELIVERY'", (order,))
                self.assertEqual(cur.fetchone()['n'], 0)
        finally:
            with get_db() as conn, conn.cursor() as cur:
                cur.execute('DROP TRIGGER phase3_fail_start')

    def test_roster_receipt_recheck_and_auth(self):
        run, order, _ = self.run_fixture()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE customer_order SET status='SCHEDULED_FOR_RAIL' WHERE order_id=%s", (order,))
            conn.commit()
        self.assertEqual(self.start(run).status_code, 409)
        self.assertEqual(self.run_state(run), ('SCHEDULED', ['ASSIGNED'], 0))
        self.register()  # actual customer session, no mocked role guard
        self.assertEqual(self.start(run).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.start(run).status_code, 401)

    def test_roster_availability_refresh_and_server_rejection(self):
        run, order, p = self.run_fixture()
        def available(start, end):
            response = self.client.get('/api/v1/roster/availability', params=dict(route_id=p['route_id'], truck_id=p['truck_id'],
                driver_id=p['driver_id'], assistant_id=p['assistant_id'], **{'from': start, 'to': end}))
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        overlapping = available(p['start_time'], p['end_time'])
        self.assertEqual(overlapping['drivers'], [])
        self.assertEqual(overlapping['assistants'], [])
        touching = available('2026-11-02T11:00:00+05:30', '2026-11-02T12:00:00+05:30')
        self.assertNotIn(p['driver_id'], [s['staff_id'] for s in touching['drivers']])
        later = available('2026-11-02T12:00:00+05:30', '2026-11-02T13:00:00+05:30')
        selected = next(s for s in later['drivers'] if s['staff_id'] == p['driver_id'])
        self.assertEqual(selected['weeks'][0]['scheduled_seconds'], 3600)
        self.assertEqual(selected['weeks'][0]['projected_seconds'], 7200)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE user u JOIN delivery_staff ds ON ds.user_id=u.user_id SET u.is_active=0 WHERE ds.delivery_staff_id=%s', (p['driver_id'],))
            conn.commit()
        inactive = available('2026-11-02T12:00:00+05:30', '2026-11-02T13:00:00+05:30')
        self.assertNotIn(p['driver_id'], [s['staff_id'] for s in inactive['drivers']])
        submitted = self.client.post('/api/v1/roster/assign', json=p | dict(start_time='2026-11-02T12:00:00+05:30', end_time='2026-11-02T13:00:00+05:30'))
        self.assertEqual(submitted.status_code, 422)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM audit_log WHERE action IN ('ASSIGN_ROSTER','ASSIGN_ORDER_TO_ROSTER','START_DELIVERY') AND outcome<>'ACCEPTED'")
            self.assertEqual(cur.fetchone()['n'], 0)
