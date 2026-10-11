"""Locks in numbers that were verified BY HAND on the small seed dataset, so the slow reference implementation
(scripts/reference.py, the oracle for the MongoDB pipelines) cannot silently drift. No database needed."""
import pytest

import reference as ref
import seed_test_data as sd
from app.main import count_working_days, month_bounds

EMPS, LOGS = sd.build_small()


def test_month_bounds_and_working_days():
    assert month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert month_bounds("2024-02") == ("2024-02-01", "2024-02-29")      # leap year
    assert month_bounds("2026-12") == ("2026-12-01", "2026-12-31")
    assert count_working_days("2026-07-01", "2026-07-31") == 23
    assert count_working_days("2026-07-13", "2026-07-31") == 15         # mid-month joiner
    assert count_working_days("2026-07-04", "2026-07-05") == 0          # a weekend
    assert count_working_days("2026-08-10", "2026-07-31") == 0          # joined after the month


def test_reference_monthly_emp1001():
    # 23 weekdays - 1 gap (17th) = 22 records; 1 LEAVE + 1 ABSENT -> 20 present; one half day -> 19.5; Sat/Sun excluded
    r = ref.employee_monthly(EMPS, LOGS, "EMP1001", "2026-07")
    assert r == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23, "present_days": 19.5, "leave_days": 1,
                 "late_count": 4, "total_late_minutes": 96, "total_overtime_minutes": 45, "attendance_pct": 84.78}


def test_reference_monthly_edge_cases():
    assert ref.employee_monthly(EMPS, LOGS, "EMP1005", "2026-07")["present_days"] == 0.0       # no logs at all
    assert ref.employee_monthly(EMPS, LOGS, "EMP1004", "2026-07")["working_days"] == 15        # counted from joined_on
    r = ref.employee_monthly(EMPS, LOGS, "EMP4001", "2026-07")
    assert (r["working_days"], r["attendance_pct"]) == (0, None)                                # joins in August
    assert ref.employee_monthly(EMPS, LOGS, "NOPE", "2026-07") is None


def test_reference_summary_headcount_rules():
    items = {i["department"]: i for i in ref.department_summary(EMPS, LOGS, "2026-07")["items"]}
    assert list(items) == ["Engineering", "HR", "Ops", "Sales", "engineering"]                 # Legal omitted; case-sensitive; code-point order
    assert items["Engineering"]["headcount"] == 5          # includes EMP1005 (no logs) and EMP1004 (joined 13 July)
    assert items["HR"]["headcount"] == 1                   # joined on the last day of the month
    assert items["Sales"]["total_late_minutes"] == 250 and items["Sales"]["late_count"] == 6
    assert items["Ops"]["leave_count"] == 1 and items["Engineering"]["on_duty_count"] == 2
    assert ref.department_summary(EMPS, LOGS, "2026-07", "Legal")["items"] == []


def test_reference_leaderboard_ties():
    rows = ref.late_leaderboard(EMPS, LOGS, "2026-07", limit=3)["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [
        (1, "EMP2001"), (2, "EMP1001"), (3, "EMP1002"), (3, "EMP1003"), (3, "EMP2002"), (3, "EMP2003")]   # limit applies AFTER ranking
    sales = ref.late_leaderboard(EMPS, LOGS, "2026-07", limit=50, department="Sales")["items"]
    assert [(r["rank"], r["total_late_minutes"]) for r in sales] == [(1, 100), (2, 60), (2, 60), (4, 30)]    # 1, 2, 2, 4
    assert all(r["emp_code"] != "EMP9999" for r in ref.late_leaderboard(EMPS, LOGS, "2026-07", limit=50)["items"])  # unknown employee ignored


def test_reference_trend():
    rows = ref.department_trend(EMPS, LOGS, "Engineering", "2026-07-01", "2026-07-09")["items"]
    assert len(rows) == 9
    assert rows[0] == {"date": "2026-07-01", "is_working_day": True, "headcount": 4, "present_count": 3.0, "late_count": 0,
                       "attendance_rate": 0.75, "moving_avg_7d": 0.75}
    assert rows[3]["is_working_day"] is False and rows[3]["attendance_rate"] is None and rows[3]["present_count"] == 0.5
    assert rows[8]["attendance_rate"] == 0.625 and rows[8]["moving_avg_7d"] == 0.725    # window = 7 ROWS (3-9 July); the 2 weekend rows are null, so (4 x 0.75 + 0.625) / 5
    empty = ref.department_trend(EMPS, LOGS, "Engineering", "2020-01-01", "2020-01-10")["items"]
    assert len(empty) == 10 and empty[0]["headcount"] == 0 and empty[0]["attendance_rate"] is None   # nobody had joined yet
    assert ref.department_trend(EMPS, LOGS, "NoSuchDept", "2026-07-01", "2026-07-05") is None