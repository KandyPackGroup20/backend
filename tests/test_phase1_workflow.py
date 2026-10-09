"""Real FastAPI + MySQL tests. Creates/drops only a unique, disposable database.

Run from backend: .venv/Scripts/python.exe -m unittest discover -s tests -p test_phase1_workflow.py -v
Requires the sibling database checkout and CREATE DATABASE permission.
Never invokes application startup migrations or existing destructive test scripts.
"""
import os
import re
import secrets
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

os.environ['SECRET_KEY'] = secrets.token_urlsafe(48)
from fastapi.testclient import TestClient
import pymysql
from app.core.config import settings
from app.core import cache
from app.core.database import get_db
from app.core.security import get_password_hash, verify_password, create_access_token
from app.main import app


def execute_sql(conn, source):
    delimiter, buffer = ';', ''
    for line in source.splitlines():
        if line.strip().upper().startswith('DELIMITER '):
            delimiter = line.strip().split()[1]
            continue
        if line.strip().startswith('--') or not line.strip():
            continue
        buffer += line + '\n'
        if buffer.rstrip().endswith(delimiter):
            statement = buffer.rstrip()[:-len(delimiter)]
            with conn.cursor() as cur:
                cur.execute(statement)
            buffer = ''
    if buffer.strip():
        raise AssertionError('Unterminated SQL')


class PhaseOneMySQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = settings.MYSQL_DATABASE
        cls.db = 'kandypack_phase1_test_' + uuid4().hex
        cls.server = pymysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
                                    user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
                                    connect_timeout=5, autocommit=True)
        with cls.server.cursor() as cur:
            cur.execute(f'CREATE DATABASE `{cls.db}`')  # generated identifier, never user input
        settings.MYSQL_DATABASE = cls.db
        cls.addClassCleanup(cls.cleanup_database)
        sql_root = Path(__file__).resolve().parents[2] / 'database'
        with get_db() as conn:
            for filename in ('01_schema.sql', '04_triggers.sql', '14_phase1_identity_guard.sql'):
                source = (sql_root / filename).read_text(encoding='utf-8')
                source = re.sub(r'(?im)^(CREATE DATABASE[^;]*;|USE kandypack_db;|DROP TABLE[^;]*;)\s*', '', source)
                execute_sql(conn, source)
            with conn.cursor() as cur:
                cls.password = 'PhaseOne!2026'
                cur.execute("INSERT INTO user (name, role, email, password_hash) VALUES (%s,%s,%s,%s)",
                            ('Admin', 'SUPERADMIN', 'phase1@kandypack.lk', get_password_hash(cls.password)))
                cls.admin_id = cur.lastrowid
                cur.execute('UPDATE user SET force_password_reset=0 WHERE user_id=%s', (cls.admin_id,))
            conn.commit()
        print('Created isolated MySQL schema:', cls.db)

    @classmethod
    def cleanup_database(cls):
        settings.MYSQL_DATABASE = cls.original_db
        assert re.fullmatch(r'kandypack_phase1_test_[0-9a-f]{32}', cls.db)
        with cls.server.cursor() as cur:
            cur.execute(f'DROP DATABASE `{cls.db}`')
        cls.server.close()

    def setUp(self):
        cache._memory_rate_limit.clear()
        self.client = TestClient(app, base_url='https://kandypack.lk')
        self.addCleanup(self.client.close)

    def admin(self):
        result = self.client.post('/api/v1/auth/login', headers={'origin': 'https://admin.kandypack.lk'},
                                  json={'email': 'phase1@kandypack.lk', 'password': self.password, 'portal_type': 'admin'})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    def register(self):
        payload = dict(email=f'{uuid4().hex}@example.com', password=self.password, name='Customer',
                       phone='0771234567', address_line='Test Street', city='Colombo', postal_code='00100')
        result = self.client.post('/api/v1/auth/register', json=payload)
        self.assertEqual(result.status_code, 201, result.text)
        return payload, result.json()

    def test_password_bypass_and_cookie_flags(self):
        r = self.client.post('/api/v1/auth/login', json={'email': 'phase1@kandypack.lk', 'password': 'password123'})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(verify_password('plaintext', 'plaintext'))
        r = self.admin()
        for flag in ('HttpOnly', 'Secure', 'SameSite=lax'):
            self.assertIn(flag, r.headers['set-cookie'])
        r = self.client.post('/api/v1/auth/change-password', json={'current_password': 'password123', 'new_password': 'changed123'})
        self.assertEqual(r.status_code, 400)

    def test_registration_atomic_duplicate_and_domain(self):
        payload, row = self.register()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT user_id FROM customer WHERE customer_id=%s', (row['customer_id'],))
            self.assertEqual(cur.fetchone()['user_id'], row['user_id'])
        self.assertEqual(self.client.post('/api/v1/auth/register', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/auth/register', headers={'origin': 'https://admin.kandypack.lk'}, json=payload).status_code, 403)
        bad = dict(payload, email=f'{uuid4().hex}@example.com', route_id=2147483647)
        self.assertEqual(self.client.post('/api/v1/auth/register', json=bad).status_code, 409)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT user_id FROM user WHERE email=%s', (bad['email'],))
            self.assertIsNone(cur.fetchone())

    def test_staff_provision_reset_delivery_link_and_duplicates(self):
        self.admin()
        staff = dict(name='Driver', email=f'{uuid4().hex}@kandypack.lk', password=self.password, role='DRIVER', license_number='B12345')
        r = self.client.post('/api/v1/auth/users', json=staff)
        self.assertEqual(r.status_code, 201, r.text)
        user_id = r.json()['user_id']
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT license_number FROM delivery_staff WHERE user_id=%s', (user_id,))
            self.assertEqual(cur.fetchone()['license_number'], 'B12345')
        self.assertEqual(self.client.post('/api/v1/auth/users', json=staff).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/auth/users', json=dict(staff, role='CUSTOMER')).status_code, 422)
        r = self.client.post('/api/v1/auth/login', json=dict(email=staff['email'], password=self.password))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get('/api/v1/auth/users').status_code, 403)
        r = self.client.post('/api/v1/auth/change-password', json=dict(current_password=self.password, new_password='NewPassword123!'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.client.get('/api/v1/auth/me').json()['force_password_reset'])

    def test_database_provisioning_guard(self):
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SET @kandypack_staff_provisioning=1')
            with self.assertRaises(pymysql.MySQLError):
                cur.execute("INSERT INTO user (name,role,email,password_hash) VALUES ('Bad','CUSTOMER',%s,'hash')", (f'{uuid4().hex}@example.com',))

    def test_lockout_sliding_window_and_fifteen_minutes(self):
        with patch.object(cache.time, 'monotonic', return_value=100):
            for _ in range(5):
                self.assertEqual(self.client.post('/api/v1/auth/login', json={'email': 'phase1@kandypack.lk', 'password': 'wrong'}).status_code, 401)
        with patch.object(cache.time, 'monotonic', return_value=161):
            self.assertEqual(self.client.post('/api/v1/auth/login', json={'email': 'phase1@kandypack.lk', 'password': self.password}).status_code, 429)
        with patch.object(cache.time, 'monotonic', return_value=1001):
            self.assertEqual(self.client.post('/api/v1/auth/login', json={'email': 'phase1@kandypack.lk', 'password': self.password}).status_code, 200)

    def test_invalid_tokens_and_current_account_status(self):
        self.client.cookies.set('kandypack_session', 'e30.eyJyb2xlIjoiU1VQRVJBRE1JTiJ9.fake')
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)
        _, row = self.register()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE user SET is_active=0 WHERE user_id=%s', (row['user_id'],))
            conn.commit()
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)

    def test_lifecycle_anonymous_and_customer_denials(self):
        for path in ['/auth/users', '/orders', '/orders/1', '/inventory/', '/rail/trips', '/roster/assignments', '/reports/analytics']:
            r = self.client.get('/api/v1' + path)
            self.assertEqual(r.status_code, 401, (path, r.text))
        _, row = self.register()
        for path in ['/auth/users', '/inventory/', '/rail/trips', '/roster/assignments', '/reports/analytics']:
            self.assertEqual(self.client.get('/api/v1' + path).status_code, 403, path)
        self.assertEqual(self.client.get('/api/v1/orders/999999').status_code, 404)
        self.assertEqual(self.client.get('/api/v1/orders').json(), [])
        self.assertEqual(self.client.get('/api/v1/auth/me', headers={'origin': 'https://admin.kandypack.lk'}).status_code, 403)

    def test_origin_and_passwordless_session_denied(self):
        self.assertEqual(self.client.post('/api/v1/auth/login', headers={'origin': 'https://evil.example'}, json={'email': 'phase1@kandypack.lk', 'password': self.password}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/inventory/session', json={'role': 'STORE_MGR'}).status_code, 410)
        self.assertEqual(self.client.post('/api/v1/notifications/send', json={'recipient': 'x@example.com', 'subject': 'test', 'message': 'test'}).status_code, 401)

    def test_staff_profile_does_not_create_customer(self):
        self.admin()
        r = self.client.put('/api/v1/auth/me', json={'name': 'Admin Updated'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()['customer_id'])

    def test_concurrent_registration_is_unique(self):
        payload = dict(email=f'{uuid4().hex}@example.com', password=self.password, name='Concurrent',
                       phone='0771234567', address_line='Test', city='Colombo', postal_code='00100')
        def submit(_):
            with_client = TestClient(app, base_url='https://kandypack.lk')
            try:
                return with_client.post('/api/v1/auth/register', json=payload).status_code
            finally:
                with_client.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(results.count(201), 1, results)
        self.assertTrue(all(code in {201, 400, 409} for code in results), results)
        with get_db() as conn, conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM user u JOIN customer c ON c.user_id=u.user_id WHERE u.email=%s', (payload['email'],))
            self.assertEqual(cur.fetchone()['n'], 1)

    def test_current_role_overrides_stale_jwt_claim(self):
        _, row = self.register()
        token = create_access_token({'sub': str(row['user_id']), 'role': 'SUPERADMIN'})
        r = self.client.get('/api/v1/auth/users', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 403)

    def test_customer_order_ownership(self):
        _, owner = self.register()
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO station_store (city,address) VALUES ('Colombo','Test station')")
            station_id = cur.lastrowid
            cur.execute("INSERT INTO delivery_route (station_id,route_name,max_delivery_time) VALUES (%s,'Test route','02:00:00')", (station_id,))
            route_id = cur.lastrowid
            cur.execute("""INSERT INTO customer_order (customer_id,order_date,delivery_date,delivery_route_id,delivery_address,recipient_name,recipient_phone)
                           VALUES (%s,CURRENT_DATE,DATE_ADD(CURRENT_DATE, INTERVAL 8 DAY),%s,'Test address','Owner','0771234567')""", (owner['customer_id'], route_id))
            order_id = cur.lastrowid
            conn.commit()
        self.assertEqual(len(self.client.get('/api/v1/orders').json()), 1)
        self.assertEqual(self.client.get(f'/api/v1/orders/{order_id}').status_code, 200)
        self.register()
        self.assertEqual(self.client.get('/api/v1/orders').json(), [])
        self.assertEqual(self.client.get(f'/api/v1/orders/{order_id}').status_code, 404)

    def test_concurrent_login_attempts_cannot_skip_lockout(self):
        def submit(_):
            client = TestClient(app, base_url='https://kandypack.lk')
            try:
                return client.post('/api/v1/auth/login', json={'email': 'phase1@kandypack.lk', 'password': 'wrong'}).status_code
            finally:
                client.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        self.assertEqual(results.count(401), 5, results)
        self.assertEqual(results.count(429), 1, results)


if __name__ == '__main__':
    unittest.main(verbosity=2)
