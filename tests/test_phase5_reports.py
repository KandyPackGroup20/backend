"""Real API/MySQL reporting and lifecycle checks; only uniquely created schemas."""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from time import perf_counter

import test_phase4_inventory as inventory
import test_phase1_workflow as identity
from app.core.database import get_db
from app.core.config import settings
from app.api.v1.orders import normalize_status
import seed_kandypack_data as seeder
import pymysql


class PhaseFiveReports(inventory.PhaseFourInventory):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with get_db() as conn:
            identity.execute_sql(conn,(Path(__file__).resolve().parents[2]/'database/17_phase5_reporting.sql').read_text())

    def report(self, path, key):
        r = self.client.get('/api/v1/reports/'+path)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.headers['cache-control'],'no-store')
        return r.json()[key]

    def test_report_authorization_and_parameters(self):
        paths=('quarterly-sales','top-products','rail-capacity-utilisation','workforce-hours','truck-utilisation','station-inventory')
        for path in paths:
            self.assertEqual(self.client.get('/api/v1/reports/'+path).status_code,401)
        self.register()
        for path in paths:
            self.assertEqual(self.client.get('/api/v1/reports/'+path).status_code,403)
        self.client.cookies.clear()
        for role in ('LOGISTICS_MGR','DISPATCHER','STORE_MGR','WAREHOUSE_STAFF'):
            headers=self.actor(role)
            for path in paths:
                self.assertEqual(self.client.get('/api/v1/reports/'+path,headers=headers).status_code,403)
        self.admin()
        for path in ('top-products?top_n=0','top-products?top_n=1%20OR%201=1','workforce-hours?week_start=2026-11-03'):
            self.assertEqual(self.client.get('/api/v1/reports/'+path).status_code,422)

    def test_sales_rollup_historical_prices_and_rank_ties(self):
        order,_,items,_=self.fixture(quantities=(7,7,3),rates=('0.5',)*3)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE customer_order SET order_date='2031-01-01',delivery_date='2031-01-09' WHERE order_id=%s",(order,))
            cur.execute('UPDATE product SET unit_price=999 WHERE product_id IN (SELECT product_id FROM order_item WHERE order_id=%s)',(order,))
            conn.commit()
        rows=self.report('quarterly-sales','quarterly_sales')
        rows=[r for r in rows if r['sales_year']==2031]
        self.assertEqual(sum(r['total_sales'] for r in rows if r['row_level']=='detail'),1700)
        self.assertEqual({r['row_level'] for r in rows},{'detail','route_total','quarter_total','year_total'})
        self.assertEqual(next(r['total_sales'] for r in rows if r['row_level']=='year_total'),1700)
        ranked=[r for r in self.report('top-products?top_n=2','top_products') if r['sales_year']==2031]
        self.assertEqual([r['product_rank'] for r in ranked],[1,1,2])
        self.assertEqual(len([r for r in self.report('top-products','top_products') if r['sales_year']==2031]),2)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE customer_order SET status='CANCELLED' WHERE order_id=%s",(order,))
            conn.commit()
        self.assertFalse([r for r in self.report('quarterly-sales','quarterly_sales') if r['sales_year']==2031])

    def test_capacity_cube_no_join_multiplication(self):
        order,trips,_,station=self.fixture(quantities=(5,5),rates=('0.5','0.5'),capacity=(6,4))
        self.assertEqual(self.allocate(order).status_code,200)
        rows=self.report('rail-capacity-utilisation','rail_capacity_utilisation')
        detail=[r for r in rows if r['station_id']==station and r['row_level']=='detail']
        self.assertEqual(sum(r['total_capacity'] for r in detail),10)
        self.assertEqual(sum(r['allocated_capacity'] for r in detail),5)
        self.assertEqual(next(r['allocated_capacity'] for r in rows if r['station_id']==station and r['row_level']=='station_total'),5)
        self.assertEqual(sum(r['total_capacity'] for r in rows if r['row_level']=='detail'),next(r['total_capacity'] for r in rows if r['row_level']=='grand_total'))
        self.assertEqual(sum(r['total_capacity'] for r in rows if r['row_level']=='month_total'),next(r['total_capacity'] for r in rows if r['row_level']=='grand_total'))

    def test_inventory_receipts_and_adjustments_not_double_counted(self):
        stock=self.stock_fixture()
        for delta,reason in ((-2,'DAMAGED'),(-1,'LOST'),(1,'RECOUNT')):
            self.assertEqual(self.client.post('/api/v1/inventory/adjustments',json=dict(inventory_id=stock['inventory_id'],quantity_delta=delta,reason=reason)).status_code,200)
        row=next(r for r in self.report('station-inventory','station_inventory') if r['inventory_id']==stock['inventory_id'])
        self.assertEqual((row['received_quantity'],row['stored_quantity'],row['adjusted_stock'],row['net_receipts']), (10,8,8,8))
        self.assertEqual((row['positive_adjustments'],row['negative_adjustments'],row['damaged_quantity']), (1,3,2))

    def test_dated_hours_and_truck_cross_month_rollup(self):
        run,order,payload=self.run_fixture()
        # Independent historical boundary fixture; production immutable requests stay intact.
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("INSERT INTO roster_assignment (request_key,route_id,truck_id,driver_id,assistant_id,dispatcher_id,start_time,end_time,status) VALUES (%s,%s,%s,%s,%s,%s,'2027-01-31 23:00:00','2027-02-01 01:00:00','COMPLETED')",(uuid4().hex,payload['route_id'],payload['truck_id'],payload['driver_id'],payload['assistant_id'],self.admin_id))
            cur.execute('UPDATE delivery_staff SET work_hours=999 WHERE delivery_staff_id=%s',(payload['driver_id'],))
            conn.commit()
        for week in ('2027-01-25','2027-02-01'):
            hours=self.report('workforce-hours?week_start='+week,'workforce_hours')
            row=next(r for r in hours if r['delivery_staff_id']==payload['driver_id'])
            self.assertEqual((row['accumulated_hours'],row['weekly_cap'],row['remaining_hours'],row['status_flag']),(1,40,39,'SAFE'))
        truck=[r for r in self.report('truck-utilisation','truck_utilisation') if r['usage_year']==2027 and r['truck_id']==payload['truck_id']]
        self.assertEqual([(r['usage_month'],r['total_operating_hours']) for r in truck],[(1,1),(2,1)])

    def test_report_weekly_warning_boundaries_and_cancelled_duties(self):
        _,_,payload=self.run_fixture()
        for n,(duration,driver_flag,assistant_flag) in enumerate(((35.9,'SAFE','SAFE'),(36,'NEAR_CAP_WARNING','SAFE'),
                (40,'NEAR_CAP_WARNING','SAFE'),(40.1,'OVER_CAP','SAFE'),(54,'OVER_CAP','NEAR_CAP_WARNING'),
                (60,'OVER_CAP','NEAR_CAP_WARNING'),(60.1,'OVER_CAP','OVER_CAP'))):
            start=datetime(2028,1,3)+timedelta(weeks=n)
            with get_db() as conn,conn.cursor() as cur:
                # Imported historical overages must be reported honestly, not hidden by current policy.
                cur.execute("INSERT INTO roster_assignment (request_key,route_id,truck_id,driver_id,assistant_id,dispatcher_id,start_time,end_time,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'COMPLETED')",(uuid4().hex,payload['route_id'],payload['truck_id'],payload['driver_id'],payload['assistant_id'],self.admin_id,start,start+timedelta(hours=duration)))
                conn.commit()
            hours=self.report('workforce-hours?week_start='+start.date().isoformat(),'workforce_hours')
            for role,flag in (('driver',driver_flag),('assistant',assistant_flag)):
                row=next(r for r in hours if r['delivery_staff_id']==payload[role+'_id'])
                self.assertAlmostEqual(row['accumulated_hours'],duration)
                self.assertEqual(row['status_flag'],flag)
        with get_db() as conn,conn.cursor() as cur:
            cur.execute("UPDATE roster_assignment SET status='CANCELLED' WHERE truck_id=%s AND start_time>='2028-01-01'",(payload['truck_id'],))
            conn.commit()
        hours=self.report('workforce-hours?week_start=2028-01-03','workforce_hours')
        self.assertEqual(next(r['accumulated_hours'] for r in hours if r['delivery_staff_id']==payload['driver_id']),0)
        self.assertFalse([r for r in self.report('truck-utilisation','truck_utilisation') if r['usage_year']==2028 and r['truck_id']==payload['truck_id']])

    def test_missing_report_view_fails_without_read_time_migration(self):
        self.admin()
        root=Path(__file__).resolve().parents[2]/'database'
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM customer_order')
            before=cur.fetchone()['n']
            cur.execute('DROP VIEW v_report_station_inventory')
        try:
            with patch('app.core.migrations.run_migrations',side_effect=AssertionError('Report GET must not migrate')):
                response=self.client.get('/api/v1/reports/station-inventory')
                self.assertEqual(response.status_code,503,response.text)
                self.assertEqual(response.headers['cache-control'],'no-store')
        finally:
            with get_db() as conn:
                for _ in range(2):
                    identity.execute_sql(conn,(root/'17_phase5_reporting.sql').read_text())
                identity.execute_sql(conn,(root/'09_management_reports.sql').read_text(encoding='utf-8-sig'))
                with conn.cursor() as cur:
                    cur.execute('SELECT COUNT(*) n FROM customer_order')
                    self.assertEqual(cur.fetchone()['n'],before)

    def test_full_role_separated_lifecycle(self):
        # Use fixture only for catalog, route, and trips. The tested order is placed via API.
        unused,trips,items,station=self.fixture(quantities=(10,),rates=('0.5',),capacity=(2,3))
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('SELECT o.delivery_route_id,i.product_id FROM customer_order o JOIN order_item i ON i.order_id=o.order_id WHERE o.order_id=%s',(unused,))
            catalog=cur.fetchone()
        self.client.cookies.clear()
        _,customer=self.register()
        with get_db() as conn,conn.cursor() as cur:
            cur.execute('UPDATE customer SET route_id=%s WHERE customer_id=%s',(catalog['delivery_route_id'],customer['customer_id']))
            conn.commit()
        payload=dict(destination_hub='CMB',booking_date=(self.now+timedelta(days=8)).date().isoformat(),items=[dict(product_id=catalog['product_id'],quantity=10)])
        for invalid in ('bad-date',(self.now+timedelta(days=6)).date().isoformat()):
            self.assertEqual(self.client.post('/api/v1/orders',json=payload|dict(booking_date=invalid)).status_code,422)
        with patch('app.api.v1.orders.log_and_dispatch_email'):
            created=self.client.post('/api/v1/orders',json=payload)
        self.assertEqual(created.status_code,201,created.text)
        order=created.json()['order_id']
        self.client.cookies.clear()
        logistics=self.actor('LOGISTICS_MGR')
        allocated=self.client.post('/api/v1/rail/allocate',headers=logistics,json={'order_id':order})
        self.assertEqual(allocated.status_code,200,allocated.text)
        self.assertEqual(self.state(order)[0],'SCHEDULED_MULTI_TRIP')
        self.assertEqual([r['allocated_quantity'] for r in self.state(order)[1]],[4,6])
        manager=self.actor('STORE_MGR',station)
        # Simulate physical arrival in test time; all intake/order transitions use real APIs.
        with get_db() as conn,conn.cursor() as cur:
            for trip in trips:
                cur.execute("UPDATE train_trip SET status='ARRIVED',departure_datetime=%s,arrival_datetime=%s WHERE trip_id=%s",(self.now-timedelta(hours=2),self.now-timedelta(hours=1),trip))
            conn.commit()
        for n,trip in enumerate(trips):
            receipt=self.client.post('/api/v1/inventory/manifests/receive',headers=manager,json={'trip_id':trip,'station_id':station})
            self.assertEqual(receipt.status_code,200,receipt.text)
            self.assertEqual(self.state(order)[0],'SCHEDULED_MULTI_TRIP' if n==0 else 'ARRIVED_AT_STATION_STORE')
        # Reuse actual crew/truck fixture, but schedule a separate run for the tested order.
        self.admin()
        _,_,assignment=self.run_fixture()
        assignment |= dict(route_id=catalog['delivery_route_id'],start_time='2026-11-04T10:00:00+05:30',end_time='2026-11-04T11:00:00+05:30')
        self.client.cookies.clear()
        dispatcher=self.actor('DISPATCHER')
        key=uuid4().hex
        response=self.client.post('/api/v1/roster/assign',headers=dispatcher|{'Idempotency-Key':key},json=assignment)
        self.assertEqual(response.status_code,201,response.text)
        run=response.json()['assignment']['roster_id']
        self.assertEqual(self.state(order)[0],'ARRIVED_AT_STATION_STORE')
        self.assertEqual(self.client.put(f'/api/v1/roster/schedules/{run}/orders',headers=dispatcher,json={'order_ids':[order]}).status_code,200)
        self.assertEqual(self.state(order)[0],'ARRIVED_AT_STATION_STORE')
        for _ in range(2):
            self.assertEqual(self.client.post(f'/api/v1/roster/schedules/{run}/start',headers=dispatcher).status_code,200)
        self.assertEqual(self.state(order)[0],'OUT_FOR_DELIVERY')
        self.assertEqual(normalize_status('OUT_FOR_DELIVERY'),'transit')
        self.assertEqual(self.run_state(run),('IN_TRANSIT',['IN_TRANSIT'],1))
        self.admin()
        for endpoint,key in (('quarterly-sales','quarterly_sales'),('top-products','top_products'),('rail-capacity-utilisation','rail_capacity_utilisation'),('workforce-hours?week_start=2026-11-02','workforce_hours'),('truck-utilisation','truck_utilisation'),('station-inventory','station_inventory')):
            self.assertTrue(self.report(endpoint,key))
        stock=next(r for r in self.report('station-inventory','station_inventory') if r['station_id']==station)
        self.assertEqual(stock['stored_quantity'],10)
        self.assertEqual(stock['received_quantity'],10)
        sales=next(r for r in self.report('quarterly-sales','quarterly_sales') if r['route_id']==catalog['delivery_route_id'] and r['row_level']=='detail')
        self.assertEqual(sales['total_quantity'],20)  # fixture order plus the API order
        self.assertEqual(sales['total_sales'],2000)
        rail=next(r for r in self.report('rail-capacity-utilisation','rail_capacity_utilisation') if r['station_id']==station and r['row_level']=='detail')
        self.assertEqual((rail['total_capacity'],rail['allocated_capacity']),(5,5))
        hours=next(r for r in self.report('workforce-hours?week_start=2026-11-02','workforce_hours') if r['delivery_staff_id']==assignment['driver_id'])
        self.assertEqual(hours['accumulated_hours'],2)
        truck=next(r for r in self.report('truck-utilisation','truck_utilisation') if r['truck_id']==assignment['truck_id'] and r['row_level']=='detail')
        self.assertEqual((truck['total_delivery_runs'],truck['total_operating_hours']),(2,2))


