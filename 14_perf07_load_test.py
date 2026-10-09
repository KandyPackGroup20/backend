
import asyncio
import time
import requests

BASE_URL = "http://127.0.0.1:8000"

LOGIN_URL = f"{BASE_URL}/api/v1/auth/login"
ME_URL = f"{BASE_URL}/api/v1/auth/me"

EMAIL = "customer1@gmail.com"
PASSWORD = "PerfTest@2026"
PORTAL_TYPE = "customer"

TOTAL_USERS = 100


def login_and_access(user_no):
    session = requests.Session()

    start = time.perf_counter()

    try:
        login_response = session.post(
            LOGIN_URL,
            json={
                "email": EMAIL,
                "password": PASSWORD,
                "portal_type": PORTAL_TYPE
            },
            timeout=30
        )

        if login_response.status_code != 200:
            elapsed = time.perf_counter() - start
            return {
                "user": user_no,
                "success": False,
                "status": login_response.status_code,
                "elapsed": elapsed,
                "message": "LOGIN_FAILED"
            }

        # Keep session cookie from successful login
        me_response = session.get(
            ME_URL,
            timeout=30
        )

        elapsed = time.perf_counter() - start

        return {
            "user": user_no,
            "success": me_response.status_code == 200,
            "status": me_response.status_code,
            "elapsed": elapsed,
            "message": "SUCCESS" if me_response.status_code == 200 else "AUTH_GET_FAILED"
        }

    except Exception as e:
        elapsed = time.perf_counter() - start

        return {
            "user": user_no,
            "success": False,
            "status": None,
            "elapsed": elapsed,
            "message": str(e)
        }

    finally:
        session.close()


async def main():
    print("=" * 70)
    print("PERF-07: 100 CONCURRENT AUTHENTICATED USERS")
    print("=" * 70)

    start_total = time.perf_counter()

    loop = asyncio.get_running_loop()

    tasks = [
        loop.run_in_executor(
            None,
            login_and_access,
            i + 1
        )
        for i in range(TOTAL_USERS)
    ]

    results = await asyncio.gather(*tasks)

    total_elapsed = time.perf_counter() - start_total

    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    times = [r["elapsed"] for r in results]

    print()
    print("RESULTS")
    print("-" * 70)

    for r in results:
        print(
            f"User {r['user']:03d} | "
            f"{r['message']:15} | "
            f"Status: {str(r['status']):4} | "
            f"{r['elapsed']:.3f} sec"
        )

    print("=" * 70)
    print("PERF-07 SUMMARY")
    print("=" * 70)

    print(f"Concurrent users     : {TOTAL_USERS}")
    print(f"Successful requests  : {len(successful)}")
    print(f"Failed requests      : {len(failed)}")
    print(f"Success rate         : {(len(successful) / TOTAL_USERS) * 100:.1f}%")
    print(f"Total test duration  : {total_elapsed:.3f} sec")
    print(f"Average user time    : {sum(times) / len(times):.3f} sec")
    print(f"Maximum user time    : {max(times):.3f} sec")

    print()

    if len(successful) == TOTAL_USERS:
        print("PERF-07 PASS: 100 concurrent authenticated users handled successfully.")
    else:
        print("PERF-07 FAIL: Some concurrent authenticated requests failed.")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())