"""Seed a SCRATCH database with test data. Never touches a database whose name lacks 'test' or 'scratch'.

    python scripts/seed_test_data.py --small          # ~100 hand-designed awkward records (July 2026)
    python scripts/seed_test_data.py --bulk 100000    # random large dataset for performance / explain checks

It reads MONGO_URI / MONGO_DB exactly like the app (a real environment variable beats .env), e.g. in PowerShell:
    $env:MONGO_DB="attendance_scratch_small"; python scripts/seed_test_data.py --small
Derived values (late_minutes, work_hours, ...) are written by hand here on purpose: they do NOT come from the app code.
"""
import argparse
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
WD = [1, 2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31]   # July 2026 weekdays (1 July = Wednesday)


def at(text: str) -> datetime:
    """'2026-07-06 09:28' (IST) -> aware UTC datetime."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=IST).astimezone(UTC)


def employee(code, name, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": name, "email": f"{name.lower().replace(' ', '.')}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": at(f"{joined} 10:00")}


def record(code, day, status, tin, tout, late, ot, wh, half, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status, "punch_in": at(tin) if tin else None,
           "punch_out": at(tout) if tout else None, "work_hours": wh, "late_minutes": late,
           "overtime_minutes": ot, "half_day": half, "history": []}
    if legacy:   # older documents: derived fields and history simply absent
        for key in ("late_minutes", "overtime_minutes", "half_day", "history"):
            doc.pop(key)
    return doc


def fill(code, days, overrides=None, skip=(), tin="09:25", tout="18:30", wh=9.08, month="2026-07"):
    """One record per day. Defaults: PRESENT, in 09:25, out 18:30, 9.08 h, not late. `overrides` change single days.
    A tout starting with '+' means 'the next calendar day'."""
    overrides, rows = overrides or {}, []
    for day in sorted(set(days) | set(overrides)):
        if day in skip:
            continue
        cfg = {"status": "PRESENT", "tin": tin, "tout": tout, "late": 0, "ot": 0, "wh": wh, "half": False, "legacy": False}
        cfg.update(overrides.get(day, {}))
        d = f"{month}-{day:02d}"
        if cfg["status"] in ("ABSENT", "LEAVE"):
            cfg.update(tin=None, tout=None, wh=None, late=0, ot=0, half=False)
        t_in = f"{d} {cfg['tin']}" if cfg["tin"] else None
        t_out = None
        if cfg["tout"]:
            if cfg["tout"].startswith("+"):
                t_out = f"{date.fromisoformat(d) + timedelta(days=1)} {cfg['tout'][1:]}"
            else:
                t_out = f"{d} {cfg['tout']}"
        rows.append(record(code, d, cfg["status"], t_in and t_in, t_out, cfg["late"], cfg["ot"], cfg["wh"], cfg["half"], cfg["legacy"]))
    return rows


def build_small():
    emps = [
        employee("EMP1001", "Asha Rao", "Engineering", "2026-01-05"),
        employee("EMP1002", "Vikram Shah", "Engineering", "2026-01-05"),
        employee("EMP1003", "Neha Iyer", "Engineering", "2026-02-02"),
        employee("EMP1004", "Karan Mehta", "Engineering", "2026-07-13"),      # joins mid-month
        employee("EMP1005", "Zoya Khan", "Engineering", "2026-03-01"),        # no logs at all
        employee("EMP1006", "Lowercase Dept", "engineering", "2026-02-01"),   # department is case-sensitive
        employee("EMP2001", "Sales One", "Sales", "2026-01-05"),
        employee("EMP2002", "Sales Two", "Sales", "2026-01-05"),
        employee("EMP2003", "Sales Three", "Sales", "2026-01-05"),
        employee("EMP2004", "Sales Four", "Sales", "2026-01-05"),
        employee("EMP3001", "Night One", "Ops", "2026-01-05", "22:00", "06:00"),   # overnight shifts
        employee("EMP3002", "Night Two", "Ops", "2026-01-05", "22:00", "06:00"),
        employee("EMP4001", "Legal Later", "Legal", "2026-08-10"),            # joins after July: department omitted
        employee("EMP5001", "Joined Last Day", "HR", "2026-07-31"),           # joins on the last day: counts
    ]
    logs = []
    logs += fill("EMP1001", WD, skip={17}, overrides={
        2: {"tin": "09:41", "late": 11, "wh": 8.82},
        4: {"tin": "10:00", "tout": "14:00", "late": 30, "wh": 4.0, "half": True},          # Saturday
        5: {"status": "ON_DUTY", "tin": "09:30", "tout": "18:30", "wh": 9.0},                # Sunday
        8: {"tin": "10:15", "late": 45, "wh": 8.25},
        9: {"tin": "09:30", "tout": "13:30", "wh": 4.0, "half": True},
        10: {"tout": "19:15", "ot": 45, "wh": 9.83},
        15: {"status": "LEAVE"}, 16: {"status": "ABSENT"},
        21: {"legacy": True},
        22: {"tin": "09:40", "late": 10, "wh": 8.83},
    })
    logs += [record("EMP1001", "2026-06-30", "PRESENT", "2026-06-30 09:25", "2026-06-30 18:30", 0, 0, 9.08, False),
             record("EMP1001", "2026-08-01", "PRESENT", "2026-08-01 09:50", "2026-08-01 18:30", 20, 0, 8.67, False)]  # outside July
    logs += fill("EMP1002", WD, overrides={
        3: {"tin": "09:50", "late": 20, "wh": 8.67},
        7: {"status": "WFH"}, 8: {"status": "WFH"}, 9: {"status": "ON_DUTY"},
        10: {"tout": None, "wh": None},                                                       # still punched in
        14: {"tin": "10:10", "late": 40, "wh": 8.33},
    })
    logs += fill("EMP1003", WD, overrides={
        6: {"tin": "10:30", "late": 60, "wh": 8.0},
        20: {"status": "LEAVE"}, 21: {"status": "LEAVE"}, 22: {"status": "LEAVE"}, 23: {"status": "LEAVE"}, 24: {"status": "LEAVE"},
        27: {"status": "ABSENT"},
        30: {"tout": "13:00", "wh": 3.58, "half": True},
    })
    logs += fill("EMP1004", [d for d in WD if d >= 13], overrides={14: {"tin": "09:45", "late": 15, "wh": 8.75}})
    logs += fill("EMP1006", [1, 2, 3])
    logs += fill("EMP2001", WD, skip={3, 10}, overrides={7: {"tin": "10:20", "late": 50, "wh": 8.17}, 21: {"tin": "10:20", "late": 50, "wh": 8.17}})
    logs += fill("EMP2002", WD, skip={1}, overrides={8: {"tin": "09:55", "late": 25, "wh": 8.58}, 22: {"tin": "10:05", "late": 35, "wh": 8.42}})
    logs += fill("EMP2003", WD, overrides={9: {"tin": "10:30", "late": 60, "wh": 8.0}})
    logs += fill("EMP2004", WD, overrides={14: {"tin": "10:00", "late": 30, "wh": 8.5}, 25: {"status": "WFH", "tin": "11:00", "tout": "15:00", "wh": 4.0, "half": True}})
    logs += fill("EMP3001", [], tin="21:55", tout="+06:40", wh=8.75, overrides={
        6: {"ot": 40}, 7: {"tin": "22:05", "tout": "+06:00", "wh": 7.92}})
    logs += fill("EMP3002", [], overrides={8: {"tin": "22:30", "tout": "+06:00", "late": 30, "wh": 7.5}, 9: {"status": "LEAVE"}})
    logs += fill("EMP5001", [31])
    logs += fill("EMP9999", [], overrides={7: {"tin": "14:30", "tout": "18:30", "late": 500, "wh": 4.0}})   # no such employee
    return emps, logs


def build_bulk(n: int, seed: int = 7):
    rnd = random.Random(seed)
    depts = ["Engineering", "Sales", "Ops", "HR", "Finance", "Support", "Legal", "Design"]
    emps = []
    for i in range(500):
        dept = depts[i % 8]
        start, end = ("22:00", "06:00") if dept == "Ops" else rnd.choice([("09:30", "18:30"), ("10:00", "19:00")])
        joined = (date(2025, 6, 1) + timedelta(days=rnd.randint(0, 200))).isoformat()
        emps.append(employee(f"EMP{200000 + i}", f"Employee {i}", dept, joined, start, end))
    days = [date(2026, 1, 1) + timedelta(days=k) for k in range(212)]   # 1 Jan .. 31 Jul 2026
    pairs = [(e, d) for e in emps for d in days]
    rnd.shuffle(pairs)
    logs = []
    for e, d in pairs[:n]:
        day, roll = d.isoformat(), rnd.random()
        if roll < 0.07:
            logs.append(record(e["emp_code"], day, "LEAVE", None, None, 0, 0, None, False)); continue
        if roll < 0.11:
            logs.append(record(e["emp_code"], day, "ABSENT", None, None, 0, 0, None, False)); continue
        status = "PRESENT" if roll < 0.85 else rnd.choice(["WFH", "ON_DUTY"])
        sh, sm = map(int, e["shift_start"].split(":"))
        start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
        length = (datetime.strptime(e["shift_end"], "%H:%M") - datetime.strptime(e["shift_start"], "%H:%M")) % timedelta(days=1)
        off_in = rnd.randint(-15, 9) if rnd.random() < 0.8 else rnd.randint(11, 90)
        t_in = start_dt + timedelta(minutes=off_in)
        late = off_in if off_in * 60 > 600 else 0
        if rnd.random() < 0.08:                                   # still punched in
            logs.append(record(e["emp_code"], day, status, t_in.astimezone(IST).strftime("%Y-%m-%d %H:%M"), None, late, 0, None, False))
            continue
        t_out = t_in + timedelta(hours=rnd.uniform(3.0, 4.4)) if rnd.random() < 0.06 else start_dt + length + timedelta(minutes=rnd.randint(-45, 90))
        t_out = max(t_out, t_in + timedelta(minutes=30))
        secs = int((t_out - t_in).total_seconds() // 1) // 60 * 60  # whole minutes keep the data tidy
        t_out = t_in + timedelta(seconds=secs)
        wh = float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        over = int((t_out - (start_dt + length)).total_seconds() // 60)
        doc = record(e["emp_code"], day, status, None, None, late, over if over >= 30 else 0, wh, wh < 4.5)
        doc["punch_in"], doc["punch_out"] = t_in.astimezone(UTC), t_out.astimezone(UTC)
        logs.append(doc)
    return emps, logs


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--small", action="store_true")
    group.add_argument("--bulk", nargs="?", const=100000, type=int, metavar="N")
    args = parser.parse_args()
    from app.main import db, ensure_indexes   # same MONGO_URI / MONGO_DB as the app
    if "test" not in db.name.lower() and "scratch" not in db.name.lower():
        sys.exit(f"Refusing to seed database '{db.name}': its name must contain 'test' or 'scratch'.")
    emps, logs = build_small() if args.small else build_bulk(args.bulk)
    ensure_indexes()
    db.employees.delete_many({})          # delete_many (not drop) keeps the indexes
    db.attendance_logs.delete_many({})
    db.employees.insert_many(emps)
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000], ordered=False)
    print(f"seeded '{db.name}': {len(emps)} employees, {len(logs)} attendance records")


if __name__ == "__main__":
    main()