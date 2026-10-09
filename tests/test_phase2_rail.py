"""Actual MySQL + FastAPI rail verification; only a newly created disposable schema.
Run from backend: .venv/Scripts/python.exe -m unittest discover -s tests -p test_phase2_rail.py -v
Reuses the Phase 1 harness and its identity regression tests; never runs startup migrations.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pymysql
from fastapi.testclient import TestClient
import test_phase1_workflow as identity
from app.core.config import settings
from app.core.database import get_db
from app.main import app


class PhaseTwoRail(identity.PhaseOneMySQL):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.REDIS_ENABLED = False
        root = Path(__file__).resolve().parents[2] / 'database'
        files = ['02_views.sql'] + ['feature_4_2_rail_allocation/' + name for name in (
            '01_trip_constraints.sql', '02_allocation_constraints.sql', '03_indexes.sql',
            '04_fn_order_item_space.sql', '05_fn_trip_remaining_capacity.sql',
            '06_trg_capacity_check.sql', '07_trg_trip_capacity_shrink.sql',
            '08_sp_schedule_train_order.sql', '09_v_trip_capacity_usage.sql',
            '12_trg_allocation_quantity_guard.sql', '14_trg_rail_alloc_origin_bi.sql',
            '15_sp_reverse_rail_allocation.sql', '16_trip_hub_guard.sql')]
        with get_db() as conn:
            for filename in files:
                source = re.sub(r'(?im)^USE kandypack_db;\s*', '', (root / filename).read_text(encoding='utf-8'))
                identity.execute_sql(conn, source)
            receipt = (root / '03_procedures.sql').read_text(encoding='utf-8')
            receipt = receipt[receipt.index('DROP PROCEDURE IF EXISTS sp_receive_manifest'):receipt.rindex('DELIMITER ;')]
            identity.execute_sql(conn, 'DELIMITER //\n' + receipt + '\nDELIMITER ;')
            with conn.cursor() as cur:
                cur.execute("INSERT INTO station_store (station_id,city,address) VALUES (100,'Kandy','Test origin')")
            conn.commit()

    def setUp(self):
        super().setUp()
        self.now = datetime.now(ZoneInfo('Asia/Colombo')).replace(tzinfo=None, microsecond=0)

    def fixture(self, quantities=(10,), rates=('0.5',), capacity=(10,), profile_other_route=False):
        _, customer = self.register()
        self.admin()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO station_store (city,address) VALUES ('Colombo','Rail test destination')")
            destination = cur.lastrowid
            cur.execute("INSERT INTO delivery_route (station_id,route_name,max_delivery_time) VALUES (%s,'Rail test route','02:00:00')", (destination,))
            route = cur.lastrowid
            if profile_other_route:
                cur.execute("INSERT INTO station_store (city,address) VALUES ('Galle','Profile destination')")
                other_station = cur.lastrowid
                cur.execute("INSERT INTO delivery_route (station_id,route_name,max_delivery_time) VALUES (%s,'Profile route','02:00:00')", (other_station,))
                cur.execute('UPDATE customer SET route_id=%s WHERE customer_id=%s', (cur.lastrowid, customer['customer_id']))
            cur.execute("""INSERT INTO customer_order (customer_id,order_date,delivery_date,delivery_route_id,delivery_address,recipient_name,recipient_phone)
                           VALUES (%s,%s,%s,%s,'Order destination','Rail customer','0771234567')""",
                        (customer['customer_id'], self.now.date(), (self.now+timedelta(days=8)).date(), route))
            order = cur.lastrowid
            items = []
            for qty, rate in zip(quantities, rates):
                cur.execute("INSERT INTO product (product_name,unit_price,space_consumption_rate) VALUES ('Rail product',100,%s)", (rate,))
                product = cur.lastrowid
                cur.execute('INSERT INTO order_item (order_id,product_id,quantity,unit_price_at_order) VALUES (%s,%s,%s,100)', (order,product,qty))
                items.append(cur.lastrowid)
            trips = []
            for n, cap in enumerate(capacity):
                cur.execute("INSERT INTO train_trip (origin_station_id,destination_station_id,departure_datetime,arrival_datetime,total_capacity) VALUES (100,%s,%s,%s,%s)",
                            (destination,self.now+timedelta(days=1,hours=n*2),self.now+timedelta(days=1,hours=n*2+1),cap))
                trips.append(cur.lastrowid)
            conn.commit()
        return order, trips, items, destination

    def allocate(self, order, trip=None):
        return self.client.post('/api/v1/rail/allocate', json={'order_id':order, **({'trip_id':trip} if trip else {})})

    def state(self, order):
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT status FROM customer_order WHERE order_id=%s', (order,))
            status = cur.fetchone()['status']
            cur.execute('SELECT ra.* FROM rail_allocation ra JOIN order_item oi ON oi.order_item_id=ra.order_item_id WHERE oi.order_id=%s ORDER BY ra.trip_id', (order,))
            return status, list(cur.fetchall())

    def test_rail_single_and_duplicate(self):
        order, trips, _, _ = self.fixture()
        response = self.allocate(order)
        self.assertEqual(response.status_code,200,response.text)
        status, allocations = self.state(order)
        self.assertEqual(status,'SCHEDULED_FOR_RAIL')
        self.assertEqual(sum(a['allocated_quantity'] for a in allocations),10)
        self.assertEqual(allocations[0]['trip_id'],trips[0])
        self.assertEqual(self.allocate(order).status_code,409)
        self.assertEqual(len(self.state(order)[1]),1)

    def test_rail_spillover_rounding_and_views(self):
        order,trips,items,_ = self.fixture(quantities=(30,),rates=('0.3333',),capacity=(2,3,5))
        response=self.allocate(order)
        self.assertEqual(response.status_code,200,response.text)
        status,allocations=self.state(order)
        self.assertEqual(status,'SCHEDULED_MULTI_TRIP')
        self.assertEqual([a['allocated_quantity'] for a in allocations],[6,9,15])
        self.assertEqual([a['trip_id'] for a in allocations],trips)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT fn_order_item_space(%s,1) AS space',(items[0],))
            self.assertEqual(cur.fetchone()['space'],Decimal('0.34'))
            for trip in trips:
                cur.execute('SELECT remaining_space FROM v_trip_capacity_usage WHERE trip_id=%s',(trip,))
                self.assertGreaterEqual(cur.fetchone()['remaining_space'],0)
                cur.execute('SELECT * FROM v_incoming_train_manifests WHERE trip_id=%s',(trip,))
                self.assertTrue(cur.fetchall(), 'Allocated cargo must be visible to station receipt')
        breakdown=self.client.get(f'/api/v1/rail/orders/{order}/allocations')
        self.assertEqual(breakdown.status_code,200,breakdown.text)
        self.assertEqual(len(breakdown.json()['allocations']),3)

    def test_rail_shortage_rolls_back_all_items(self):
        order,_,_,_=self.fixture(quantities=(10,10),rates=('0.5','0.5'),capacity=(6,))
        self.assertEqual(self.allocate(order).status_code,400)
        self.assertEqual(self.state(order),('PENDING_RAIL_SCHEDULING',[]))
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM order_status_history WHERE order_id=%s',(order,))
            self.assertEqual(cur.fetchone()['n'],0)

    def test_rail_manual_uses_order_route_and_history(self):
        order,trips,_,destination=self.fixture(profile_other_route=True)
        suitable=self.client.get(f'/api/v1/rail/orders/{order}/suitable-trips')
        self.assertEqual(suitable.status_code,200,suitable.text)
        self.assertEqual(suitable.json()['destination_station_id'],destination)
        self.assertEqual(self.allocate(order,trips[0]).status_code,200)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT status FROM order_status_history WHERE order_id=%s',(order,))
            self.assertEqual(cur.fetchone()['status'],'SCHEDULED_FOR_RAIL')

    def test_rail_manual_cutoff_empty_and_invalid_quantity(self):
        for qty in (None,0):
            order,trips,_,_=self.fixture(quantities=() if qty is None else (qty,),rates=() if qty is None else ('0.5',))
            self.assertEqual(self.allocate(order,trips[0]).status_code,422)
            self.assertEqual(self.allocate(order).status_code,422)
            self.assertEqual(self.state(order)[1],[])
        order,trips,_,_=self.fixture()
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('UPDATE train_trip SET arrival_datetime=%s WHERE trip_id=%s',(self.now+timedelta(days=10),trips[0]))
            conn.commit()
        self.assertEqual(self.allocate(order,trips[0]).status_code,422)
        self.assertEqual(self.allocate(order).status_code,400)

    def test_rail_hubs_timezone_and_decimal_validation(self):
        _,_,_,dest=self.fixture()
        hubs=self.client.get('/api/v1/rail/stations').json()
        self.assertTrue(any(h['station_id']==100 for h in hubs['origins']))
        start=(self.now+timedelta(days=2)).replace(tzinfo=ZoneInfo('Asia/Colombo'))
        body={'origin_station_id':100,'destination_station_id':dest,'departure_datetime':start.isoformat(),
              'arrival_datetime':(start+timedelta(hours=2)).isoformat(),'total_capacity':'12.34'}
        response=self.client.post('/api/v1/rail/trips',json=body)
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['departure_datetime'],start.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertEqual(self.client.post('/api/v1/rail/trips',json=dict(body,total_capacity='0.001')).status_code,422)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO station_store (city,address) VALUES ('Badulla','Unsupported')")
            bad=cur.lastrowid
            conn.commit()
        self.assertEqual(self.client.post('/api/v1/rail/trips',json=dict(body,destination_station_id=bad)).status_code,422)
        with get_db() as conn,conn.cursor() as cur:
            with self.assertRaises(pymysql.MySQLError):
                cur.execute('INSERT INTO train_trip (origin_station_id,destination_station_id,departure_datetime,arrival_datetime,total_capacity) VALUES (%s,%s,%s,%s,10)',(dest,100,start.replace(tzinfo=None),start.replace(tzinfo=None)+timedelta(hours=1)))

    def test_rail_concurrent_same_order_no_duplicates(self):
        order,_,_,_=self.fixture()
        token=self.client.cookies.get('kandypack_session')
        def submit(_):
            client=TestClient(app,base_url='https://kandypack.lk')
            try:
                return client.post('/api/v1/rail/allocate',headers={'Authorization':f'Bearer {token}'},json={'order_id':order}).status_code
            finally:
                client.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(results),[200,409])
        self.assertEqual(sum(a['allocated_quantity'] for a in self.state(order)[1]),10)

    def clone_order(self, order):
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO customer_order (customer_id,order_date,delivery_date,delivery_route_id,delivery_address,recipient_name,recipient_phone)
                           SELECT customer_id,order_date,delivery_date,delivery_route_id,delivery_address,recipient_name,recipient_phone FROM customer_order WHERE order_id=%s""",(order,))
            second=cur.lastrowid
            cur.execute('INSERT INTO order_item (order_id,product_id,quantity,unit_price_at_order) SELECT %s,product_id,quantity,unit_price_at_order FROM order_item WHERE order_id=%s',(second,order))
            item=cur.lastrowid
            conn.commit()
        return second,item

    def test_rail_competing_orders_no_overbooking(self):
        first,trips,_,_=self.fixture(capacity=(5,))
        second,_=self.clone_order(first)
        token=self.client.cookies.get('kandypack_session')
        def submit(order):
            client=TestClient(app,base_url='https://kandypack.lk')
            try:
                return client.post('/api/v1/rail/allocate',headers={'Authorization':f'Bearer {token}'},json={'order_id':order}).status_code
            finally:
                client.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,[first,second]))
        self.assertEqual(sorted(results),[200,400],results)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT remaining_space FROM v_trip_capacity_usage WHERE trip_id=%s',(trips[0],))
            self.assertEqual(cur.fetchone()['remaining_space'],0)

    def test_rail_stale_snapshot_capacity_guard(self):
        first,trips,_,_=self.fixture(capacity=(5,))
        second,item=self.clone_order(first)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM rail_allocation')
            cur.fetchone()  # establish an older repeatable-read snapshot
            self.assertEqual(self.allocate(first).status_code,200)
            with self.assertRaises(pymysql.MySQLError):
                cur.execute('INSERT INTO rail_allocation (order_item_id,trip_id,allocated_quantity,allocated_space,allocated_by) VALUES (%s,%s,10,5,%s)',(item,trips[0],self.admin_id))

    def test_rail_update_capacity_and_received_manifest(self):
        order,trips,_,_=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        self.assertEqual(self.client.put(f'/api/v1/rail/trips/{trips[0]}',json={'total_capacity':4}).status_code,400)
        self.assertEqual(self.client.patch(f'/api/v1/rail/trips/{trips[0]}/cancel').status_code,409)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE manifest SET status='RECEIVED' WHERE trip_id=%s",(trips[0],))
            conn.commit()
        second,_=self.clone_order(order)
        self.assertEqual(self.allocate(second,trips[0]).status_code,409)
        self.assertEqual(self.allocate(second).status_code,400)
        self.assertEqual(self.client.post(f'/api/v1/rail/orders/{order}/reverse').status_code,409)

    def test_rail_spillover_receipt_advances_only_after_all_trips(self):
        order,trips,_,destination=self.fixture(quantities=(30,),rates=('0.5',),capacity=(5,5,5))
        self.assertEqual(self.allocate(order).status_code,200)
        for index,trip in enumerate(trips):
            with get_db() as conn,conn.cursor() as cur:
                cur.execute("UPDATE train_trip SET status='ARRIVED',departure_datetime=%s,arrival_datetime=%s WHERE trip_id=%s",(self.now-timedelta(hours=2),self.now-timedelta(hours=1),trip))
                conn.commit()
            response=self.client.post('/api/v1/inventory/manifests/receive',json={'station_id':destination,'trip_id':trip})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.state(order)[0],'ARRIVED_AT_STATION_STORE' if index==2 else 'SCHEDULED_MULTI_TRIP')
            self.assertEqual(self.client.post('/api/v1/inventory/manifests/receive',json={'station_id':destination,'trip_id':trip}).status_code,409)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT SUM(stored_quantity) AS qty FROM inventory WHERE station_id=%s',(destination,))
            self.assertEqual(cur.fetchone()['qty'],30)

    def test_rail_stale_snapshot_quantity_guard(self):
        order,trips,items,_=self.fixture(capacity=(20,20))
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM rail_allocation')
            cur.fetchone()
            self.assertEqual(self.allocate(order,trips[0]).status_code,200)
            with self.assertRaises(pymysql.MySQLError):
                cur.execute('INSERT INTO rail_allocation (order_item_id,trip_id,allocated_quantity,allocated_space,allocated_by) VALUES (%s,%s,10,5,%s)',(items[0],trips[1],self.admin_id))

    def test_rail_migration_repeatable_and_preserves_allocations(self):
        order,trips,_,_=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        before=self.state(order)
        with get_db() as conn,conn.cursor() as cur:
            # Simulate the old scheduler's missing manifest before upgrading.
            cur.execute('DELETE FROM manifest WHERE trip_id=%s',(trips[0],))
            conn.commit()
        migration=(Path(__file__).resolve().parents[2]/'database'/'15_phase2_rail_workflow.sql').read_text(encoding='utf-8')
        self.assertNotRegex(migration,r'(?im)^\s*(USE |DROP TABLE|CREATE DATABASE)')
        with get_db() as conn:
            identity.execute_sql(conn,migration)
            identity.execute_sql(conn,migration)
        self.assertEqual(self.state(order),before)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM manifest WHERE trip_id=%s',(trips[0],))
            self.assertEqual(cur.fetchone()['n'],1)
        second,_=self.clone_order(order)
        self.assertEqual(self.allocate(second,trips[0]).status_code,200)

    def test_rail_reversal_releases_capacity(self):
        order,trips,_,_=self.fixture()
        self.assertEqual(self.allocate(order).status_code,200)
        response=self.client.post(f'/api/v1/rail/orders/{order}/reverse')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.state(order),('PENDING_RAIL_SCHEDULING',[]))
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT remaining_space,total_capacity FROM v_trip_capacity_usage WHERE trip_id=%s',(trips[0],))
            row=cur.fetchone()
            self.assertEqual(row['remaining_space'],row['total_capacity'])
        self.assertEqual(self.allocate(order).status_code,200)

    def test_rail_logistics_role_and_mutation_denials(self):
        order,_,_,_=self.fixture()
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO user (name,role,email,password_hash) VALUES ('Manager','LOGISTICS_MGR',%s,%s)",(f'{uuid4().hex}@kandypack.lk',identity.get_password_hash(self.password)))
            actor=cur.lastrowid
            cur.execute('UPDATE user SET force_password_reset=0 WHERE user_id=%s',(actor,))
            conn.commit()
        token=identity.create_access_token({'sub':str(actor)})
        self.assertEqual(self.client.get('/api/v1/rail/orders/pending',headers={'Authorization':f'Bearer {token}'}).status_code,200)
        response=self.client.post('/api/v1/rail/allocate',headers={'Authorization':f'Bearer {token}'},json={'order_id':order})
        self.assertEqual(response.status_code,200,response.text)
        self.register()
        self.assertEqual(self.allocate(order).status_code,403)
