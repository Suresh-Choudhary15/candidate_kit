"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

All application code lives in this single file, organised in sections:
Config -> Time helpers -> Rules (R1-R5) -> Indexes -> App & health -> (endpoints are added below).
"""
import calendar
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # override=False: real environment variables win over .env

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = frozenset({"PRESENT", "WFH", "ON_DUTY"})  # R6: these count as present
STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"})

GRACE_SECONDS = 10 * 60          # R2: late only if STRICTLY more than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = 4.5       # R5

MIN_EPOCH_MS = 100_000_000_000   # anything smaller looks like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01
MAX_PAGE = 1_000_000_000
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> integer epoch milliseconds. A naive datetime is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)  # exact integer arithmetic, no float


def now_utc() -> datetime:
    """Current time as an aware UTC datetime, truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value, places: int) -> float:
    """Round half-up (R8). Python's round() and MongoDB's $round are banker's rounding: not used."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Overnight shift when shift_end <= shift_start (R1), e.g. 22:00-06:00."""
    return _hhmm_to_minutes(shift_end) <= _hhmm_to_minutes(shift_start)


# --------------------------------------------------------------------------- #
# Rules R1-R5 (pure functions, no database access)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in. For an overnight shift, a punch-in whose IST
    time-of-day is earlier than shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_of_day < _hhmm_to_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple:
    """(start, end) of the shift as aware datetimes, anchored on the ATTENDANCE date in IST.
    For an overnight shift the end falls on the next calendar day (R1)."""
    y, mo, d = (int(x) for x in date_str.split("-"))
    sh, sm = (int(x) for x in shift_start.split(":"))
    eh, em = (int(x) for x in shift_end.split(":"))
    start = datetime(y, mo, d, sh, sm, tzinfo=IST)
    end = datetime(y, mo, d, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def calc_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if punch-in is strictly more than 600 s after shift start; then the
    whole minutes (floor) since shift start, not since the end of the grace period."""
    seconds = (punch_in - shift_start_dt) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def calc_overtime_minutes(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes (floor) after shift end, counted only if >= 30, else 0."""
    seconds = (punch_out - shift_end_dt) // timedelta(seconds=1)
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def calc_half_day(work_hours: Optional[float]) -> bool:
    """R5: half day when the ROUNDED work_hours < 4.50."""
    return work_hours is not None and work_hours < HALF_DAY_BELOW_HOURS


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  employee: dict, date_str: str) -> dict:
    """The four derived fields stored with every record (DATA_MODEL.md section 3)."""
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return result  # ABSENT / LEAVE (or nothing to compute from)
    start_dt, end_dt = shift_bounds(date_str, employee["shift_start"], employee["shift_end"])
    result["late_minutes"] = calc_late_minutes(punch_in, start_dt)
    if punch_out is not None:
        result["work_hours"] = calc_work_hours(punch_in, punch_out)
        result["overtime_minutes"] = calc_overtime_minutes(punch_out, end_dt)
        result["half_day"] = calc_half_day(result["work_hours"])
    return result


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent: same name + same spec = no-op)
# --------------------------------------------------------------------------- #
EMPLOYEE_INDEXES = [
    IndexModel([("emp_code", ASCENDING)], name="uq_emp_code", unique=True),              # identity + race-safe create
    IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp"),   # list by department, sorted by emp_code
    IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"),  # headcount per department
    IndexModel([("joined_on", ASCENDING)], name="joined"),                               # headcount without a department filter
]
ATTENDANCE_INDEXES = [
    IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], name="uq_emp_date", unique=True),  # one record per day; race-safe punch-in
    IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"),                 # global list (date desc, emp_code asc) and month scans
    IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"),
    IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin"),          # punch-out: latest record at/before a time
]


_indexes_ready = False  # set once every index exists; /health retries if startup could not do it


def ensure_indexes() -> None:
    global _indexes_ready
    db.employees.create_indexes(EMPLOYEE_INDEXES)
    db.attendance_logs.create_indexes(ATTENDANCE_INDEXES)
    _indexes_ready = True


