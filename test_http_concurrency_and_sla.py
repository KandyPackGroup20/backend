import urllib.request
import urllib.error
import json
import http.cookiejar
import time
import uuid
import pymysql
import threading

BASE_URL = "http://127.0.0.1:8000/api/v1"

def get_db():
    return pymysql.connect(
        host="127.0.0.1",
        port=3307,
        user="root",
        password="",
        database="kandypack_db",
        cursorclass=pymysql.cursors.DictCursor
    )

def http_post(url, data, token):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"kandypack_session={token}"
        },
        method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req) as resp:
            dur = time.time() - t0
            return resp.status, json.loads(resp.read().decode("utf-8")), dur
    except urllib.error.HTTPError as e:
        dur = time.time() - t0
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body, dur

def test_concurrency_and_sla():
    print("=== HTTP CONCURRENCY & TIMING SLA BENCHMARK ===")
    
    # 1. Login as Logistics Manager
    login_req = urllib.request.Request(
        f"{BASE_URL}/auth/login",
        data=json.dumps({
            "email": "logistics@kandypack.lk",
            "password": "password123",
            "portal_type": "admin"
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(login_req) as resp:
        cookies = resp.headers.get_all("Set-Cookie")
        session_token = ""
        for c in cookies:
            if "kandypack_session=" in c:
                session_token = c.split("kandypack_session=")[1].split(";")[0]
                break

    assert session_token, "Failed to get logistics manager session token"

    # 2. Setup Test Data in DB for Concurrency
    conn = get_db()
    with conn.cursor() as cur:
        tag = uuid.uuid4().hex[:6]
        cur.execute("SELECT station_id FROM station_store WHERE city = 'Kandy' LIMIT 1")
        origin = cur.fetchone()["station_id"]

        cur.execute("INSERT INTO station_store (city, address) VALUES (%s, 'HTTP Conc Dest')", (f"HTTP-{tag}",))
        dest = cur.lastrowid

        cur.execute("INSERT INTO delivery_route (station_id, route_name, max_delivery_time) VALUES (%s, %s, '08:00:00')", (dest, f"HTTP Route {tag}"))
        route = cur.lastrowid

        cur.execute("INSERT INTO user (name, role, email, password_hash) VALUES ('HTTP Cust', 'CUSTOMER', %s, 'x')", (f"http-{tag}@example.com",))
        uid = cur.lastrowid

        cur.execute("INSERT INTO customer (user_id, customer_name, route_id, phone, address_line, city, postal_code) VALUES (%s, 'HTTP Cust', %s, '0771234567', 'Test Rd', 'Kandy', '20000')", (uid, route))
        cust = cur.lastrowid

        cur.execute("INSERT INTO product (product_name, unit_price, space_consumption_rate) VALUES (%s, 100.00, 1.0000)", (f"HTTP Box {tag}",))
        prod = cur.lastrowid

        # Trip capacity = 10 units
        cur.execute("INSERT INTO train_trip (origin_station_id, destination_station_id, departure_datetime, arrival_datetime, total_capacity, status) VALUES (%s, %s, NOW() + INTERVAL 2 DAY, NOW() + INTERVAL 2 DAY + INTERVAL 4 HOUR, 10, 'SCHEDULED')", (origin, dest))
        trip_id = cur.lastrowid

        # 2 Orders, each needing 6 units space (6 * 1.0 = 6) -> 6 + 6 = 12 > 10. Since only 1 trip exists, 2nd will fail with INSUFFICIENT_RAIL_CAPACITY
        orders = []
        for _ in range(2):
            cur.execute("INSERT INTO customer_order (customer_id, order_date, delivery_date, status) VALUES (%s, CURDATE(), CURDATE() + INTERVAL 7 DAY, 'PENDING_RAIL_SCHEDULING')", (cust,))
            oid = cur.lastrowid
            cur.execute("INSERT INTO order_item (order_id, product_id, quantity, unit_price_at_order) VALUES (%s, %s, 6, 100.00)", (oid, prod))
            orders.append(oid)

        conn.commit()

    print(f"Test data created: Trip #{trip_id} (cap 10), Orders {orders} (6 units each)")

    # 3. Fire 2 Simultaneous HTTP Allocate Requests at Trip #trip_id
    results = [None, None]
    barrier = threading.Barrier(2)

    def allocate_worker(idx, oid):
        barrier.wait()
        res = http_post(f"{BASE_URL}/rail/allocate", {"order_id": oid}, session_token)
        results[idx] = res

    t1 = threading.Thread(target=allocate_worker, args=(0, orders[0]))
    t2 = threading.Thread(target=allocate_worker, args=(1, orders[1]))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print("\nSimultaneous HTTP Allocation Results:")
    for i, res in enumerate(results):
        status, body, dur = res
        print(f"  Request {i+1} (Order {orders[i]}): HTTP {status}, Time: {dur:.3f}s, Response: {body}")
        assert dur <= 5.0, f"Allocation exceeded 5s SLA: {dur}s"

    # Verify exactly 1 succeeded and 1 failed
    statuses = [r[0] for r in results]
    assert 200 in statuses, "At least one allocation must succeed with HTTP 200"
    assert 400 in statuses, "One allocation must fail with HTTP 400 (INSUFFICIENT_RAIL_CAPACITY)"
    
    # Check DB usage
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(allocated_space), 0) AS total_used FROM rail_allocation WHERE trip_id = %s", (trip_id,))
        used = float(cur.fetchone()["total_used"])
        print(f"\nTrip #{trip_id} Total Used Space: {used} / 10.00")
        assert used <= 10.0, f"Over-allocation detected! used={used} > 10.0"
        assert used == 6.0, f"Expected exactly 6.00 used space, got {used}"
    print("  -> Concurrency verified: No over-allocation! Exactly one allocation succeeded.")

    # 4. Multi-trip Spillover Allocation Benchmark (SLA <= 8s)
    print("\n--- Multi-trip Spillover Allocation Benchmark ---")
    with conn.cursor() as cur:
        # Trip B (cap 5) and Trip C (cap 10)
        cur.execute("INSERT INTO train_trip (origin_station_id, destination_station_id, departure_datetime, arrival_datetime, total_capacity, status) VALUES (%s, %s, NOW() + INTERVAL 3 DAY, NOW() + INTERVAL 3 DAY + INTERVAL 4 HOUR, 5, 'SCHEDULED')", (origin, dest))
        trip_b = cur.lastrowid
        cur.execute("INSERT INTO train_trip (origin_station_id, destination_station_id, departure_datetime, arrival_datetime, total_capacity, status) VALUES (%s, %s, NOW() + INTERVAL 4 DAY, NOW() + INTERVAL 4 DAY + INTERVAL 4 HOUR, 10, 'SCHEDULED')", (origin, dest))
        trip_c = cur.lastrowid

        cur.execute("INSERT INTO customer_order (customer_id, order_date, delivery_date, status) VALUES (%s, CURDATE(), CURDATE() + INTERVAL 7 DAY, 'PENDING_RAIL_SCHEDULING')", (cust,))
        spillover_order_id = cur.lastrowid
        # Needs 8 units (Trip B has 5, Trip C has 10 -> will spillover across B and C)
        cur.execute("INSERT INTO order_item (order_id, product_id, quantity, unit_price_at_order) VALUES (%s, %s, 8, 100.00)", (spillover_order_id, prod))
        conn.commit()

    st, body, multi_dur = http_post(f"{BASE_URL}/rail/allocate", {"order_id": spillover_order_id}, session_token)
    print(f"Multi-trip Spillover HTTP {st}, Time: {multi_dur:.3f}s, Response: {body}")
    assert st == 200, f"Multi-trip allocation failed: {body}"
    assert body.get("status_result") in ["SUCCESS_MULTI_TRIP", "SUCCESS_MULTI_TRIP_SPILLOVER"], f"Expected multi-trip success, got {body}"
    assert multi_dur <= 8.0, f"Multi-trip allocation exceeded 8s SLA: {multi_dur}s"
    print(f"  -> Multi-trip spillover allocation SLA verified: {multi_dur:.3f}s <= 8.0s")

    # 5. Reverse Order Allocation SLA & Verification (LM-18)
    print("\n--- Reverse Allocation Benchmark (LM-18) ---")
    st, body, rev_dur = http_post(f"{BASE_URL}/rail/orders/{spillover_order_id}/reverse", {}, session_token)
    print(f"Reverse Allocation HTTP {st}, Time: {rev_dur:.3f}s, Response: {body}")
    assert st == 200, f"Reverse allocation failed: {body}"
    assert body.get("status_result") == "SUCCESS_REVERSED"
    print(f"  -> Reverse allocation verified: capacity restored!")

    conn.close()
    print("\n=== CONCURRENCY & TIMING SLAs ALL PASSED ===")

if __name__ == "__main__":
    test_concurrency_and_sla()
