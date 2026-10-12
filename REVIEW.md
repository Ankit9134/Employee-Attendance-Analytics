# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200; never checks MongoDB | Mongo down → still 200 | Added `db.command("ping")` and raise 503 on failure |
| 2 | `compute_late_minutes` | Uses `> 10` (minutes) but R2 says strictly more than 10:00 (600 seconds); also computes minutes from grace-period end, not from shift_start | Shift 09:30, punch 09:40:01 → returns 0 instead of 10 | Rewritten: compare `diff_seconds > 600`, return `diff_seconds // 60` |
| 3 | `compute_work_hours` | Uses Python `round()` which does banker's rounding, not half-up | 3.545 h → 3.54 instead of 3.55 | Replaced with `Decimal.quantize(ROUND_HALF_UP)` |
| 4 | `compute_overtime` | Builds `end` as a naive datetime from `date_str` then compares with a UTC `punch_out`; timezone mismatch gives wrong result | Any punch-out → wrong overtime | Fixed: build `end_ist` in IST, compare IST-aware datetimes; also handle overnight shift where end is next day |
| 5 | `compute_overtime` | Does not enforce the 30-minute minimum (R3) | 29-min overtime → non-zero result | Added `return diff if diff >= 30 else 0` |
| 6 | `create_employee` | Race condition: check-then-insert allows two concurrent creates to both succeed | Two simultaneous POSTs with same `emp_code` → two 201s | Rely on unique index + catch `DuplicateKeyError` |
| 7 | `create_employee` | `created_at` stored as naive local time (`datetime.now()`) instead of UTC | Timestamps wrong in non-UTC environments | Changed to `datetime.now(tz=timezone.utc)` |
| 8 | `list_employees` | `skip = page * page_size` — off by one; page 1 skips first 20 rows | `GET /employees?page=1` returns items 21-40 | Fixed to `(page - 1) * page_size` |
| 9 | `list_employees` | `total` counts entire collection, ignores `department` filter | Filtered total is wrong | Changed to `count_documents(q)` |
| 10 | `list_employees` | No `sort` — order is undefined | Results in random order | Added `.sort("emp_code", ASCENDING)` |
| 11 | `punch_in` | `datetime.fromtimestamp(ms/1000)` — divides by 1000 but `punched_at` is already ms; also produces naive local time | Wrong timestamp stored | Replaced with `ms_to_dt()` which does `ms // 1000` and returns UTC-aware datetime |
| 12 | `punch_in` | No 404 check for unknown employee | Unknown emp_code → 500 (KeyError on `emp["shift_start"]`) | Added `if not emp: raise HTTPException(404, ...)` |
| 13 | `punch_in` | Race condition: check-then-insert for duplicate date | Two simultaneous punch-ins → two 201s | Rely on unique compound index `(emp_code, date)` + catch `DuplicateKeyError` |
| 14 | `punch_in` | `date` computed as `ts.date()` ignoring IST and overnight shift rule (R1) | Overnight shift punch-in at 01:00 IST gets wrong date | Replaced with `attendance_date_for_punch()` using IST-converted time |
| 15 | `list_attendance` | Loads entire collection into memory (`find(q)` without limit), then sorts and paginates in Python | 100k records → OOM / very slow | Replaced with server-side `.sort().skip().limit()` and `count_documents(q)` for total |
| 16 | `list_attendance` | `total = len(docs)` counts only the fetched page after Python-side pagination, not the full filtered set | `total` always equals `page_size` or less | Fixed: `total = db.attendance_logs.count_documents(q)` before pagination |

**Things examined and found fine:**
- Pydantic v2 `model_dump()` usage is correct.
- `load_dotenv()` at module level is acceptable for this single-file setup.
