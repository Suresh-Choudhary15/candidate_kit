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


# ---------------- GET /attendance ----------------
DAY = 86_400_000


def seed_days(api):
    """EMP0001, EMP0002 (Engineering) and EMP0003 (Sales), punched in on 2026-07-06 / 07 / 08 (all 09:40:00 IST)."""
    for code, dept in (("EMP0001", "Engineering"), ("EMP0002", "Engineering"), ("EMP0003", "Sales")):
        api.post("/employees", json=employee(code, dept))
    for i in range(3):
        for code in ("EMP0003", "EMP0001", "EMP0002"):          # inserted out of order on purpose
            punch_in(api, code, T_0940_00 + i * DAY, status="WFH" if (code == "EMP0002" and i == 1) else "PRESENT")


def test_list_attendance_sort_total_and_shape(api):
    seed_days(api)
    r = api.get("/attendance", params={"page_size": 100}).json()
    assert r["total"] == 9 and r["page"] == 1 and r["page_size"] == 100
    keys = [(i["date"], i["emp_code"]) for i in r["items"]]
    # date descending, then emp_code ascending
    assert keys[:3] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-08", "EMP0003")]
    assert keys[-1] == ("2026-07-06", "EMP0003")
    assert all("_id" not in i and "id" not in i for i in r["items"])


def test_list_attendance_filters(api):
    seed_days(api)
    r = api.get("/attendance", params={"emp_code": "EMP0001"}).json()
    assert r["total"] == 3 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07", "2026-07-06"]
    r = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 3 and {i["date"] for i in r["items"]} == {"2026-07-07"}          # inclusive bounds
    r = api.get("/attendance", params={"status": "WFH"}).json()
    assert r["total"] == 1 and r["items"][0]["emp_code"] == "EMP0002"
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07"}).json()
    assert r["total"] == 2
    assert api.get("/attendance", params={"emp_code": "NOPE"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_list_attendance_pagination(api):
    seed_days(api)
    p1 = api.get("/attendance", params={"page": 1, "page_size": 4}).json()
    p3 = api.get("/attendance", params={"page": 3, "page_size": 4}).json()
    assert len(p1["items"]) == 4 and len(p3["items"]) == 1 and p3["total"] == 9
    assert api.get("/attendance", params={"page": 99}).json()["items"] == []


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"}, {"date_from": "2026-02-30"}, {"date_to": "bad"},
    {"date_from": "2026-7-1"}, {"status": "BOGUS"}, {"page": 0}, {"page_size": 0}, {"page_size": 101},
])
def test_list_attendance_422(api, params):
    assert api.get("/attendance", params=params).status_code == 422


def test_list_attendance_legacy_document_defaults(api):
    api.post("/employees", json=employee("EMP0002"))
    m.db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                     "punch_in": m.ms_to_dt(T_0940_00), "punch_out": m.ms_to_dt(T_1835),
                                     "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    item = api.get("/attendance").json()["items"][0]
    assert item["half_day"] is False and item["history"] == []
    m.db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-15", "status": "ABSENT",
                                     "punch_in": None, "punch_out": None, "work_hours": None})
    item = api.get("/attendance", params={"status": "ABSENT"}).json()["items"][0]
    assert (item["late_minutes"], item["overtime_minutes"], item["half_day"], item["history"]) == (0, 0, False, [])


def test_attendance_query_builder_sort():
    q = m.build_attendance_query(None, None, None, None, 1, 20)
    assert q["sort"] == [("date", -1), ("emp_code", 1)] and q["skip"] == 0 and q["limit"] == 20
    q = m.build_attendance_query("EMP0001", "2026-07-01", "2026-07-31", "PRESENT", 3, 10)
    assert q["filter"] == {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "PRESENT"}
    assert q["skip"] == 20 and q["sort"] == [("date", -1)]


