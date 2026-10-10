# Decisions

1. **Indexes.** Unique `emp_code` and `(emp_code, date)` indexes enforce natural keys. Employee `(department, joined_on)` and `joined_on` indexes support department/headcount queries. Attendance indexes cover date/employee sorting, employee-month reads, status filters, and monthly late scans. I rejected one index per metric: ranking still aggregates by month, while more indexes slow writes and consume space.
2. **Punch-in race.** Both requests may read the employee and calculate the same IST attendance date. They then attempt the insert. MongoDB's unique `(emp_code, date)` index accepts one; the other gets a duplicate-key error, which the API returns as 409. There is no check-then-insert claim on correctness.
3. **Ties.** Ranking uses `$rank` on total late minutes. If rank `limit` is tied, all rows with that rank are returned, ordered by employee code within the tie.
4. **Headcount.** Department summary starts with employees whose `joined_on` is on or before month end, then looks up each employee's month logs. This preserves people with no logs and excludes future joiners.
5. **100x data.** I would benchmark the real workload, then move recurring summaries to incrementally maintained daily/monthly rollups so analytics do not repeatedly scan raw attendance history.
