"""API tests against an in-memory fake MongoDB (mongomock): fast, no Docker needed.
They check logic and status codes. Real concurrency and aggregation features are tested against real MongoDB."""
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m


@pytest.fixture()
def api(monkeypatch):
    client = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", client)
    monkeypatch.setattr(m, "db", client["test_db"])
    m.ensure_indexes()
    return TestClient(m.app)


def employee(code="EMP0001", dept="Engineering", start="09:30", end="18:30", **extra):
    body = {"emp_code": code, "name": "Asha", "email": "asha@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": "2026-01-05"}
    body.update(extra)
    return body


# IST epoch ms on 2026-07-06 (Monday)
T_0928, T_0940_00, T_0940_01, T_1015_59 = 1783310280000, 1783311000000, 1783311001000, 1783313159000
T_1835, T_1910, T_2155, T_NEXT_0640, T_NEXT_0130 = 1783343100000, 1783345200000, 1783355100000, 1783386600000, 1783368000000


# ---------------- employees ----------------
def test_create_employee_ok_and_shape(api):
    r = api.post("/employees", json=employee())
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(body["created_at"], int) and body["created_at"] > 10**12
    assert "_id" not in body


def test_create_employee_defaults_and_ignored_fields(api):
    payload = employee()
    del payload["shift_start"], payload["shift_end"]
    payload["created_at"] = 5
    body = api.post("/employees", json=payload).json()
    assert (body["shift_start"], body["shift_end"]) == ("09:30", "18:30")
    assert body["created_at"] != 5


def test_duplicate_employee_409(api):
    assert api.post("/employees", json=employee()).status_code == 201
    assert api.post("/employees", json=employee()).status_code == 409


@pytest.mark.parametrize("bad", [
    {"emp_code": "EMP1"}, {"emp_code": "emp0001"}, {"emp_code": "EMP1234567"}, {"emp_code": "EMP١٢٣٤"},
    {"name": ""}, {"email": "nope"}, {"department": ""}, {"shift_start": "9:30"}, {"shift_end": "24:00"},
    {"joined_on": "2026-02-30"}, {"joined_on": "2026-7-6"}, {"joined_on": "yesterday"},
    {"shift_start": "09:00", "shift_end": "09:00"},
])
def test_create_employee_validation_422(api, bad):
    assert api.post("/employees", json=employee(**bad)).status_code == 422


def test_create_employee_missing_field_422(api):
    payload = employee()
    del payload["department"]
    assert api.post("/employees", json=payload).status_code == 422


def test_list_employees_filter_sort_paginate(api):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Engineering"), ("EMP0004", "Sales")]:
        api.post("/employees", json=employee(code, dept))
    r = api.get("/employees").json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]
    assert r["total"] == 4
    r = api.get("/employees", params={"department": "Sales"}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]
    assert api.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    r = api.get("/employees", params={"page": 2, "page_size": 3}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0004"] and r["total"] == 4 and r["page"] == 2
    assert all("_id" not in e for e in r["items"])


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_pagination_422(api, params):
    assert api.get("/employees", params=params).status_code == 422


# ---------------- punch-in ----------------
def punch_in(api, code, ms=None, **extra):
    body = {"emp_code": code, **extra}
    if ms is not None:
        body["punched_at"] = ms
    return api.post("/attendance/punch-in", json=body)


def test_punch_in_late_rules(api):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        api.post("/employees", json=employee(code))
    r = punch_in(api, "EMP0001", T_0940_00)
    assert r.status_code == 201
    assert r.json() == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": T_0940_00,
                        "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                        "half_day": False, "history": []}
    assert punch_in(api, "EMP0002", T_0940_01).json()["late_minutes"] == 10
    r = punch_in(api, "EMP0003", T_1015_59, status="WFH").json()
    assert (r["late_minutes"], r["status"]) == (45, "WFH")


def test_punch_in_truncates_to_seconds(api):
    api.post("/employees", json=employee())
    assert punch_in(api, "EMP0001", T_0940_01 + 999).json()["punch_in"] == T_0940_01


def test_punch_in_errors(api):
    api.post("/employees", json=employee())
    assert punch_in(api, "EMP0099", T_0940_00).status_code == 404
    assert punch_in(api, "EMP0001", T_0940_00).status_code == 201
    assert punch_in(api, "EMP0001", T_0940_00).status_code == 409
    assert punch_in(api, "EMP0001", T_0940_00 + 60_000).status_code == 409  # same attendance date


@pytest.mark.parametrize("bad", [
    {"punched_at": 1783311000}, {"punched_at": 0}, {"punched_at": -5}, {"punched_at": 1783311000000.5},
    {"punched_at": "1783311000000"}, {"punched_at": True}, {"punched_at": 99_999_999_999},
    {"punched_at": 4_102_444_800_001}, {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "junk"},
    {"emp_code": None},
])
def test_punch_in_validation_422(api, bad):
    api.post("/employees", json=employee())
    body = {"emp_code": "EMP0001", "punched_at": T_0940_00, **bad}
    assert api.post("/attendance/punch-in", json=body).status_code == 422


def test_punch_in_without_time_uses_now_and_ignores_derived_fields(api):
    api.post("/employees", json=employee())
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "late_minutes": 99, "work_hours": 5})
    assert r.status_code == 201
    assert r.json()["work_hours"] is None and r.json()["punch_in"] > 10**12


