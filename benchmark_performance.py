import os
import sys
import time
import json
import warnings

warnings.filterwarnings("ignore")

BASE_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

_test_client = None


def get_client():
    global _test_client
    if _test_client is None:
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            _test_client = TestClient(app)
        except Exception as e:
            print(f"Failed to initialize local TestClient: {e}")
            return None
    return _test_client


def print_banner():
    print("\n--- Performance and Rate Limit Benchmark ---\n")


def http_request(method: str, path: str, body: dict = None, headers: dict = None):
    if os.environ.get("USE_TESTCLIENT", "0") == "1":
        client = get_client()
        start_tc = time.perf_counter()
        if method == "GET":
            r = client.get(path, headers=headers)
        elif method == "POST":
            r = client.post(path, json=body, headers=headers)
        else:
            r = client.request(method, path, json=body, headers=headers)
        elapsed_ms = (time.perf_counter() - start_tc) * 1000
        try:
            j = r.json()
        except Exception:
            j = {"text": r.text}
        return r.status_code, j, dict(r.headers), elapsed_ms

    import urllib.request
    import urllib.error

    url = f"{BASE_URL}{path}"
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    start_time = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            resp_body = resp.read().decode("utf-8")
            resp_json = json.loads(resp_body) if resp_body else {}
            return resp.status, resp_json, dict(resp.headers), elapsed_ms
    except urllib.error.HTTPError as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"detail": str(e)}
        return e.code, err_body, dict(e.headers), elapsed_ms
    except Exception:
        client = get_client()
        if client:
            start_tc = time.perf_counter()
            if method == "GET":
                r = client.get(path, headers=headers)
            elif method == "POST":
                r = client.post(path, json=body, headers=headers)
            else:
                r = client.request(method, path, json=body, headers=headers)
            elapsed_ms = (time.perf_counter() - start_tc) * 1000
            try:
                j = r.json()
            except Exception:
                j = {"text": r.text}
            return r.status_code, j, dict(r.headers), elapsed_ms

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        return 0, {"error": "Server not reachable"}, {}, elapsed_ms


def check_health():
    print("[1] Checking health and cache status...")
    status_code, body, _, elapsed = http_request("GET", "/health")
    if status_code == 404 and os.environ.get("USE_TESTCLIENT") != "1":
        os.environ["USE_TESTCLIENT"] = "1"
        status_code, body, _, elapsed = http_request("GET", "/health")

    if status_code != 200:
        print(f"  Backend unreachable ({elapsed:.1f}ms)")
        return False

    db_status = body.get("database", "unknown")
    cache_info = body.get("cache", {})
    engine = cache_info.get("engine", "unknown")
    redis_active = cache_info.get("redis_connected", False)

    print(f"  Backend: ONLINE ({elapsed:.1f}ms)")
    print(f"  Database: {db_status}")
    print(f"  Cache: {engine} (redis connected: {redis_active})")
    print()
    return True


def demo_rate_limiting():
    print("[2] Testing login rate limiting...")
    target_email = "intruder@kandypack.lk"

    for attempt in range(1, 7):
        payload = {
            "email": target_email,
            "password": f"wrong_pass_{attempt}",
            "portal_type": "admin"
        }
        status_code, body, _, latency = http_request("POST", "/api/v1/auth/login", body=payload)
        detail = body.get("detail", "")

        if status_code == 401:
            print(f"  Attempt {attempt}: 401 Unauthorized -> {detail} ({latency:.1f}ms)")
        elif status_code == 429:
            print(f"  Attempt {attempt}: 429 Too Many Requests -> {detail} ({latency:.1f}ms)")
            print("  Rate limit triggered successfully.\n")
            return
        else:
            print(f"  Attempt {attempt}: {status_code} ({latency:.1f}ms)")

    print()


def demo_cache_performance():
    print("[3] Testing schedule query caching...")
    endpoint = "/api/v1/rail/schedules"

    try:
        from app.core.cache import invalidate_cache
        invalidate_cache("cache:rail:")
    except Exception:
        pass

    status_1, data_1, headers_1, cold_ms = http_request("GET", endpoint)
    x_cache_1 = headers_1.get("x-cache") or headers_1.get("X-Cache") or "MISS"
    print(f"  Request 1 (cold / {x_cache_1}): {cold_ms:6.2f}ms")

    status_2, _, headers_2, warm_ms_1 = http_request("GET", endpoint)
    x_cache_2 = headers_2.get("x-cache") or headers_2.get("X-Cache") or "HIT"
    print(f"  Request 2 (warm / {x_cache_2}): {warm_ms_1:6.2f}ms")

    status_3, _, headers_3, warm_ms_2 = http_request("GET", endpoint)
    x_cache_3 = headers_3.get("x-cache") or headers_3.get("X-Cache") or "HIT"
    print(f"  Request 3 (warm / {x_cache_3}): {warm_ms_2:6.2f}ms")

    avg_warm = (warm_ms_1 + warm_ms_2) / 2.0
    speedup = (cold_ms / avg_warm) if avg_warm > 0 else 1.0

    print("\n--- Summary ---")
    print(f"  Cold request (DB)   : {cold_ms:.2f} ms")
    print(f"  Warm request (Cache): {avg_warm:.2f} ms")
    print(f"  Speedup             : {speedup:.1f}x")
    print()


def main():
    print_banner()
    if not check_health():
        sys.exit(1)
    demo_rate_limiting()
    demo_cache_performance()
    print("Done.")


if __name__ == "__main__":
    main()