def ensure_indexes_with_retry(seconds: int = 15) -> None:
    """MongoDB may still be starting: retry every second, then log and carry on (never crash).
    If this gives up, /health creates the indexes later as soon as MongoDB answers."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            ensure_indexes()
            log.info("indexes ready")
            return
        except PyMongoError as exc:
            if time.monotonic() >= deadline:
                log.error("could not create indexes at startup: %s", exc)
                return
            time.sleep(1)


# --------------------------------------------------------------------------- #
# App & health
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes_with_retry()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
def database_unreachable(_request: Request, exc: ConnectionFailure):
    """Any endpoint that cannot reach MongoDB answers 503 instead of a 500 traceback."""
    return JSONResponse(status_code=503, content={"detail": f"database unavailable: {exc.__class__.__name__}"})


@app.get("/health")
def health():
    """200 only if MongoDB answers a ping, otherwise 503. Also makes sure the indexes exist."""
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, f"database unavailable: {exc.__class__.__name__}")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Validation helpers & models
# --------------------------------------------------------------------------- #
def unprocessable(message: str, loc: tuple = ("body",)) -> RequestValidationError:
    """A 422 with FastAPI's standard body {"detail": [{"loc":..., "msg":..., "type":...}]}.
    Use as:  raise unprocessable("...")"""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": None}])


# StrictInt: rejects floats, strings and booleans. The range rejects second-looking values.
EpochMs = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def parse_date_str(value: str) -> date:
    """Strict YYYY-MM-DD that must be a real calendar date (2026-02-30 is invalid)."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("not a real calendar date")


