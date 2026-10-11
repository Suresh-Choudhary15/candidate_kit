"""Compares the running API with the slow reference implementation over many parameter combinations.
    python scripts/compare.py                    # all endpoints that exist
    python scripts/compare.py --only trend,summary
Needs: API running (uvicorn app.main:app --port 8000) on the same MONGO_URI / MONGO_DB as this script, with data seeded."""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference as ref  # noqa: E402
from app.main import db  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--only", default="monthly,summary,leaderboard,trend")
parser.add_argument("--months", type=int, default=0, help="only the last N months of data (0 = all); use 2 on the 100k dataset")
parser.add_argument("--max-employees", type=int, default=40, help="sample size for the per-employee check")
args = parser.parse_args()
only = set(args.only.split(","))

employees = list(db.employees.find({}, {"_id": 0}))
logs = list(db.attendance_logs.find({}, {"_id": 0}))
http = httpx.Client(base_url=args.base, timeout=60)
months = sorted({l["date"][:7] for l in logs}) or ["2026-07"]
months = (months[-args.months:] if args.months else months) + ["2020-01"]                                            # a month with no data at all
depts = sorted({e["department"] for e in employees})
stats = {"checked": 0, "mismatches": 0, "skipped": set()}


def check(label, path, params, expected):
    stats["checked"] += 1
    r = http.get(path, params=params)
    if r.status_code == 404 and r.json().get("detail") == "Not Found":
        stats["skipped"].add(path.split("/")[2] if path.count("/") > 2 else path)
        stats["checked"] -= 1
        return False
    if expected is None:
        got = r.status_code
        ok = got == 404
        detail = f"expected 404, got {got}"
    else:
        got = r.json() if r.status_code == 200 else r.status_code
        ok = got == expected
        detail = f"\n    expected: {expected}\n    got:      {got}"
    if not ok:
        stats["mismatches"] += 1
        if stats["mismatches"] <= 8:
            print(f"MISMATCH {label} {params}{detail}")
    return True


if "monthly" in only:
    codes = [e["emp_code"] for e in employees][: args.max_employees]
    for code in codes:
        for month in months:
            if not check("monthly", f"/analytics/employees/{code}/monthly", {"month": month}, ref.employee_monthly(employees, logs, code, month)):
                break
    check("monthly-unknown", "/analytics/employees/EMP000000/monthly", {"month": months[0]}, None)

if "summary" in only:
    for month in months:
        for dept in [None] + depts + ["NoSuchDept"]:
            params = {"month": month, **({"department": dept} if dept else {})}
            if not check("summary", "/analytics/departments/summary", params, ref.department_summary(employees, logs, month, dept)):
                break

if "leaderboard" in only:
    for month in months:
        for limit in (1, 2, 3, 5, 10, 50):
            for dept in [None] + depts:
                params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
                if not check("leaderboard", "/analytics/leaderboard/late", params, ref.late_leaderboard(employees, logs, month, limit, dept)):
                    break

if "trend" in only:
    ranges = []
    for month in months[-3:]:
        first, last = ref.bounds(month)
        d0 = date.fromisoformat(first)
        ranges += [(first, last), (first, first), ((d0 + timedelta(days=3)).isoformat(), (d0 + timedelta(days=17)).isoformat())]
    ranges += [("2020-01-01", "2020-01-10"), ("2026-07-04", "2026-07-12"), ("2026-06-20", "2026-09-19")]   # empty range, starts on Saturday, exactly 92 days
    for dept in depts:
        for date_from, date_to in ranges:
            check("trend", f"/analytics/departments/{dept}/trend", {"from": date_from, "to": date_to},
                  ref.department_trend(employees, logs, dept, date_from, date_to))
    check("trend-unknown", "/analytics/departments/NoSuchDept/trend", {"from": "2026-07-01", "to": "2026-07-05"}, None)

print(f"checked {stats['checked']} requests, {stats['mismatches']} mismatches"
      + (f"; not implemented yet: {sorted(stats['skipped'])}" if stats["skipped"] else ""))
sys.exit(1 if stats["mismatches"] else 0)