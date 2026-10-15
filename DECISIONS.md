# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, 409 on duplicates); `(department, emp_code)` for the sorted employee list; `(department, joined_on)` and `(joined_on)` for headcount and the trend. `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly summary, punch-out lookup); `(date desc, emp_code)` for list order and month-wide analytics; `(status, date desc, emp_code)` for status-filtered lists. Rejected: an index on `punch_in`. `date` rises with `punch_in`, so `(emp_code, date)` already finds the latest record.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one insert win (201); the other raises `DuplicateKeyError`, which I turn into 409. There is no find-then-insert.
3. **Ties.** `$rank` runs first, then `limit` filters `rank <= limit`. Everyone tied at the cutoff rank is returned, so the result can be longer than `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s logs. No logs just means an empty lookup, and the employee still adds 1.
5. **100x data.** Stop aggregating raw logs per request: keep a per-employee-per-month summary document, updated when records change, and read that.
