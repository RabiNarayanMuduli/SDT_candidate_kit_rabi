# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique) guarantees business-key uniqueness and fast lookups; `(department, joined_on)` speeds up department queries and headcount scans without collection scans.
   - `attendance_logs`: `(emp_code, date)` (unique) enforces the single-punch-per-day natural key and powers employee monthly aggregation; `(date, -1, emp_code, 1)` supports sorted pagination in `GET /attendance`; `(date, late_minutes)` enables index scans for the late leaderboard.
   - *Rejected*: An index on `date` alone, because compound `(date, emp_code)` covers both filtering and secondary sorting without extra write overhead.

2. **Punch-in race.**
   Both requests compute the same attendance date and attempt `insert_one()`. Because of the database-level unique compound index on `(emp_code, date)`, MongoDB atomically accepts the first write and rejects the second with `DuplicateKeyError`. Our code catches this exception and returns HTTP 409, avoiding check-then-insert race conditions.

3. **Ties.**
   Using MongoDB's `$rank` window operator, tied employees share the same rank (e.g., 1, 2, 2, 4). The `limit` filter is evaluated *after* ranking (`rank <= limit`), and rows are ordered by `total_late_minutes` descending, then `emp_code` ascending. If two employees tie at the cutoff, both are included, returning more than `limit` rows as specified.

4. **Headcount.**
   The aggregation pipeline starts from `employees` matching `joined_on <= period_end`, groups by `department` to establish accurate headcount, and then performs a `$lookup` to join `attendance_logs`. Employees with zero logs still contribute to the initial headcount.

5. **One thing you would change for 100x data.**
   Pre-aggregate attendance metrics asynchronously into daily and monthly summary collections (or shard `attendance_logs` on `emp_code`) to serve dashboards without scanning raw punch events.

