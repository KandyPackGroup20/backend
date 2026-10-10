"""Station integration on a unique disposable schema; includes prior lifecycle tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import test_phase3_roster as roster
import test_phase1_workflow as identity
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import get_db
from app.core.security import create_access_token


class PhaseFourInventory(roster.PhaseThreeRoster):
    def actor(self, role, station=None):
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO user (name,role,email,password_hash,station_id) VALUES (%s,%s,%s,'unused',%s)", (role,role,uuid4().hex+'@kandypack.lk',station if role=='WAREHOUSE_STAFF' else None))
            user = cur.lastrowid
            cur.execute('UPDATE user SET force_password_reset=0 WHERE user_id=%s', (user,))
            if role=='STORE_MGR' and station:
                cur.execute('UPDATE station_store SET manager_id=%s WHERE station_id=%s', (user,station))
            conn.commit()
        return {'Authorization': 'Bearer '+create_access_token({'sub': str(user),'role': role})}

    def stock_fixture(self):
        run, order, _ = self.run_fixture()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT i.* FROM inventory i JOIN delivery_route r ON r.station_id=i.station_id JOIN customer_order o ON o.delivery_route_id=r.route_id WHERE o.order_id=%s', (order,))
            return cur.fetchone()

    def test_station_scoping_every_resource(self):
        stock = self.stock_fixture()
        other = self.stock_fixture()
        for role in ('STORE_MGR','WAREHOUSE_STAFF'):
            headers = self.actor(role, stock['station_id'])
            self.client.cookies.clear()
            self.assertEqual(self.client.get('/api/v1/inventory/stations',headers=headers).json()['stations'][0]['station_id'], stock['station_id'])
            self.assertEqual(self.client.get('/api/v1/inventory/',headers=headers).status_code,200)
            for path in (f"/?station_id={other['station_id']}",f"/bins?station_id={other['station_id']}",
                         f"/manifests?station_id={other['station_id']}", f"/reports/summary?station_id={other['station_id']}", f"/adjustments/{other['inventory_id']}"):
                response=self.client.get('/api/v1/inventory'+path,headers=headers)
                self.assertEqual(response.status_code,403,response.text)
            self.assertEqual(self.client.put(f"/api/v1/inventory/{other['inventory_id']}/bin",headers=headers,json={'location_id':None}).status_code,403)
            self.assertEqual(self.client.post('/api/v1/inventory/adjustments',headers=headers,json=dict(inventory_id=other['inventory_id'],quantity_delta=-1,reason='DAMAGED')).status_code,403)
        unassigned=self.actor('WAREHOUSE_STAFF')
        self.assertEqual(self.client.get('/api/v1/inventory/my-station',headers=unassigned).json()['assigned_station'],None)
        self.assertEqual(self.client.get('/api/v1/inventory/',headers=unassigned).status_code,403)

    def test_intake_rejects_future_empty_and_wrong_destination(self):
        order,trips,_,station=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        self.assertEqual(self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=station,trip_id=trips[0])).json()['detail'],'TRIP_NOT_ARRIVED')
        self.assertEqual(self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=100,trip_id=trips[0])).status_code,409)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO train_trip (origin_station_id,destination_station_id,departure_datetime,arrival_datetime,total_capacity,status) VALUES (100,%s,%s,%s,10,'ARRIVED')",(station,self.now-timedelta(hours=2),self.now-timedelta(hours=1)))
            empty=cur.lastrowid
            cur.execute('INSERT INTO manifest (station_id,trip_id) VALUES (%s,%s)',(station,empty))
            conn.commit()
        r=self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=station,trip_id=empty))
        self.assertEqual(r.json()['detail'],'MANIFEST_EMPTY')
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM inventory WHERE station_id=%s',(station,))
            self.assertEqual(cur.fetchone()['n'],0)

    def test_damage_duplicate_conflict_and_negative_rollback(self):
        stock=self.stock_fixture()
        headers=self.actor('WAREHOUSE_STAFF',stock['station_id']) | {'Idempotency-Key': uuid4().hex}
        self.client.cookies.clear()
        payload=dict(inventory_id=stock['inventory_id'],quantity_delta=-3,reason='DAMAGED')
        for _ in range(2):
            r=self.client.post('/api/v1/inventory/adjustments',headers=headers,json=payload)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['new_stored_quantity'],stock['stored_quantity']-3)
        self.assertEqual(self.client.post('/api/v1/inventory/adjustments',headers=headers,json=payload|dict(quantity_delta=-2)).status_code,409)
        self.assertEqual(self.client.post('/api/v1/inventory/adjustments',headers=headers|{'Idempotency-Key':uuid4().hex},json=payload|dict(quantity_delta=-1000)).status_code,400)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM stock_adjustment WHERE inventory_id=%s',(stock['inventory_id'],))
            self.assertEqual(cur.fetchone()['n'],1)
        report=self.client.get(f"/api/v1/inventory/reports/summary?station_id={stock['station_id']}",headers=headers)
        self.assertEqual(report.status_code,200,report.text)
        self.assertEqual(report.json()['overview']['total_stored_units'],7)
        self.assertEqual(report.json()['overview']['total_damaged_or_lost_units'],3)
        self.assertEqual(report.json()['overview']['total_loss_value_lkr'],300)
        for change in (dict(reason=' '),dict(quantity_delta=0),dict(quantity_delta=1.5)):
            self.assertIn(self.client.post('/api/v1/inventory/adjustments',headers=headers,json=payload|change).status_code,(400,422))

    def test_damage_concurrent_requests_do_not_overdraw(self):
        stock=self.stock_fixture()
        headers=self.actor('WAREHOUSE_STAFF',stock['station_id'])
        def adjust(_):
            with TestClient(app,base_url='https://kandypack.lk') as client:
                return client.post('/api/v1/inventory/adjustments',headers=headers|{'Idempotency-Key':uuid4().hex},json=dict(inventory_id=stock['inventory_id'],quantity_delta=-7,reason='LOST')).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(adjust,range(2))),[200,400])

    def test_bin_noop_and_cross_station_validation(self):
        stock=self.stock_fixture()
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO storage_location (station_id,location_code,location_type) VALUES (%s,'B1','BIN')",(stock['station_id'],))
            own=cur.lastrowid
            cur.execute("INSERT INTO storage_location (station_id,location_code,location_type) VALUES (100,%s,'BIN')",(uuid4().hex,))
            other=cur.lastrowid
            conn.commit()
        path=f"/api/v1/inventory/{stock['inventory_id']}/bin"
        for value in (own,own,None,None):
            self.assertEqual(self.client.put(path,json={'location_id':value}).status_code,200)
        self.assertEqual(self.client.put(path,json={'location_id':other}).status_code,400)

    def test_migration_preserves_stock_and_repeats(self):
        stock=self.stock_fixture()
        source=(Path(__file__).resolve().parents[2]/'database/16_phase4_station_workflow.sql').read_text(encoding='utf-8')
        with get_db() as conn,conn.cursor() as cur:
            # Simulate the pre-Phase-4 shape only inside this generated test schema.
            cur.execute('ALTER TABLE user DROP FOREIGN KEY fk_user_station, DROP COLUMN station_id')
            cur.execute('ALTER TABLE stock_adjustment DROP INDEX uq_stock_adjustment_request, DROP COLUMN request_key')
            for _ in range(2):
                identity.execute_sql(conn,source)
            cur.execute('SELECT stored_quantity FROM inventory WHERE inventory_id=%s',(stock['inventory_id'],))
            self.assertEqual(cur.fetchone()['stored_quantity'],stock['stored_quantity'])

    def test_receipt_concurrent_warehouse_and_atomic_failure(self):
        order,trips,_,station=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE train_trip SET status='ARRIVED',departure_datetime=%s,arrival_datetime=%s WHERE trip_id=%s",(self.now-timedelta(hours=2),self.now-timedelta(hours=1),trips[0]))
            cur.execute("CREATE TRIGGER phase4_fail_history BEFORE INSERT ON order_status_history FOR EACH ROW BEGIN IF NEW.status='ARRIVED_AT_STATION_STORE' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='injected receipt failure'; END IF; END")
            conn.commit()
        try:
            r=self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=station,trip_id=trips[0]))
            self.assertEqual(r.status_code,500)
            with get_db() as conn,conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) n FROM inventory WHERE station_id=%s',(station,))
                self.assertEqual(cur.fetchone()['n'],0)
                cur.execute('SELECT status,received_at FROM manifest WHERE trip_id=%s',(trips[0],))
                self.assertEqual(cur.fetchone(),dict(status='PENDING',received_at=None))
            self.assertEqual(self.state(order)[0],'SCHEDULED_FOR_RAIL')
        finally:
            with get_db() as conn,conn.cursor() as cur:
                cur.execute('DROP TRIGGER phase4_fail_history')
        headers=self.actor('WAREHOUSE_STAFF',station)
        def receive(_):
            with TestClient(app,base_url='https://kandypack.lk') as client:
                return client.post('/api/v1/inventory/manifests/receive',headers=headers,json=dict(station_id=station,trip_id=trips[0])).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(receive,range(2))),[200,409])
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT stored_quantity FROM inventory WHERE station_id=%s',(station,))
            self.assertEqual(cur.fetchone()['stored_quantity'],10)
            cur.execute("SELECT COUNT(*) n FROM order_status_history WHERE order_id=%s AND status='ARRIVED_AT_STATION_STORE'",(order,))
            self.assertEqual(cur.fetchone()['n'],1)

    def test_warehouse_provisioning_assignment_and_damage_sign(self):
        stock=self.stock_fixture()
        payload=dict(name='Warehouse tester',email=uuid4().hex+'@kandypack.lk',role='WAREHOUSE_STAFF',password='StationTest!2026',station_id=stock['station_id'])
        response=self.client.post('/api/v1/auth/users',json=payload)
        self.assertEqual(response.status_code,201,response.text)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT station_id FROM user WHERE user_id=%s',(response.json()['user_id'],))
            self.assertEqual(cur.fetchone()['station_id'],stock['station_id'])
        response=self.client.post('/api/v1/inventory/adjustments',json=dict(inventory_id=stock['inventory_id'],quantity_delta=2,reason='DAMAGED'))
        self.assertEqual(response.status_code,422)

    def test_receipt_never_regresses_cancelled_order(self):
        order,trips,_,station=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE train_trip SET status='ARRIVED',departure_datetime=%s,arrival_datetime=%s WHERE trip_id=%s",(self.now-timedelta(hours=2),self.now-timedelta(hours=1),trips[0]))
            cur.execute("UPDATE customer_order SET status='CANCELLED' WHERE order_id=%s",(order,))
            conn.commit()
        response=self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=station,trip_id=trips[0]))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['detail'],'INVALID_ORDER_STATE')
        self.assertEqual(self.state(order)[0],'CANCELLED')
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM inventory WHERE station_id=%s',(station,))
            self.assertEqual(cur.fetchone()['n'],0)
            cur.execute('UPDATE customer_order SET status=NULL WHERE order_id=%s',(order,))
            conn.commit()
        response=self.client.post('/api/v1/inventory/manifests/receive',json=dict(station_id=station,trip_id=trips[0]))
        self.assertEqual(response.status_code,409,response.text)
        self.assertIsNone(self.state(order)[0])
