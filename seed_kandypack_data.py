"""Create a NEW disposable demo database; never connect a seed run to an existing schema.

Run from backend: .venv/Scripts/python.exe seed_kandypack_data.py
Requires CREATE DATABASE permission and the sibling database checkout.
Set KANDYPACK_SEED_PASSWORD (12+ characters) to log into demo accounts.
Without it accounts receive an undisclosed random password. Staff retain forced reset.
The script prints the generated schema name; it never changes application configuration.
"""
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pymysql
from app.core.config import settings
from app.core.security import get_password_hash

SQL_ROOT = Path(__file__).resolve().parent.parent / 'database'


def execute_sql(conn, source):
    delimiter, buffer = ';', ''
    for line in source.splitlines():
        if line.strip().upper().startswith('DELIMITER '):
            delimiter = line.strip().split()[1]
            continue
        if not line.strip() or line.lstrip().startswith('--'):
            continue
        buffer += line + '\n'
        if buffer.rstrip().endswith(delimiter):
            with conn.cursor() as cur:
                cur.execute(buffer.rstrip()[:-len(delimiter)])
            buffer = ''
    if buffer.strip():
        raise ValueError('Unterminated SQL initialization statement')


def initialize_empty_schema(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT DATABASE() AS name')
        if not re.fullmatch(r'kandypack_(?:demo|phase5_test)_[0-9a-f]{32}', cur.fetchone()['name'] or ''):
            raise ValueError('Initialization is restricted to a generated disposable schema')
        cur.execute('SHOW TABLES')
        if cur.fetchone():
            raise ValueError('Initialization requires an empty disposable schema')
    files = ['01_schema.sql', '04_triggers.sql', '14_phase1_identity_guard.sql', '02_views.sql']
    files += ['feature_4_2_rail_allocation/' + name for name in (
        '01_trip_constraints.sql','02_allocation_constraints.sql','03_indexes.sql',
        '04_fn_order_item_space.sql','05_fn_trip_remaining_capacity.sql','06_trg_capacity_check.sql',
        '07_trg_trip_capacity_shrink.sql','08_sp_schedule_train_order.sql','09_v_trip_capacity_usage.sql',
        '12_trg_allocation_quantity_guard.sql','14_trg_rail_alloc_origin_bi.sql',
        '15_sp_reverse_rail_allocation.sql','16_trip_hub_guard.sql')]
    files += ['09_roster_assignment.sql','10_roster_reporting.sql','16_phase4_station_workflow.sql','17_phase5_reporting.sql']
    for name in files:
        source = (SQL_ROOT / name).read_text(encoding='utf-8-sig')
        # Base schema contains legacy reset statements: omit them even in a new schema.
        source = re.sub(r'(?im)^(CREATE DATABASE[^;]*;|USE kandypack_db;|DROP TABLE[^;]*;)\s*', '', source)
        execute_sql(conn, source)


def seed_data(conn, password=None):
    """Atomic seed into an empty, already initialized disposable schema only."""
    password = password or secrets.token_urlsafe(24)
    if len(password) < 12:
        raise ValueError('Demo password must contain at least 12 characters')
    now = datetime.now(ZoneInfo('Asia/Colombo')).replace(tzinfo=None, microsecond=0)
    hashed = get_password_hash(password)
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute('SELECT DATABASE() AS name')
            if not re.fullmatch(r'kandypack_(?:demo|phase5_test)_[0-9a-f]{32}', cur.fetchone()['name'] or ''):
                raise ValueError('Seeding is restricted to a generated disposable schema')
            for table in ('user','station_store','product','truck','customer_order'):
                cur.execute(f'SELECT COUNT(*) AS n FROM `{table}`')  # fixed internal allowlist
                if cur.fetchone()['n']:
                    raise ValueError('Seed requires empty data; existing data will never be overwritten')
            users = {}
            def account(role, suffix=''):
                key = role.lower() + suffix
                email = key + ('@example.invalid' if role=='CUSTOMER' else '@kandypack.lk')
                cur.execute('INSERT INTO user (name,role,email,password_hash) VALUES (%s,%s,%s,%s)',
                            ('Demo '+key,role,email,hashed))
                users[key] = cur.lastrowid
                return cur.lastrowid
            for role in ('SUPERADMIN','LOGISTICS_MGR','DISPATCHER'):
                account(role)
            stations = []
            for city in ('Kandy','Colombo','Negombo','Galle','Matara','Jaffna','Trincomalee'):
                manager = account('STORE_MGR',city.lower()) if city!='Kandy' else None
                cur.execute('INSERT INTO station_store (city,address,manager_id) VALUES (%s,%s,%s)',(city,city+' demo station',manager))
                station = cur.lastrowid
                stations.append(station)
                if city!='Kandy':
                    warehouse = account('WAREHOUSE_STAFF',city.lower())
                    cur.execute('UPDATE user SET station_id=%s WHERE user_id=%s',(station,warehouse))
                    cur.execute("INSERT INTO storage_location (station_id,location_code,location_type) VALUES (%s,'A1','BIN')",(station,))
            routes = []
            for n in range(10):
                cur.execute("INSERT INTO delivery_route (station_id,route_name,max_delivery_time) VALUES (%s,%s,'04:00:00')",(stations[1+n%6],f'Demo route {n+1}'))
                routes.append(cur.lastrowid)
            products = []
            for n, name in enumerate(('Tea','Cinnamon','Pepper','Cardamom','Cloves','Coffee')):
                cur.execute('INSERT INTO product (product_name,unit_price,unit_weight_kg,space_consumption_rate) VALUES (%s,%s,2,0.5)',(name,100*(n+1)))
                products.append(cur.lastrowid)
            trucks, drivers, assistants = [], [], []
            for n in range(6):
                cur.execute("INSERT INTO truck (plate_number,capacity,capacity_unit) VALUES (%s,2000,'KG')",(f'DEMO-{n+1}',))
                trucks.append(cur.lastrowid)
                for role, output in (('DRIVER',drivers),('ASSISTANT',assistants)):
                    user = account(role,str(n+1))
                    cur.execute('INSERT INTO delivery_staff (user_id,license_number) VALUES (%s,%s)',(user,f'DEMO-{role}-{n+1}'))
                    output.append(cur.lastrowid)
            orders = []
            for n in range(40):
                user = account('CUSTOMER',str(n+1))
                route = routes[n%10]
                city = ('Colombo','Negombo','Galle','Matara','Jaffna','Trincomalee')[n%10%6]
                cur.execute('INSERT INTO customer (user_id,customer_name,route_id,phone,address_line,city,postal_code) VALUES (%s,%s,%s,%s,%s,%s,%s)',(user,f'Demo customer {n+1}',route,'0771234567','Demo recipient address',city,'00000'))
                customer = cur.lastrowid
                cur.execute('INSERT INTO customer_order (customer_id,order_date,delivery_date,delivery_route_id,delivery_address,recipient_name,recipient_phone) VALUES (%s,%s,%s,%s,%s,%s,%s)',(customer,now.date(),(now+timedelta(days=8+n%14)).date(),route,'Demo recipient address',f'Demo customer {n+1}','0771234567'))
                order = cur.lastrowid
                orders.append(order)
                for p in (n%6,(n+1)%6):
                    cur.execute('INSERT INTO order_item (order_id,product_id,quantity,unit_price_at_order) VALUES (%s,%s,%s,%s)',(order,products[p],5+n%5,100*(p+1)))
                cur.execute("INSERT INTO order_status_history (order_id,status) VALUES (%s,'PENDING_RAIL_SCHEDULING')",(order,))
            trips = []
            for station in stations[1:]:
                for n in range(3):
                    departure = now+timedelta(days=1+n)
                    cur.execute('INSERT INTO train_trip (origin_station_id,destination_station_id,departure_datetime,arrival_datetime,total_capacity) VALUES (%s,%s,%s,%s,%s)',(stations[0],station,departure,departure+timedelta(hours=4),5 if n==0 else 500))
                    trips.append(cur.lastrowid)
            # Historical accepted duties, separated by days, comfortably within caps.
            # These are roster history, not fabricated deliveries or stock movements.
            for n in range(18):
                start = (now-timedelta(days=21-n)).replace(hour=8,minute=0,second=0)
                cur.execute("INSERT INTO roster_assignment (request_key,route_id,truck_id,driver_id,assistant_id,dispatcher_id,start_time,end_time,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'COMPLETED')",(uuid4().hex,routes[n%10],trucks[n%6],drivers[n%6],assistants[n%6],users['dispatcher'],start,start+timedelta(hours=2)))
                run = cur.lastrowid
                cur.execute("INSERT INTO audit_log (user_id,action,entity_id,outcome,entity_name,roster_id,occurred_at) VALUES (%s,'ASSIGN_ROSTER',%s,'ACCEPTED','roster_assignment',%s,%s)",(users['dispatcher'],run,run,start-timedelta(days=1)))
        conn.commit()
        return dict(orders=orders,stations=stations,routes=routes,products=products,trips=trips,
                    trucks=trucks,drivers=drivers,assistants=assistants,users=users)
    except Exception:
        conn.rollback()
        raise


def main():
    schema = 'kandypack_demo_' + uuid4().hex
    conn = pymysql.connect(host=settings.MYSQL_HOST,port=settings.MYSQL_PORT,user=settings.MYSQL_USER,
                           password=settings.MYSQL_PASSWORD,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE `{schema}`')  # generated, never IF NOT EXISTS
        conn.select_db(schema)
        initialize_empty_schema(conn)
        data = seed_data(conn,os.environ.get('KANDYPACK_SEED_PASSWORD'))
        print(json.dumps({'database':schema, 'orders':len(data['orders']), 'routes':len(data['routes']),
                          'destination_stations':6,'train_trips':len(data['trips']),
                          'staff_password_reset_required':True}))
    except Exception:
        print('Initialization failed; inspect only this newly created schema:',schema)
        raise
    finally:
        conn.close()


if __name__=='__main__':
    main()
