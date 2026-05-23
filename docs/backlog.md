# Backlog

Known issues and follow-ups that aren't yet scheduled.

## Web service: torn-read race in `_job_to_dict`

**Found:** 2026-05-23, final review of the web-service feature (`841abdd`).
**Where:** `server.py` — `_job_to_dict` (~lines 128–146) and any route handler that calls it (`GET /jobs/{id}`, `GET /jobs`).

`JobStore.update(...)` sets `status` / `result` / `error` / `finished_at` sequentially inside one `_lock` hold. Route handlers read those same fields via `_job_to_dict` **without** acquiring the lock, so a poll can land in the microsecond window between two `setattr` calls and observe inconsistent state — e.g. `status="succeeded"` with `result: null`, or `status="failed"` with `error: null`. CPython's GIL makes individual field reads atomic, but the multi-field tuple has no consistent-snapshot guarantee.

**Worst-case symptom:** one confusing poll response per ~very-many polls. No data loss, no crash. Not exploitable.

**Fix:** add `JobStore.snapshot(job_id) -> Job | None` that returns a shallow `Job` copy under `_lock`; have `_job_to_dict` consume snapshots instead of live references. ~15 lines. Same change also closes the minor `job.log[-1]` outside-the-lock read in `_run_job`'s `None`-result branch.
