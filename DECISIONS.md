# DECISIONS.md

1. **Indexes.**
   - `employees.emp_code` unique — enforces uniqueness and serves every employee lookup by code.
   - `employees.department` — serves the department filter in list and analytics.
   - `attendance_logs (emp_code, date)` unique — enforces one record per employee per day and serves per-employee date-range queries.
   - `attendance_logs (date DESC, emp_code ASC)` — serves the list endpoint's sort without a collection scan.
   - `attendance_logs (emp_code, date, status)` — covers the monthly analytics match + group.
   Considered but rejected: a separate index on `late_minutes` alone — the compound index above already covers the leaderboard match on `date` range.

2. **Punch-in race.**
   Two identical requests arrive simultaneously. Both skip the find-then-insert check (there is none). Both call `insert_one` with the same `(emp_code, date)`. The unique compound index on `(emp_code, date)` means MongoDB serialises the two writes; exactly one succeeds and the other raises `DuplicateKeyError`, which the handler converts to 409. No application-level lock is needed.

3. **Ties.**
   `$rank` (standard competition ranking) assigns the same rank to tied employees and skips the next rank. The filter `rank <= limit` is applied after ranking, so if two employees tie at rank 10 and `limit=10`, both are returned. The response can therefore contain more rows than `limit`.

4. **Headcount.**
   The department summary pipeline starts from the `employees` collection (`$match joined_on <= last_day`), groups by department to get headcount, then `$lookup` attendance logs. Employees with zero logs still appear in the group stage with empty log arrays, so their headcount contribution is never lost.

5. **100x the data.**
   I would add a partial index on `attendance_logs` covering only records with `late_minutes > 0` for the leaderboard, and shard `attendance_logs` on `emp_code` (or a hashed key) to distribute write load. The department trend `$densify` + per-day headcount `$lookup` would be replaced with a pre-aggregated daily snapshot collection updated by a change stream, avoiding the expensive per-row lookup at query time.
