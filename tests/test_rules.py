"""Unit tests for the business-rule helpers (R1-R5). No database needed."""
from datetime import datetime, timedelta

import pytest

from app.main import (IST, attendance_date, calc_half_day, calc_late_minutes, calc_overtime_minutes,
                      calc_work_hours, derive_fields, dt_to_ms, is_overnight, ms_to_dt, round_half_up,
                      shift_bounds)

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# ---------------- conversions / truncation ----------------
def test_ms_round_trip_and_truncation():
    ms = 1783311000900  # 09:40:00.900 IST on 2026-07-06
    dt = ms_to_dt(ms)
    assert dt.microsecond == 0
    assert dt_to_ms(dt) == 1783311000000
    assert dt == ist(2026, 7, 6, 9, 40, 0)


def test_dt_to_ms_naive_is_utc():
    assert dt_to_ms(datetime(1970, 1, 1, 0, 0, 1)) == 1000


# ---------------- R2 late ----------------
@pytest.mark.parametrize("h,m,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after start: still inside grace
    (9, 40, 1, 10),     # one second beyond grace: late, counted from shift start
    (10, 15, 59, 45),
    (9, 20, 0, 0),      # early arrival
    (9, 30, 0, 0),
])
def test_late_minutes(h, m, s, expected):
    start, _ = shift_bounds("2026-07-06", "09:30", "18:30")
    assert calc_late_minutes(ist(2026, 7, 6, h, m, s), start) == expected


def test_late_ignores_subsecond_after_truncation():
    start, _ = shift_bounds("2026-07-06", "09:30", "18:30")
    punch = ms_to_dt(1783311000900)  # 09:40:00.900 truncated -> 09:40:00
    assert calc_late_minutes(punch, start) == 0


# ---------------- R3 overtime ----------------
@pytest.mark.parametrize("h,m,s,expected", [
    (18, 59, 59, 0),    # 29m59s
    (19, 0, 0, 30),     # exactly 30m
    (19, 10, 0, 40),
    (19, 10, 59, 40),   # floor
    (18, 35, 0, 0),
    (17, 0, 0, 0),      # left early
])
def test_overtime(h, m, s, expected):
    _, end = shift_bounds("2026-07-06", "09:30", "18:30")
    assert calc_overtime_minutes(ist(2026, 7, 6, h, m, s), end) == expected


# ---------------- R1 overnight ----------------
def test_is_overnight():
    assert is_overnight("22:00", "06:00")
    assert is_overnight("09:00", "09:00")
    assert not is_overnight("09:30", "18:30")


def test_overnight_attendance_date():
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 6, 22, 30), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 1, 30), "22:00", "06:00") == "2026-07-06"   # previous day
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"  # not earlier than end
    assert attendance_date(ist(2026, 7, 7, 1, 30), "09:30", "18:30") == "2026-07-07"    # day shift: plain date


def test_attendance_date_uses_ist_not_utc():
    # 2026-07-06 23:00 UTC is 2026-07-07 04:30 IST
    from datetime import timezone
    assert attendance_date(datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc), "09:30", "18:30") == "2026-07-07"


def test_overnight_late_and_overtime():
    start, end = shift_bounds("2026-07-06", "22:00", "06:00")
    assert start == ist(2026, 7, 6, 22, 0)
    assert end == ist(2026, 7, 7, 6, 0)
    assert calc_late_minutes(ist(2026, 7, 6, 22, 30), start) == 30
    assert calc_late_minutes(ist(2026, 7, 6, 21, 55), start) == 0
    assert calc_overtime_minutes(ist(2026, 7, 7, 6, 40), end) == 40


# ---------------- R4 / R5 ----------------
def test_work_hours_half_up_and_half_day():
    t0 = ist(2026, 7, 6, 9, 0)
    assert calc_work_hours(t0, t0 + timedelta(seconds=16182)) == 4.50   # 4.495 -> 4.50 (half-up)
    assert calc_half_day(4.50) is False
    assert calc_work_hours(t0, t0 + timedelta(seconds=16178)) == 4.49
    assert calc_half_day(4.49) is True
    assert calc_work_hours(t0, t0 + timedelta(hours=3, minutes=30)) == 3.5


def test_half_day_none():
    assert calc_half_day(None) is False


def test_round_half_up():
    assert round_half_up(0.03125, 4) == 0.0313
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(0.125, 2) == 0.13
    assert round_half_up(1.005, 2) == 1.01
    assert round_half_up(2.5, 0) == 3.0


# ---------------- derive_fields: matches the sample documents in DATA_MODEL.md ----------------
def test_derive_sample_normal_day():
    f = derive_fields("PRESENT", ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35), DAY, "2026-07-06")
    assert f == {"work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_derive_sample_late_with_overtime():
    f = derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10), DAY, "2026-07-06")
    assert f == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}


def test_derive_sample_open_record():
    f = derive_fields("PRESENT", ist(2026, 7, 7, 9, 35), None, DAY, "2026-07-07")
    assert f == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_derive_sample_half_day_wfh():
    f = derive_fields("WFH", ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0), DAY, "2026-07-07")
    assert f == {"work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True}


def test_derive_sample_overnight():
    f = derive_fields("PRESENT", ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40), NIGHT, "2026-07-06")
    assert f == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_derive_absent_leave(status):
    f = derive_fields(status, None, None, DAY, "2026-07-08")
    assert f == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
