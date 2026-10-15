# Starter review

| # | Where | What is wrong | How to notice | How fixed |
|---|---|---|---|---|
| 1 | `compute_work_hours` | Python `round` uses ties-to-even, but rule R4 requires decimal half-up. | A duration yielding exactly 8.125 hours rounds to 8.12 instead of 8.13. | Use `Decimal` with `ROUND_HALF_UP`. |
| 2 | `compute_overtime` | Overnight shifts put the end time on the attendance date instead of the following date. | A 22:00–06:00 record punched out at 06:40 reports no overtime or a negative difference. | Place shift end on the next calendar day for overnight schedules. |
| 3 | `compute_late_minutes` / punch-in | Late calculation derives shift start from the punch's local date, not its attendance date. | An overnight punch at 00:30 is compared with a future 22:00 start and reported as on time. | Calculate relative to the shift start on the attendance date. |
| 4 | `EmployeeIn.email` | The starter only limits length; it accepts malformed email strings. | POST an email without `@` and observe employee creation. | Validate the contract's email shape. |
| 5 | `PunchOutIn` / punch-out | The timestamp path accepts out-of-range values and does not consistently reject non-integer milliseconds or truncate to whole seconds. | Send a float, timestamp in seconds, or millisecond fraction. | Apply the shared strict epoch-millisecond converter and truncate before calculations. |
| 6 | `punch_out` lookup | It searches for a record on the punch-out calendar date, so it cannot find an overnight shift started yesterday; it also fails to select the most recent eligible punch-in. | Punch out a 22:00–06:00 shift at 06:40 next day. | Find the latest `punch_in <= punched_at` across attendance dates. |
| 7 | `punch_out` update/response | It never computes or stores `half_day`, and its response omits the full attendance record fields. | Punch out after 3.5 hours; response lacks `half_day`. | Recompute all derived values and return the record contract. |
| 8 | First `AttendancePatchIn` and PATCH route | It permits clients to set the derived `half_day`; omits required actor/reason; history entries omit `by` and `reason`. | Submit a fabricated half-day or inspect the audit history after correction. | Accept only status/times plus required audit metadata; compute derived values on the server. |
| 9 | Both PATCH implementations | There are two definitions for the same path and method, with inconsistent behavior and duplicate model names. | Inspect generated routes or send a correction with a rule edge case; the first registered handler can shadow the later one. | Keep a single route and request model. |
| 10 | First PATCH implementation | Its punch-in date check uses UTC `.date()` and is wrong for IST and overnight attendance dates. | A valid IST punch near UTC midnight is rejected or assigned incorrectly. | Convert to the contract's IST attendance date. |
| 11 | Second PATCH implementation | It uses `< 4` instead of the required rounded-hours `< 4.50` half-day threshold. | Regularize to 4.25 hours and it is incorrectly a full day. | Apply R4 rounding first, then compare against 4.50. |
| 12 | PATCH history | Both variants record the history timestamp and changes but do not include required actor and reason; one variant also treats client corrections too loosely. | Inspect a successful PATCH response's history. | Append one complete immutable audit entry and use an optimistic conditional update. |
| 13 | `create_indexes` | It omits a useful index for finding the most recent eligible punch-in and department headcount queries. | Inspect explain plans on seeded-scale lookups. | Add purpose-specific compound indexes. |

### Reviewed and kept

The unique `(emp_code, date)` attendance index is the right race-safe guarantee for punch-in. The conditional `punch_out: null` update and history snapshot condition are useful compare-and-set patterns; the implementation retains those concurrency safeguards while correcting the surrounding behavior.