class EmployeeCreate(BaseModel):
    """Unknown fields (created_at, _id, ...) are ignored: the server sets created_at."""
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        parse_date_str(v)
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"  # ABSENT/LEAVE are not created by punch-in


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Manual correction. Derived fields cannot be sent (they are not model fields, so they are ignored)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (database document -> API JSON). _id is never returned.
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def _ms_or_none(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_record(doc: dict) -> dict:
    """Attendance record -> API shape. Legacy documents may lack late_minutes, overtime_minutes,
    half_day or history: missing means 0 / 0 / false / []."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):  # BSON dates inside the nested object too
                changes[field] = {"from": _ms_or_none(change.get("from")), "to": _ms_or_none(change.get("to"))}
            else:
                changes[field] = {"from": change.get("from"), "to": change.get("to")}
        history.append({"at": dt_to_ms(entry["at"]), "by": entry.get("by"),
                        "reason": entry.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints: employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)  # total of the FILTERED set (R10)
    cursor = (db.employees.find(query, {"_id": 0})
              .sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size)
              .limit(page_size))
    return {"items": [serialize_employee(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance (punch-in, punch-out)
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()  # `is None`: 0 is not "missing"
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])           # R1
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, emp, date_str),                      # R2; others at defaults
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before the punch-out: works across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = rec["punch_in"]
    if ts <= punch_in_dt or ts - punch_in_dt > timedelta(hours=24):
        raise unprocessable("punched_at must be after punch_in and within 24 hours of it")
    derived = derive_fields(rec["status"], punch_in_dt, ts, emp, rec["date"])       # R3, R4, R5
    # Conditional atomic update: only succeeds if nobody punched out in the meantime.
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update({"punch_out": ts, **derived})
    return serialize_record(rec)


def require_date(value: str, name: str) -> str:
    """Validate a YYYY-MM-DD value coming from a query/path parameter -> 422 if invalid."""
    try:
        parse_date_str(value)
    except ValueError as exc:
        raise unprocessable(f"{name}: {exc}")
    return value


def build_attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                           status: Optional[str], page: int, page_size: int) -> dict:
    """The ONE place that builds the attendance listing query (the explain endpoint reuses it).
    Returns the find() specification: filter, sort, skip, limit."""
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        flt["date"] = {}
        if date_from is not None:
            flt["date"]["$gte"] = date_from
        if date_to is not None:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    # Required order: date desc, then emp_code asc. When emp_code is fixed by the filter the second key
    # is constant, so sorting by date alone gives the identical order and lets MongoDB walk the
    # (emp_code, date) index backwards with no in-memory sort.
    sort = [("date", DESCENDING)] if emp_code is not None else [("date", DESCENDING), ("emp_code", ASCENDING)]
    return {"filter": flt, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        require_date(date_from, "date_from")
    if date_to is not None:
        require_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:  # ISO strings sort like dates
        raise unprocessable("date_from must not be after date_to")
    q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    # An unfiltered count is answered from collection metadata instead of scanning 100k documents.
    total = coll.count_documents(q["filter"]) if q["filter"] else coll.estimated_document_count()
    cursor = coll.find(q["filter"]).sort(q["sort"]).skip(q["skip"]).limit(q["limit"])  # paging done by MongoDB
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: regularization (manual correction with audit trail)
# --------------------------------------------------------------------------- #
CORRECTABLE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _record_state(rec: dict) -> dict:
    """The seven correctable fields of a stored record, with defaults for missing legacy fields."""
    return {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # NOTE: the path parameter is called `date` to match the contract; it only shadows the datetime.date import here.
    require_date(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")
    old = _record_state(rec)

    # ---- build the final state of the record ----
    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("ABSENT/LEAVE clear the punch times: do not supply punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:  # R1: must stay on the record's day
            raise unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise unprocessable("punch_out must be after punch_in and within 24 hours of it")
    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(status, new_in, new_out, emp, date)}  # recompute R2-R5 from the final times

    # ---- audit entry: only fields that actually changed ----
    changes = {f: {"from": old[f], "to": new[f]} for f in CORRECTABLE_FIELDS if old[f] != new[f]}
    if not changes:
        raise unprocessable("the request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- optimistic concurrency: write only if the record is still exactly as we read it ----
    old_history = rec.get("history")
    expected = {"_id": rec["_id"], "history": old_history, "status": old["status"],
                "punch_in": old["punch_in"], "punch_out": old["punch_out"]}
    result = db.attendance_logs.update_one(expected, {"$set": new, "$push": {"history": entry}})
    if result.matched_count == 0:  # someone else changed it between our read and our write
        raise HTTPException(409, "record was modified concurrently, retry")
    rec.update(new)
    rec["history"] = (old_history or []) + [entry]
    return serialize_record(rec)


# --------------------------------------------------------------------------- #
# Analytics: shared helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]
PRESENCE_LIST = sorted(PRESENCE)
MAX_TREND_DAYS = 92


def month_bounds(month: str) -> tuple:
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Year 0000 is not a real calendar year -> 422."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise unprocessable("month: year must be at least 0001", ("query", "month"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last_day:02d}"


def count_working_days(first: str, last: str) -> int:
    """Monday-Friday days from first to last inclusive (R7: no holiday calendar)."""
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    count = 0
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# MongoDB expression fragments. They read the STORED derived values and tolerate legacy documents
# (missing half_day / late_minutes / overtime_minutes). "present2" = present days counted in HALF days
# (2 for a full day, 1 for a half day): integers keep every sum and rounding below exact.
DATE_EXPR = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": DATE_EXPR}, 5]}
IS_PRESENT = {"$in": ["$status", PRESENCE_LIST]}                        # R6
IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
HALVES = {"$cond": [IS_HALF, 1, 2]}
PRESENT2_ANY_DAY = {"$cond": [IS_PRESENT, HALVES, 0]}                   # trend counts weekends too
PRESENT2_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, HALVES, 0]}   # R7: Mon-Fri only
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
OVERTIME_MIN = {"$ifNull": ["$overtime_minutes", 0]}
IS_LATE = {"$gt": [LATE_MIN, 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_div(num, den) -> dict:
    """Exact integer half-up rounding of num/den (both non-negative integers):
    floor((2*num + den) / (2*den)). MongoDB's $round is banker's rounding, so it is not used (R8)."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, num]}, den]}, {"$multiply": [2, den]}]}}


# --------------------------------------------------------------------------- #
# Analytics: employee monthly summary
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> tuple:
    """(collection, pipeline). One $group over this employee's records of the month."""
    first, last = month_bounds(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},   # uses (emp_code, date) index
        {"$group": {
            "_id": None,
            "present2": {"$sum": PRESENT2_WEEKDAY},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": OVERTIME_MIN},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    working_days = count_working_days(max(first, emp["joined_on"]), last)               # R7
    coll, pipeline = build_employee_monthly_pipeline(emp_code, month)
    row = next(iter(db[coll].aggregate(pipeline)), None) or {}
    present2 = int(row.get("present2", 0))
    present_days = Decimal(present2) / 2
    pct = None
    if working_days > 0:
        pct = round_half_up(present_days / Decimal(working_days) * 100, 2)               # R8
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": float(present_days), "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary (cross-collection)
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(month: str, department: Optional[str]) -> tuple:
    """(collection, pipeline, hint). Starts from `employees` so people with zero logs still count (R9)."""
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}            # joined on or before the last day of the month
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    per_employee = [                                  # one small summary document per employee
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present2": {"$sum": PRESENT2_WEEKDAY},
            # work_hours in whole hundredths (exact integers) so the average is exact: R4 stores 2 decimals
            "wh_hundredths": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
            "wh_records": count_if(has_hours),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MIN},
            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        }},
    ]
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},   # keep employees with no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present2": {"$sum": {"$ifNull": ["$s.present2", 0]}},
            "wh_hundredths": {"$sum": {"$ifNull": ["$s.wh_hundredths", 0]}},
            "wh_records": {"$sum": {"$ifNull": ["$s.wh_records", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$divide": ["$present2", 2]},
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_records", 0]},
                                         {"$divide": [half_up_div("$wh_hundredths", "$wh_records"), 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
    ]
    return "employees", pipeline, ("dept_joined" if department is not None else "joined")


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    month_bounds(month)  # validates the year
    coll, pipeline, hint = build_department_summary_pipeline(month, department)
    items = []
    for row in db[coll].aggregate(pipeline, hint=hint):
        for key in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            row[key] = int(row[key])
        row["present_days"] = float(row["present_days"])
        items.append(row)
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard (ties share a rank)
# --------------------------------------------------------------------------- #
def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> tuple:
    """(collection, pipeline). Rank first, THEN apply `limit` (rank <= limit)."""
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},                      # (date, emp_code) index
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MIN}, "late_count": count_if(IS_LATE)}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},                                                        # drops logs of unknown employees
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})                # rank inside the department only
    pipeline += [
        # sortBy has ONE key (total), so equal totals get equal ranks: 1, 2, 2, 4 (standard competition ranking)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month_bounds(month)
    coll, pipeline = build_leaderboard_pipeline(month, limit, department)
    items = []
    for row in db[coll].aggregate(pipeline):
        row["rank"] = int(row["rank"])
        row["total_late_minutes"] = int(row["total_late_minutes"])
        row["late_count"] = int(row["late_count"])
        items.append(row)
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend (gap filling with $densify + 7-day moving average)
# --------------------------------------------------------------------------- #
def build_trend_pipeline(department: str, date_from: str, date_to: str) -> tuple:
    """(collection, pipeline, hint). Everything is computed inside MongoDB.
    attendance_rate and moving_avg_7d are produced as integer ten-thousandths first (exact sums, exact
    half-up rounding) and divided by 10000 at the very end."""
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
    end_exclusive = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    zero = {"present2": 0, "late_count": 0}
    pipeline = [
        # 1. this department's employees (hinted index), 2. their records in the range
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [
                         {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                         {"$project": {"_id": 0, "date": 1, "present2": PRESENT2_ANY_DAY,
                                       "late": {"$cond": [IS_LATE, 1, 0]}}},
                     ], "as": "l"}},
        {"$unwind": "$l"},
        {"$group": {"_id": "$l.date", "present2": {"$sum": "$l.present2"}, "late_count": {"$sum": "$l.late"}}},
        # 3. two seed rows guarantee the day axis exists even when the range has no records at all
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": date_from, **zero}, {"_id": date_to, **zero}]}]}},
        {"$group": {"_id": "$_id", "present2": {"$sum": "$present2"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # 4. one row per calendar day
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {"$set": {"present2": {"$ifNull": ["$present2", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
                  "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}}},
        # 5. headcount per day (R9): one uncorrelated lookup of the department's join dates, then a running filter
        {"$lookup": {"from": "employees",
                     "pipeline": [{"$match": {"department": department}},
                                  {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
                     "as": "joins"}},
        {"$set": {"headcount": {"$sum": {"$map": {
            "input": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j._id", "$date"]}}},
            "as": "j", "in": "$$j.n"}}}}},
        # 6. attendance_rate in ten-thousandths (null on weekends / empty department days)
        {"$set": {"rate_t": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_div({"$multiply": ["$present2", 10000]}, {"$multiply": [2, "$headcount"]}), None]}}},
        # 7. moving window: current row and up to 6 preceding rows of the range
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "mv_sum": {"$sum": "$rate_t", "window": {"documents": [-6, 0]}},
            "mv_n": {"$sum": {"$cond": [{"$eq": ["$rate_t", None]}, 0, 1]}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present2", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_t", None]}, None, {"$divide": ["$rate_t", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$mv_n", 0]},
                                        {"$divide": [half_up_div("$mv_sum", "$mv_n"), 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, "dept_joined"


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")):
    require_date(date_from, "from")
    require_date(date_to, "to")
    if date_to < date_from:
        raise unprocessable("to must not be before from", ("query", "to"))
    days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1   # inclusive day count
    if days > MAX_TREND_DAYS:
        raise unprocessable(f"the range may cover at most {MAX_TREND_DAYS} days", ("query", "to"))
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipeline, hint = build_trend_pipeline(department, date_from, date_to)
    items = []
    for row in db[coll].aggregate(pipeline, hint=hint):
        row["headcount"] = int(row["headcount"])
        row["late_count"] = int(row["late_count"])
        row["present_count"] = float(row["present_count"])
        items.append(row)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Still to come: /admin/explain/{endpoint}.
# --------------------------------------------------------------------------- #