def test_punch_in_overnight_date(api):
    api.post("/employees", json=employee("EMP0005", start="22:00", end="06:00"))
    api.post("/employees", json=employee("EMP0006", start="22:00", end="06:00"))
    assert punch_in(api, "EMP0005", T_2155).json()["date"] == "2026-07-06"
    r = punch_in(api, "EMP0006", T_NEXT_0130).json()
    assert r["date"] == "2026-07-06"  # 01:30 IST on the 7th belongs to the shift that started on the 6th


# ---------------- punch-out ----------------
def punch_out(api, code, ms):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms})


def test_punch_out_normal_and_overtime(api):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/employees", json=employee("EMP0003", "Sales"))
    punch_in(api, "EMP0001", T_0940_00)
    punch_in(api, "EMP0003", T_1015_59, status="WFH")
    r = punch_out(api, "EMP0001", T_1835)
    assert r.status_code == 200
    j = r.json()
    assert (j["work_hours"], j["overtime_minutes"], j["half_day"], j["punch_out"]) == (8.92, 0, False, T_1835)
    j = punch_out(api, "EMP0003", T_1910).json()
    assert (j["work_hours"], j["overtime_minutes"], j["late_minutes"]) == (8.9, 40, 45)


def test_punch_out_persists(api):
    api.post("/employees", json=employee())
    punch_in(api, "EMP0001", T_0940_00)
    punch_out(api, "EMP0001", T_1835)
    rec = m.db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert rec["work_hours"] == 8.92 and rec["punch_out"] is not None


def test_punch_out_errors(api):
    api.post("/employees", json=employee("EMP0001"))
    api.post("/employees", json=employee("EMP0002"))
    punch_in(api, "EMP0001", T_0940_00)
    punch_in(api, "EMP0002", T_0940_01)
    assert punch_out(api, "EMP0099", T_1835).status_code == 404            # unknown employee
    api.post("/employees", json=employee("EMP0004"))
    assert punch_out(api, "EMP0004", T_1835).status_code == 404            # employee without any punch-in
    assert punch_out(api, "EMP0002", T_0940_00).status_code == 404         # before its only punch-in
    assert punch_out(api, "EMP0002", T_0940_01).status_code == 422         # equal to punch-in
    assert punch_out(api, "EMP0002", T_0940_01 + 86_400_000 + 1000).status_code == 422  # > 24 h
    assert punch_out(api, "EMP0002", T_0940_01 + 86_400_000).status_code == 200         # exactly 24 h is allowed
    assert punch_out(api, "EMP0001", T_1835).status_code == 200
    assert punch_out(api, "EMP0001", T_1835).status_code == 409            # already punched out


def test_punch_out_validation_422(api):
    api.post("/employees", json=employee())
    for bad in (1783343100, 1783343100000.5, "x", 0):
        r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422


def test_punch_out_half_day(api):
    api.post("/employees", json=employee())
    punch_in(api, "EMP0001", T_0940_00)
    j = punch_out(api, "EMP0001", T_0940_00 + 3 * 3600_000 + 1800_000).json()  # 3.5 h
    assert (j["work_hours"], j["half_day"]) == (3.5, True)


def test_punch_out_overnight_crosses_midnight(api):
    api.post("/employees", json=employee("EMP0005", start="22:00", end="06:00"))
    punch_in(api, "EMP0005", T_2155)
    j = punch_out(api, "EMP0005", T_NEXT_0640).json()
    assert (j["date"], j["work_hours"], j["overtime_minutes"], j["late_minutes"]) == ("2026-07-06", 8.75, 40, 0)


def test_punch_out_closes_latest_record(api):
    api.post("/employees", json=employee())
    punch_in(api, "EMP0001", T_0940_00)
    punch_out(api, "EMP0001", T_1835)
    day2 = T_0940_00 + 86_400_000
    punch_in(api, "EMP0001", day2)
    j = punch_out(api, "EMP0001", day2 + 8 * 3600_000).json()
    assert j["date"] == "2026-07-07"
