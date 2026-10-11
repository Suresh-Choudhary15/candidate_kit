"""Calls GET /admin/explain/* for all five endpoints and asserts: the plan uses IXSCAN and COLLSCAN appears NOWHERE
(not even in rejected plans or sub-pipelines). Run it on the 100k dataset:
    python scripts/seed_test_data.py --bulk 100000      (restart nothing: same database as the running API)
    python scripts/check_explain.py"""
import json
import os
import re
import sys

import httpx

BASE = os.getenv("API", "http://localhost:8000")
client = httpx.Client(base_url=BASE, timeout=120)
CASES = {
    "attendance_list (no filter)": ("attendance_list", {}),
    "attendance_list (emp_code)": ("attendance_list", {"emp_code": "EMP200010", "page": 2}),
    "attendance_list (date range)": ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-15"}),
    "attendance_list (status)": ("attendance_list", {"status": "WFH"}),
    "attendance_list (status+range)": ("attendance_list", {"status": "PRESENT", "date_from": "2026-06-01", "date_to": "2026-06-30"}),
    "employee_monthly": ("employee_monthly", {"emp_code": "EMP200010", "month": "2026-07"}),
    "department_summary (all)": ("department_summary", {"month": "2026-07"}),
    "department_summary (dept)": ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    "late_leaderboard": ("late_leaderboard", {"month": "2026-07", "limit": 10}),
    "late_leaderboard (dept)": ("late_leaderboard", {"month": "2026-07", "limit": 5, "department": "Sales"}),
    "department_trend": ("department_trend", {"department": "Engineering", "from": "2026-05-01", "to": "2026-07-31"}),
}
bad = 0
for label, (endpoint, params) in CASES.items():
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    if r.status_code != 200:
        print(f"FAIL  {label}: HTTP {r.status_code} {r.text[:300]}"); bad += 1; continue
    text = json.dumps(r.json()["explain"])
    ix, coll = "IXSCAN" in text, "COLLSCAN" in text
    stats = re.search(r'"executionTimeMillis":\s*(\d+)', text)
    indexes = sorted(set(re.findall(r'"indexName":\s*"([^"]+)"', text)) | set(re.findall(r'"indexesUsed":\s*\[([^\]]*)\]', text)))
    ok = ix and not coll
    bad += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label:34} IXSCAN={ix} COLLSCAN={coll} {stats.group(1) + ' ms' if stats else ''} indexes={indexes}")
sys.exit(1 if bad else 0)