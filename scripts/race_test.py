"""Parallel-request checks for the race-safe endpoints. Works on Windows (no curl/xargs needed).
Start the API first (uvicorn app.main:app --port 8000), then:   python scripts/race_test.py
Use a scratch database. Each run uses fresh random employee codes, so it can be repeated."""
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.getenv("API", "http://localhost:8000")
T_IN, T_OUT = 1783311000000, 1783343100000   # 2026-07-06 09:40:00 / 18:35:00 IST
N = 30
client = httpx.Client(base_url=BASE, timeout=30, limits=httpx.Limits(max_connections=N))
failures = 0


def code():
    return f"EMP{random.randint(100000, 999999)}"


def new_employee(c):
    r = client.post("/employees", json={"emp_code": c, "name": "Race", "email": "r@b.co",
                                         "department": "RaceDept", "joined_on": "2026-01-05"})
    assert r.status_code == 201, r.text


def burst(method, url, bodies):
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return list(pool.map(lambda b: client.request(method, url, json=b), bodies))


def check(name, ok, detail):
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


# (a) the same employee created 30 times at once
c = code()
body = {"emp_code": c, "name": "Race", "email": "r@b.co", "department": "RaceDept", "joined_on": "2026-01-05"}
codes = Counter(r.status_code for r in burst("POST", "/employees", [body] * N))
check("create employee race", codes == Counter({201: 1, 409: N - 1}), dict(codes))

# (b) the same punch-in 30 times at once
c = code(); new_employee(c)
codes = Counter(r.status_code for r in burst("POST", "/attendance/punch-in", [{"emp_code": c, "punched_at": T_IN}] * N))
check("punch-in race", codes == Counter({201: 1, 409: N - 1}), dict(codes))

# (c) the same punch-out 30 times at once
codes = Counter(r.status_code for r in burst("POST", "/attendance/punch-out", [{"emp_code": c, "punched_at": T_OUT}] * N))
check("punch-out race", codes == Counter({200: 1, 409: N - 1}), dict(codes))

# (d) 20 different corrections at once on the same record: every 200 must leave exactly one history entry
bodies = [{"punch_in": T_IN - (i + 1) * 60_000, "reason": f"parallel fix {i}", "regularized_by": f"user{i}"} for i in range(20)]
results = burst("PATCH", f"/attendance/{c}/2026-07-06", bodies)
codes = Counter(r.status_code for r in results)
history = client.get("/attendance", params={"emp_code": c}).json()["items"][0]["history"]
check("patch race: no 5xx", not any(s >= 500 for s in codes), dict(codes))
check("patch race: 200s == history entries", codes[200] == len(history) and codes[200] >= 1,
      f"200 responses={codes[200]}, history entries={len(history)}, statuses={dict(codes)}")

sys.exit(1 if failures else 0)