# ---------------- PATCH /attendance/{emp_code}/{date} ----------------
def patch(api, code, day, **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return api.patch(f"/attendance/{code}/{day}", json=body)


@pytest.fixture()
def closed_record(api):
    """EMP0003: WFH, in 10:15:59 (late 45), out 19:10:00 (overtime 40) on 2026-07-06."""
    api.post("/employees", json=employee("EMP0003", "Sales"))
    punch_in(api, "EMP0003", T_1015_59, status="WFH")
    punch_out(api, "EMP0003", T_1910)
    return api


def test_patch_punch_in_recomputes_and_audits(closed_record):
    r = patch(closed_record, "EMP0003", "2026-07-06", punch_in=T_0928)
    assert r.status_code == 200
    j = r.json()
    assert (j["punch_in"], j["late_minutes"], j["work_hours"], j["overtime_minutes"]) == (T_0928, 0, 9.7, 40)
    assert len(j["history"]) == 1
    h = j["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {
        "punch_in": {"from": T_1015_59, "to": T_0928},
        "late_minutes": {"from": 45, "to": 0},
        "work_hours": {"from": 8.9, "to": 9.7},
    }                                                          # overtime did not change, so it is not listed
    stored = m.db.attendance_logs.find_one({"emp_code": "EMP0003"})
    assert stored["history"][0]["changes"]["punch_in"]["to"] == m.ms_to_dt(T_0928)  # BSON date in Mongo, ms in the API


def test_patch_appends_history_never_overwrites(closed_record):
    patch(closed_record, "EMP0003", "2026-07-06", punch_in=T_0928)
    j = patch(closed_record, "EMP0003", "2026-07-06", status="ON_DUTY", reason="was on client duty", regularized_by="boss").json()
    assert j["status"] == "ON_DUTY" and len(j["history"]) == 2
    assert j["history"][0]["by"] == "hr.admin" and j["history"][1]["by"] == "boss"
    assert j["history"][1]["changes"] == {"status": {"from": "WFH", "to": "ON_DUTY"}}


def test_patch_422_cases(closed_record):
    api = closed_record
    assert patch(api, "EMP0003", "2026-07-06", punch_in=T_1015_59).status_code == 422                       # changes nothing
    assert patch(api, "EMP0003", "2026-07-06").status_code == 422                                          # empty correction
    assert patch(api, "EMP0003", "2026-07-06", status="LEAVE", punch_in=T_0928).status_code == 422          # contradiction
    assert patch(api, "EMP0003", "2026-07-06", status="ABSENT", punch_out=T_1910).status_code == 422
    assert patch(api, "EMP0003", "2026-07-06", punch_in=T_0928 + DAY).status_code == 422                   # other day
    assert patch(api, "EMP0003", "2026-07-06", punch_out=T_1015_59 - 1000).status_code == 422              # before punch_in
    assert patch(api, "EMP0003", "2026-07-06", punch_out=T_1015_59 + DAY + 1000).status_code == 422         # > 24 h
    assert patch(api, "EMP0003", "2026-07-06", punch_in=T_0928, reason="x").status_code == 422              # reason too short
    assert patch(api, "EMP0003", "2026-07-06", punch_in=T_0928, regularized_by="").status_code == 422
    assert patch(api, "EMP0003", "2026-07-06", punch_in=1783310280).status_code == 422                      # seconds
    assert patch(api, "EMP0003", "2026-07-06", status="BOGUS").status_code == 422
    assert patch(api, "EMP0003", "2026-02-30", status="LEAVE").status_code == 422                           # bad path date
    assert m.db.attendance_logs.find_one({"emp_code": "EMP0003"}).get("history") == []                     # nothing was written


def test_patch_404_cases(closed_record):
    assert patch(closed_record, "EMP0099", "2026-07-06", status="LEAVE").status_code == 404
    assert patch(closed_record, "EMP0003", "2026-07-20", status="LEAVE").status_code == 404


def test_patch_to_leave_clears_times_then_back_needs_punch_in(closed_record):
    api = closed_record
    j = patch(api, "EMP0003", "2026-07-06", status="LEAVE", reason="approved leave").json()
    assert (j["status"], j["punch_in"], j["punch_out"], j["work_hours"], j["late_minutes"], j["overtime_minutes"]) == \
           ("LEAVE", None, None, None, 0, 0)
    ch = j["history"][0]["changes"]
    assert ch["status"] == {"from": "WFH", "to": "LEAVE"}
    assert ch["punch_in"] == {"from": T_1015_59, "to": None} and ch["punch_out"] == {"from": T_1910, "to": None}
    assert ch["work_hours"] == {"from": 8.9, "to": None} and ch["late_minutes"] == {"from": 45, "to": 0}
    assert ch["overtime_minutes"] == {"from": 40, "to": 0}
    assert patch(api, "EMP0003", "2026-07-06", status="PRESENT", reason="mistake, was present").status_code == 422
    j = patch(api, "EMP0003", "2026-07-06", status="PRESENT", punch_in=T_0928, reason="mistake, was present").json()
    assert j["status"] == "PRESENT" and j["late_minutes"] == 0 and j["work_hours"] is None and len(j["history"]) == 2


def test_patch_absent_record_to_present(api):
    api.post("/employees", json=employee())
    m.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT",
                                     "punch_in": None, "punch_out": None, "work_hours": None})   # legacy: no history field
    j = patch(api, "EMP0001", "2026-07-08", status="PRESENT", punch_in=T_0940_00 + 2 * DAY, punch_out=T_1835 + 2 * DAY).json()
    assert j["work_hours"] == 8.92 and len(j["history"]) == 1


def test_patch_overnight_record_keeps_date(api):
    api.post("/employees", json=employee("EMP0005", start="22:00", end="06:00"))
    punch_in(api, "EMP0005", T_2155)
    j = patch(api, "EMP0005", "2026-07-06", punch_out=T_NEXT_0640).json()
    assert (j["work_hours"], j["overtime_minutes"]) == (8.75, 40)
    # a punch_in that falls on the next IST day but before shift_end still belongs to 2026-07-06
    assert patch(api, "EMP0005", "2026-07-06", punch_in=T_NEXT_0130 - 3600_000).status_code == 200
    assert patch(api, "EMP0005", "2026-07-06", punch_in=T_NEXT_0130 + 6 * 3600_000).status_code == 422


