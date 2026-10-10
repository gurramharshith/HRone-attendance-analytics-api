# Starter review

| # | Where | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | MongoClient configuration | The URI and database were initialized at import time with defaults and no explicit UTC behavior. | Set environment variables before starting; seeded BSON dates may be compared as naive/aware datetimes. | Environment variables (with `.env` support) configure a UTC-aware client, and persisted instants stay UTC. |
| 2 | `health` | It always returns 200 without checking MongoDB. | Stop MongoDB and call `GET /health`. | It pings MongoDB and returns 503 on connection failure. |
| 3 | `EmployeeIn` | It accepts invalid employee code, email, dates, shift times, and equal shift boundaries. | Submit malformed `EMP` code, email, `2026-02-30`, `25:00`, or equal shift times. | Added the OpenAPI format/length/date checks and different shift validation. |
| 4 | `create_employee` | Duplicate detection is check-then-insert, so parallel requests can both pass; `created_at` is naive. | Send parallel creates with one `emp_code`. | A unique index enforces uniqueness; duplicate key becomes 409. Creation time is UTC. |
| 5 | `list_employees` | `skip=page * page_size` skips page one, and `total` ignores the department filter. | Request page 1 and compare `total` between department filters. | Uses `(page - 1) * page_size`, filtered count, sorted results, and max page size 100. |
| 6 | `compute_late_minutes` | It uses the punch-in calendar date as shift date, ignoring overnight shifts; naive/aware values can fail. | Punch in at 02:00 for a 22:00–06:00 shift. | Shift anchor is derived in IST and late minutes are floored after the strict 10-minute grace. |
| 7 | `compute_work_hours` | Python `round` is ties-to-even, not required half-up. | Use a duration that rounds exactly at a half centesimal boundary. | Uses decimal half-up rounding on whole-second duration. |
| 8 | `compute_overtime` | It never advances the end date for overnight shifts and has no 30-minute minimum. | Punch out 06:40 for a 22:00–06:00 shift, or only 20 minutes late. | Overnight end is on the following date; overtime is floored and reported only from 30 minutes. |
| 9 | `punch_in` | Unknown employee causes a 500; falsey timestamp `0` silently becomes now; timestamp conversion uses host local time; no validation or second truncation. | Unknown `emp_code`, invalid timestamp, or fractional-second timestamp. | Explicit employee lookup/404, strict epoch-millisecond validation, UTC conversion, truncation, and IST date assignment. |
| 10 | `punch_in` | Check-then-insert permits duplicate attendance under concurrent requests; response leaks an `id` not in contract. | Send simultaneous punches for one employee/date. | Unique `(emp_code,date)` index makes insertion atomic; duplicate key is 409; `_id` is omitted. |
| 11 | `list_attendance` | It loads and sorts the full matching collection in Python, then slices; it leaks Mongo `_id`; omitted fields are not defaulted. | Run against 100k records and inspect the response. | Database-side indexed sorting/pagination, filtered count, and API serialization/defaults. |
| 12 | `list_attendance` | It does not validate date formats/order or status, and the original sort lacks the required employee tie-break. | Use reversed dates, malformed date, invalid status, or same-date records. | Inclusive validated date filters and exact contract sort (`date` descending, `emp_code` ascending). |
| 13 | Helpers / response boundary | Datetimes were returned as BSON/Python values rather than epoch milliseconds, and `_id` was treated as an API id. | Inspect create, punch, and list responses. | A shared serializer converts every nested instant (including history) to epoch ms and omits `_id`. |
| 14 | Startup | No required indexes existed, so natural-key concurrency and large list queries were unsupported. | Inspect indexes or run explain on seeded-scale data. | Idempotent unique and query-supporting indexes are created at startup. |

### Checked and kept

- The starter's employee code as a client-supplied business identifier matches the contract; MongoDB `_id` is never used as that identifier.
- The attendance record natural key is `(emp_code, date)`, so a unique compound index is the appropriate duplicate guard.
- The original `load_dotenv()` behavior does not override already-set environment variables, which preserves injected deployment configuration.

### Final contract re-audit

- Rechecked routes, request/response schemas, R1-R10, and stored-document defaults against the restored `openapi.yaml` and `DATA_MODEL.md`; verified the supplied sample documents and seed script against the implementation.
- Fixed a completed-implementation edge case in the employee monthly working-day aggregation: a month entirely before `joined_on` now clamps to zero working days instead of constructing a negative date range. Added a regression test that expects `working_days: 0` and `attendance_pct: null`.
- Removed the seed script's implicit localhost/database fallback so it also requires the configured `MONGO_URI` and `MONGO_DB`; this avoids writing sample documents to an unintended default database.
- Integration coverage verifies all five explain targets report indexed plans without collection scans on the generated 100,000-record fixture. This is local MongoDB 8.0.12 evidence, not a guarantee for every deployment or hidden dataset.
