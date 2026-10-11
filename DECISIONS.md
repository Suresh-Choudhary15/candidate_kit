# DECISIONS.md

1. **Indexes.** Unique `uq_emp_code` and `uq_emp_date` make creation and punch-ins race-safe. `date_emp` serves listings and leaderboard scans, `status_date_emp` status filters, `emp_punchin` the latest record at or before a punch-out, and the department indexes headcount. I rejected a separate `(emp_code, date desc)` index: MongoDB reads the unique `(emp_code, date)` index backwards for free.

2. **Punch-in race.** Both requests compute the date and call `insert_one` with no prior find. The unique `(emp_code, date)` index lets exactly one insert commit (201); the other raises `DuplicateKeyError`, returned as 409.

3. **Ties.** `$rank` over total late minutes in `$setWindowFields` gives equal totals the same rank (100, 60, 60, 30 give 1, 2, 2, 4). The limit is applied afterwards as `rank <= limit`, so everyone tied at the cutoff is kept.

4. **Headcount.** The pipeline starts from `employees` with `joined_on` up to month-end, so an employee with no logs counts and a later joiner does not. It then `$lookup`s logs, `$unwind`s keeping empty arrays, and counts one per employee.

5. **One thing I would change for 100x the data.** A pre-aggregated monthly rollup collection, updated on each punch or PATCH, so analytics stop scanning raw logs. Corrections must update it too.
