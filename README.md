# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

Python 3.11+ and MongoDB 6.0+ (developed and tested on Python 3.12 with MongoDB 7.0).

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # real environment variables win over a local .env
uvicorn app.main:app --port 8000
```

`.env.example` shows the two variables. Indexes are created at startup and are idempotent; if MongoDB was not reachable
yet, `GET /health` creates them as soon as it answers. Interactive docs: `http://localhost:8000/docs`.

## Tests and checks

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # rule helpers + API tests on an in-memory fake MongoDB (no server needed)
```

The scripts in `scripts/` need the API running against a **scratch** database (its name must contain `test` or `scratch`):
`seed_test_data.py --small | --bulk 100000` (awkward-case data / 100k records), `compare.py` (API vs. a slow loop-based
reference implementation in `reference.py`), `race_test.py` (parallel requests), `check_explain.py` and
`check_attendance_plans.py` (index usage, no COLLSCAN), `perf.py` (timings). The fake database cannot run the
aggregation pipelines, so those are verified with `compare.py` on a real MongoDB.

## Assumptions (where the contract leaves room)

- Trend range limit: "more than 92 days" is counted inclusively, so 92 rows are allowed and 93 give `422`.
- `moving_avg_7d` averages the reported (already rounded) `attendance_rate` values; all pipeline rounding is exact
  half-up integer arithmetic (MongoDB's `$round` is banker's rounding).
- Punch-out earlier than the employee's only punch-in finds no record at or before that time: `404`; equal to the
  punch-in: `422`; exactly 24 h after it is still allowed.
- Attendance records dated before an employee's `joined_on` are counted as stored; only `working_days` starts at `joined_on`.
- Leaderboard `limit` is applied after ranking, so a tie at the cutoff returns more than `limit` rows.
- The unfiltered `total` of `GET /attendance` comes from collection metadata (`estimated_document_count`) so it stays fast
  at 100k+ records; filtered totals use `count_documents`.
- `PATCH` uses optimistic concurrency: if the record changed between read and write the answer is `409` (retry).
- An explicit `null` for `punch_in` / `punch_out` in `PATCH` is treated as "not supplied".

## Not done / known gaps

Nothing known to be missing from the contract. Verified on MongoDB 7.0 only (the pipelines use `$setWindowFields`,
`$densify`, `$documents`, which exist from 5.1 / 6.0 on).
