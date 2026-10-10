"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

All application code lives in this single file, organised in sections:
Config -> Time helpers -> Rules (R1-R5) -> Indexes -> App & health -> (endpoints are added below).
"""
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.errors import PyMongoError

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


def ensure_indexes() -> None:
    db.employees.create_indexes(EMPLOYEE_INDEXES)
    db.attendance_logs.create_indexes(ATTENDANCE_INDEXES)


def ensure_indexes_with_retry(seconds: int = 15) -> None:
    """MongoDB may still be starting: retry every second, then log and carry on (never crash)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            ensure_indexes()
            log.info("indexes ready")
            return
        except PyMongoError as exc:
            if time.monotonic() >= deadline:
                log.error("could not create indexes: %s", exc)
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


@app.get("/health")
def health():
    """200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, f"database unavailable: {exc.__class__.__name__}")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Endpoints: added step by step (employees, punch-in, punch-out, list, PATCH,
# analytics, explain). The starter's endpoints are intentionally removed: they used
# the buggy helpers and are rewritten from scratch in the next steps.
# --------------------------------------------------------------------------- #
