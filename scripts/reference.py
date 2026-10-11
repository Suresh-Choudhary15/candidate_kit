"""A deliberately simple, slow, loop-based implementation of the four analytics (the 'oracle').
It works on plain Python lists of documents (as read from MongoDB) and shares NO code with the API:
if the fast MongoDB pipelines and this slow version disagree, one of them is wrong, and we find out which by hand.
Rules implemented straight from openapi.yaml: R5-R10, rounding half-up with Decimal."""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def hu(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def bounds(month: str):
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def weekday(day: str) -> bool:
    return date.fromisoformat(day).weekday() < 5


def presence_value(log: dict) -> Decimal:
    """0 for absent/leave, 1 for a present day, 0.5 for a present half day."""
    if log["status"] not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def late(log: dict) -> int:
    return log.get("late_minutes") or 0


def employee_monthly(employees, logs, emp_code, month):
    emp = next((e for e in employees if e["emp_code"] == emp_code), None)
    if emp is None:
        return None
    first, last = bounds(month)
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working, day = 0, start
    while day <= date.fromisoformat(last):
        working += day.weekday() < 5
        day += timedelta(days=1)
    mine = [l for l in logs if l["emp_code"] == emp_code and first <= l["date"] <= last]
    present = sum((presence_value(l) for l in mine if weekday(l["date"])), Decimal(0))
    return {
        "emp_code": emp_code, "month": month, "working_days": working, "present_days": float(present),
        "leave_days": sum(1 for l in mine if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in mine if late(l) > 0),
        "total_late_minutes": sum(late(l) for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": hu(present / Decimal(working) * 100, 2) if working else None,
    }


def department_summary(employees, logs, month, department=None):
    first, last = bounds(month)
    items = []
    for dept in sorted({e["department"] for e in employees}):
        if department is not None and dept != department:
            continue
        members = {e["emp_code"] for e in employees if e["department"] == dept and e["joined_on"] <= last}
        if not members:
            continue
        mine = [l for l in logs if l["emp_code"] in members and first <= l["date"] <= last]
        hours = [Decimal(str(l["work_hours"])) for l in mine if l["status"] in PRESENCE and l.get("work_hours") is not None]
        items.append({
            "department": dept, "headcount": len(members),
            "present_days": float(sum((presence_value(l) for l in mine if weekday(l["date"])), Decimal(0))),
            "avg_work_hours": hu(sum(hours) / Decimal(len(hours)), 2) if hours else None,
            "late_count": sum(1 for l in mine if late(l) > 0),
            "total_late_minutes": sum(late(l) for l in mine),
            "leave_count": sum(1 for l in mine if l["status"] == "LEAVE"),
            "on_duty_count": sum(1 for l in mine if l["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def late_leaderboard(employees, logs, month, limit=10, department=None):
    first, last = bounds(month)
    by_code = {e["emp_code"]: e for e in employees}
    totals = {}
    for l in logs:
        if first <= l["date"] <= last and l["emp_code"] in by_code:
            entry = totals.setdefault(l["emp_code"], [0, 0])
            entry[0] += late(l)
            entry[1] += 1 if late(l) > 0 else 0
    rows = [(code, t, c) for code, (t, c) in totals.items() if t > 0 and (department is None or by_code[code]["department"] == department)]
    rows.sort(key=lambda r: (-r[1], r[0]))
    out = []
    for position, (code, total, count) in enumerate(rows):
        rank = 1 + sum(1 for r in rows if r[1] > total)           # 1 + number of people strictly worse off
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"], "department": by_code[code]["department"],
                        "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": out}


def department_trend(employees, logs, department, date_from, date_to):
    members = [e for e in employees if e["department"] == department]
    if not members:
        return None
    codes = {e["emp_code"] for e in members}
    rows, rates = [], []
    day, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    while day <= end:
        ds = day.isoformat()
        todays = [l for l in logs if l["emp_code"] in codes and l["date"] == ds]
        headcount = sum(1 for e in members if e["joined_on"] <= ds)
        present = sum((presence_value(l) for l in todays), Decimal(0))
        working = day.weekday() < 5
        rate = Decimal(str(hu(present / Decimal(headcount), 4))) if working and headcount > 0 else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({
            "date": ds, "is_working_day": working, "headcount": headcount, "present_count": float(present),
            "late_count": sum(1 for l in todays if late(l) > 0),
            "attendance_rate": float(rate) if rate is not None else None,
            "moving_avg_7d": hu(sum(window) / Decimal(len(window)), 4) if window else None,
        })
        day += timedelta(days=1)
    return {"department": department, "items": rows}