def test_patch_ignores_client_supplied_derived_fields(closed_record):
    j = patch(closed_record, "EMP0003", "2026-07-06", punch_in=T_0928, late_minutes=99, work_hours=1, half_day=True).json()
    assert (j["late_minutes"], j["work_hours"], j["half_day"]) == (0, 9.7, False)


def test_patch_concurrent_modification_returns_409(closed_record, monkeypatch):
    """Simulate another writer slipping in between our read and our write."""
    coll = m.db.attendance_logs
    real_update = coll.update_one

    def racing_update(flt, update, *a, **kw):
        real_update({"emp_code": "EMP0003"}, {"$push": {"history": {"at": m.now_utc(), "by": "other", "reason": "race",
                                                                   "changes": {"status": {"from": "WFH", "to": "WFH"}}}}})
        return real_update(flt, update, *a, **kw)

    monkeypatch.setattr(coll, "update_one", racing_update)
    assert patch(closed_record, "EMP0003", "2026-07-06", punch_in=T_0928).status_code == 409
    assert len(m.db.attendance_logs.find_one({"emp_code": "EMP0003"})["history"]) == 1   # the other writer's entry survived


# ---------------- robustness: database down / indexes created late ----------------
def test_unreachable_database_gives_503_not_500(monkeypatch):
    bad = m.MongoClient("mongodb://localhost:1", tz_aware=True, serverSelectionTimeoutMS=200)
    monkeypatch.setattr(m, "client", bad)
    monkeypatch.setattr(m, "db", bad["x"])
    c = TestClient(m.app)
    assert c.get("/health").status_code == 503
    assert c.get("/employees").status_code == 503
    assert c.post("/employees", json=employee()).status_code == 503


def test_health_creates_missing_indexes(monkeypatch):
    client = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", client)
    monkeypatch.setattr(m, "db", client["late_db"])
    monkeypatch.setattr(m, "_indexes_ready", False)
    assert "uq_emp_code" not in client["late_db"].employees.index_information()
    assert TestClient(m.app).get("/health").status_code == 200
    assert "uq_emp_code" in client["late_db"].employees.index_information()
    assert "uq_emp_date" in client["late_db"].attendance_logs.index_information()


# ---------------- /admin/explain validation (the explain command itself needs a real MongoDB) ----------------
@pytest.mark.parametrize("path", [
    "/admin/explain/nonsense",
    "/admin/explain/employee_monthly",                                  # emp_code + month missing
    "/admin/explain/employee_monthly?emp_code=EMP0001",                 # month missing
    "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13",
    "/admin/explain/department_summary",
    "/admin/explain/late_leaderboard?month=2026-07&limit=0",
    "/admin/explain/late_leaderboard?month=2026-07&limit=51",
    "/admin/explain/department_trend?department=Sales&from=2026-07-01",
    "/admin/explain/department_trend?department=Sales&from=2026-07-10&to=2026-07-01",
    "/admin/explain/department_trend?department=Sales&from=2026-01-01&to=2026-04-03",   # 93 days
    "/admin/explain/department_trend?from=2026-07-01&to=2026-07-05",                      # department missing
    "/admin/explain/attendance_list?date_from=2026-02-30",
    "/admin/explain/attendance_list?status=BOGUS",
    "/admin/explain/attendance_list?page=0",
])
def test_explain_validation_422(api, path):
    assert api.get(path).status_code == 422


def test_explain_runs_the_same_query_builders(api, monkeypatch):
    """The explain endpoint must hand MongoDB exactly what the real endpoint builds. mongomock has no explain, so capture the command."""
    seen = {}
    monkeypatch.setattr(m.db, "command", lambda cmd, *a, **kw: seen.update(cmd=cmd) or {"ok": 1.0, "inf": float("inf")})
    r = api.get("/admin/explain/attendance_list", params={"emp_code": "EMP0001", "date_from": "2026-07-01", "page": 2, "page_size": 10})
    expected = m.build_attendance_query("EMP0001", "2026-07-01", None, None, 2, 10)
    assert r.status_code == 200 and r.json()["collection"] == "attendance_logs"
    assert r.json()["explain"]["inf"] == {"$numberDouble": "Infinity"}          # valid JSON, never a bare Infinity
    inner = seen["cmd"]["explain"]
    assert (inner["filter"], inner["skip"], inner["limit"]) == (expected["filter"], expected["skip"], expected["limit"])
    assert seen["cmd"]["verbosity"] == "executionStats" and list(seen["cmd"])[0] == "explain"
    r = api.get("/admin/explain/department_summary", params={"month": "2026-07", "department": "Sales"})
    _, pipeline, hint = m.build_department_summary_pipeline("2026-07", "Sales")
    assert seen["cmd"]["explain"]["pipeline"] == pipeline and seen["cmd"]["explain"]["hint"] == hint
    r = api.get("/admin/explain/department_trend", params={"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"})
    assert r.status_code == 200 and seen["cmd"]["explain"]["aggregate"] == "employees"