class PhaseFiveSeeder(unittest.TestCase):
    def test_safe_seed_counts_dates_and_history(self):
        schema='kandypack_phase5_test_'+uuid4().hex
        conn=pymysql.connect(host=settings.MYSQL_HOST,port=settings.MYSQL_PORT,user=settings.MYSQL_USER,password=settings.MYSQL_PASSWORD,cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE `{schema}`')
            conn.select_db(schema)
            seeder.initialize_empty_schema(conn)
            data=seeder.seed_data(conn,'DisposableOnly!2026')
            self.assertEqual((len(data['orders']),len(data['routes']),len(data['stations']),len(data['trips'])),(40,10,7,18))
            with conn.cursor() as cur:
                cur.execute('SELECT MIN(DATEDIFF(delivery_date,order_date)) AS days FROM customer_order')
                self.assertGreaterEqual(cur.fetchone()['days'],7)
                cur.execute("SELECT COUNT(*) n FROM audit_log WHERE action='ASSIGN_ROSTER' AND outcome='ACCEPTED'")
                self.assertEqual(cur.fetchone()['n'],18)
                cur.execute('SELECT COUNT(*) n FROM v_report_truck_utilisation')
                self.assertGreater(cur.fetchone()['n'],0)
            with self.assertRaisesRegex(ValueError,'empty data'):
                seeder.seed_data(conn)
            with self.assertRaisesRegex(ValueError,'empty disposable'):
                seeder.initialize_empty_schema(conn)
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) n FROM customer_order')
                self.assertEqual(cur.fetchone()['n'],40)
            from fastapi.testclient import TestClient
            from app.main import app
            from app.core import cache
            cache._memory_rate_limit.clear()
            with patch.object(settings,'MYSQL_DATABASE',schema):
                client=TestClient(app,base_url='https://admin.kandypack.lk')
                try:
                    login=client.post('/api/v1/auth/login',json={'email':'superadmin@kandypack.lk','password':'DisposableOnly!2026','portal_type':'admin'})
                    self.assertEqual(login.status_code,200,login.text)
                    self.assertEqual(client.get('/api/v1/reports/quarterly-sales').status_code,403)
                    reset=client.post('/api/v1/auth/change-password',json={'current_password':'DisposableOnly!2026','new_password':'ChangedForDemo!2026'})
                    self.assertEqual(reset.status_code,200,reset.text)
                    timings={}
                    for path in ('quarterly-sales','top-products','rail-capacity-utilisation','workforce-hours','truck-utilisation','station-inventory'):
                        start=perf_counter()
                        response=client.get('/api/v1/reports/'+path)
                        timings[path]=round(perf_counter()-start,4)
                        self.assertEqual(response.status_code,200,response.text)
                    print('Six seeded report HTTP durations (seconds):',timings)
                finally:
                    client.close()
        finally:
            # Exact generated schema only, never settings.MYSQL_DATABASE.
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE `{schema}`')
            conn.close()
