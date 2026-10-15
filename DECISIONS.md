# Design decisions

I use a unique `employees.emp_code` index for business identity, a unique `(emp_code, date)` attendance index for one record per employee-day and race-safe punch-in, and `(emp_code, punch_in)` to find the latest punch eligible for punch-out. The date/employee and status/date indexes support attendance listing and common date/status filters. Department plus joined date supports headcount lookups. I did not add a standalone `work_hours` index because the API does not filter or sort by it.

Two simultaneous punch-ins can both read the employee, but only one insert can satisfy the unique employee-day index. MongoDB accepts one; PyMongo raises `DuplicateKeyError` for the other, which becomes 409. For a leaderboard tie at the limit, `$rank` gives tied employees the same rank, and filtering after ranking keeps every employee whose rank is within the limit; employee code provides stable order.

Department summaries start from employees and left-join logs. That preserves employees with no logs, while `joined_on <= month end` excludes future joiners. At 100x data, I would measure query plans and memory first, then consider pre-aggregated daily summaries and a retention/archive strategy.
