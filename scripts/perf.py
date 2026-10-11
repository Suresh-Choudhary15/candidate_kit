"""Times every list/analytics endpoint on the running API (median of 3 runs). Use the 100k dataset:
    python scripts/seed_test_data.py --bulk 100000 ; python scripts/perf.py"""
import os
import statistics
import sys
import time

import httpx

client = httpx.Client(base_url=os.getenv("API", "http://localhost:8000"), timeout=120)
CASES = [
    ("GET /health", "/health", {}),
    ("GET /employees", "/employees", {"page": 5}),
    ("GET /attendance (no filter)", "/attendance", {}),
    ("GET /attendance (page 500)", "/attendance", {"page": 500}),
    ("GET /attendance (emp_code)", "/attendance", {"emp_code": "EMP200010"}),
    ("GET /attendance (status+range)", "/attendance", {"status": "PRESENT", "date_from": "2026-06-01", "date_to": "2026-06-30"}),
    ("employee monthly", "/analytics/employees/EMP200010/monthly", {"month": "2026-07"}),
    ("department summary (all)", "/analytics/departments/summary", {"month": "2026-07"}),
    ("department summary (dept)", "/analytics/departments/summary", {"month": "2026-07", "department": "Engineering"}),
    ("late leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
    ("late leaderboard (dept)", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10, "department": "Sales"}),
    ("department trend (92 days)", "/analytics/departments/Engineering/trend", {"from": "2026-05-01", "to": "2026-07-31"}),
]
slow = 0
for label, path, params in CASES:
    times = []
    for _ in range(3):
        start = time.perf_counter()
        r = client.get(path, params=params)
        times.append((time.perf_counter() - start) * 1000)
        if r.status_code != 200:
            print(f"ERROR {label}: HTTP {r.status_code} {r.text[:200]}"); break
    else:
        med = statistics.median(times)
        slow += med > 500
        print(f"{'SLOW' if med > 500 else 'ok  '}  {med:7.0f} ms  {label}")
sys.exit(1 if slow else 0)