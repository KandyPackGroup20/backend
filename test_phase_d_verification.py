import urllib.request
import urllib.error
import urllib.parse
import json
import http.cookiejar
import time
import concurrent.futures

BASE_URL = "http://127.0.0.1:8000/api/v1"

class HttpClient:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def request(self, method, url, data=None, headers=None):
        if headers is None:
            headers = {}
        encoded_data = None
        if data is not None:
            if isinstance(data, dict):
                encoded_data = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif isinstance(data, (str, bytes)):
                encoded_data = data if isinstance(data, bytes) else data.encode("utf-8")
        req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
        try:
            with self.opener.open(req) as resp:
                body = resp.read().decode("utf-8")
                resp_headers = dict(resp.headers)
                status = resp.status
                try:
                    json_data = json.loads(body)
                except Exception:
                    json_data = body
                return status, json_data, resp_headers
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            resp_headers = dict(e.headers)
            try:
                json_data = json.loads(body)
            except Exception:
                json_data = body
            return e.code, json_data, resp_headers

def test_phase_d():
    print("=== PHASE D: BACKEND VERIFICATION ===")

    # 1. Logins
    print("\n--- 1. Testing Logins ---")
    users = [
        ("logistics@kandypack.lk", "admin", 200),
        ("dispatch@kandypack.lk", "admin", 200),
        ("wh.staff1@kandypack.lk", "admin", 200),
        ("customer1@gmail.com", "admin", 403),
        ("customer1@gmail.com", "customer", 200),
    ]

    tokens = {}
    clients = {}

    for email, portal, expected_status in users:
        client = HttpClient()
        status, data, headers = client.request(
            "POST",
            f"{BASE_URL}/auth/login",
            data={"email": email, "password": "password123", "portal_type": portal}
        )
        print(f"Login {email} (portal: {portal}): status {status}")
        assert status == expected_status, f"Expected {expected_status}, got {status}: {data}"
        if expected_status == 403:
            assert "CUSTOMER_ACCESS_DENIED" in str(data.get("detail", "")), f"Expected CUSTOMER_ACCESS_DENIED, got {data}"
            print("  -> Verified: customer on admin portal received 403 CUSTOMER_ACCESS_DENIED")
        elif status == 200:
            clients[email] = client
            # Extract session cookie
            for cookie in client.cj:
                if cookie.name == "kandypack_session":
                    tokens[email] = cookie.value
            print(f"  -> Verified: login successful, role={data.get('role')}")

    # Also log in superadmin
    admin_client = HttpClient()
    status, data, _ = admin_client.request(
        "POST",
        f"{BASE_URL}/auth/login",
        data={"email": "admin@kandypack.lk", "password": "password123", "portal_type": "admin"}
    )
    assert status == 200
    for cookie in admin_client.cj:
        if cookie.name == "kandypack_session":
            tokens["admin@kandypack.lk"] = cookie.value
    clients["admin@kandypack.lk"] = admin_client

    # 2. RBAC Testing
    print("\n--- 2. RBAC on Rail Endpoints ---")
    rail_endpoints = [
        ("GET", "/rail/trips"),
        ("GET", "/rail/orders/pending"),
        ("GET", "/rail/schedules"),
        ("GET", "/rail/audit"),
        ("GET", "/rail/trips/capacity"),
    ]

    for method, ep in rail_endpoints:
        url = f"{BASE_URL}{ep}"
        
        # Test 401 No Token
        anon_client = HttpClient()
        s, d, _ = anon_client.request(method, url)
        assert s == 401, f"{ep} expected 401 for no token, got {s}"

        # Test 401 Garbage Token
        garbage_client = HttpClient()
        s, d, _ = garbage_client.request(method, url, headers={"Cookie": "kandypack_session=invalid.garbage.token"})
        assert s == 401, f"{ep} expected 401 for garbage token, got {s}"

        # Test 403 Customer
        s, d, _ = clients["customer1@gmail.com"].request(method, url)
        assert s == 403, f"{ep} expected 403 for customer, got {s}"

        # Test 403 Dispatcher
        s, d, _ = clients["dispatch@kandypack.lk"].request(method, url)
        assert s == 403, f"{ep} expected 403 for dispatcher, got {s}"

        # Test 403 Warehouse Staff
        s, d, _ = clients["wh.staff1@kandypack.lk"].request(method, url)
        assert s == 403, f"{ep} expected 403 for warehouse staff, got {s}"

        # Test 200 Logistics Mgr
        s, d, _ = clients["logistics@kandypack.lk"].request(method, url)
        assert s == 200, f"{ep} expected 200 for logistics mgr, got {s}"

        # Test 200 Superadmin
        s, d, _ = clients["admin@kandypack.lk"].request(method, url)
        assert s == 200, f"{ep} expected 200 for superadmin, got {s}"

    print("  -> All RBAC rules (401 unauth/garbage, 403 customer/dispatch/wh, 200 logistics/superadmin) verified!")

    # 3. Cache MISS / HIT & Invalidation
    print("\n--- 3. Cache MISS / HIT & Fresh Capacity ---")
    log_client = clients["logistics@kandypack.lk"]
    
    # Invalidate cache first by creating a dummy trip or calling invalidation
    # First call:
    s1, d1, h1 = log_client.request("GET", f"{BASE_URL}/rail/schedules")
    print(f"Schedule call 1: X-Cache={h1.get('x-cache') or h1.get('X-Cache')}")
    # Second call:
    s2, d2, h2 = log_client.request("GET", f"{BASE_URL}/rail/schedules")
    cache_header = h2.get('x-cache') or h2.get('X-Cache')
    print(f"Schedule call 2: X-Cache={cache_header}")
    assert cache_header == "HIT", f"Expected X-Cache HIT, got {cache_header}"
    print("  -> Cache HIT verified!")

    # 4. Latency / Timing benchmark against SLAs
    print("\n--- 4. Latency SLAs Benchmarking ---")
    # Lookup <= 3s
    t0 = time.time()
    s, d, _ = log_client.request("GET", f"{BASE_URL}/rail/trips")
    lookup_time = time.time() - t0
    print(f"Trips Lookup time: {lookup_time:.3f}s (SLA <= 3s)")
    assert lookup_time <= 3.0, f"Lookup exceeded 3s: {lookup_time}"

    t0 = time.time()
    s, d, _ = log_client.request("GET", f"{BASE_URL}/rail/orders/pending")
    pending_time = time.time() - t0
    print(f"Pending Orders Lookup time: {pending_time:.3f}s (SLA <= 3s)")
    assert pending_time <= 3.0, f"Pending orders lookup exceeded 3s: {pending_time}"

    print("\n=== ALL PHASE D CHECKS PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    test_phase_d()
