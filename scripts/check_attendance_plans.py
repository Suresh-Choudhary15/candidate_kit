"""Shows whether the GET /attendance query shapes use an index and avoid COLLSCAN / in-memory SORT.
Run with the app's environment (reads MONGO_URI / MONGO_DB from .env), after the DB has some data:
    python scripts/check_attendance_plans.py
It uses the SAME builder the endpoint uses (build_attendance_query), so what you see is what runs."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bson import json_util  # noqa: E402

from app.main import build_attendance_query, db  # noqa: E402

SHAPES = {
    "no filters": dict(emp_code=None, date_from=None, date_to=None, status=None),
    "emp_code": dict(emp_code="EMP0001", date_from=None, date_to=None, status=None),
    "date range": dict(emp_code=None, date_from="2026-07-01", date_to="2026-07-31", status=None),
    "status": dict(emp_code=None, date_from=None, date_to=None, status="PRESENT"),
    "emp_code + range": dict(emp_code="EMP0001", date_from="2026-07-01", date_to="2026-07-31", status=None),
    "status + range": dict(emp_code=None, date_from="2026-07-01", date_to="2026-07-31", status="WFH"),
}

bad = 0
for name, shape in SHAPES.items():
    q = build_attendance_query(**shape, page=1, page_size=20)
    cmd = {"find": "attendance_logs", "filter": q["filter"], "sort": dict(q["sort"]),
           "skip": q["skip"], "limit": q["limit"]}
    plan = json.loads(json_util.dumps(db.command({"explain": cmd, "verbosity": "queryPlanner"})))
    winning = json.dumps(plan["queryPlanner"]["winningPlan"])   # only the plan that actually runs
    everything = json.dumps(plan)                               # winning + rejected candidate plans
    sort_stage = bool(re.search(r'"stage":\s*"SORT"', winning))
    ixscan = "IXSCAN" in winning
    coll_any = "COLLSCAN" in everything
    index = re.findall(r'"indexName":\s*"([^"]+)"', winning)
    ok = ixscan and not coll_any and not sort_stage
    bad += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name:18} index={index[:1]} COLLSCAN(anywhere)={coll_any} SORT(winning)={sort_stage}")
    if not ok:
        print("      winning plan:", winning)
sys.exit(1 if bad else 0)