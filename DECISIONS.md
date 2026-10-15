# DECISIONS.md

1. **Indexes.** employees: unique `emp_code` (no duplicate codes), `(department, emp_code)` for the sorted list, `(department, joined_on)` and `(joined_on)` for headcount. attendance_logs: unique `(emp_code, date)` for one record per day and per-employee lookups, `(date desc, emp_code)` for list order and month reports, `(status, date desc, emp_code)` for the status filter. I skipped an index on `punch_in` because the date moves with it, so `(emp_code, date)` already finds the latest record.
2. **Punch-in race.** Both requests work out the same date and call `insert_one`. The unique index lets only one through (201); the other gets a duplicate-key error and I return 409. I never check first and then insert.
3. **Ties.** I rank everyone first with `$rank`, then keep rows where rank is at most `limit`. Everyone tied at the cutoff stays in, so the list can be longer than `limit`.
4. **Headcount.** The summary starts from the employees collection (joined on or before month end) and looks up their logs. Someone with no logs gets an empty result but still counts as one.
5. **100x data.** I'd stop crunching raw logs on every request and keep a monthly summary per employee, updated whenever a record changes.
