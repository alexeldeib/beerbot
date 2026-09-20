# Idle database compute and reliable wakeups

## Why Neon stayed awake

The original durable workers queried both queues every second, maintenance ran
about once a minute, and Fly's 30-second `/ready` probe executed `SELECT 1`.
These independent sources prevented the database's five-minute idle suspension.
CU-hours meter allocated compute multiplied by running time, not CPU utilization.

## Current behavior

- Incoming GroupMe callbacks still acknowledge only after durable insertion.
  After that commit, they wake both local workers, including duplicate receipts.
- The execution worker drains eligible inbox messages and wakes delivery after
  committed work. The sender drains eligible replies. Claiming, transactions,
  group ordering, retry limits, and uncertain-delivery handling are unchanged.
- With pending work, each worker schedules the earliest durable retry deadline.
  A locked head stays eligible for a short recheck. A `sending` head schedules
  crash recovery after its uncertainty deadline; later blocked replies do not
  force one-second polling. Uncertain outcomes are never automatically resent.
- Empty workers wait on an in-memory event until the next shared hourly boundary.
  `QUEUE_RECOVERY_SECONDS` defaults to 3600 (minimum configurable value 600).
  Recovery catches missed wake hints and work left by another process. Both
  workers align their sparse scans to one boundary to share a Neon wake window.
- Maintenance runs at startup, worker wake/retry, or the hourly sweep—not on a
  separate minute timer. Content older than three days is removed in bounded
  batches; full batches request a prompt continuation. During inactivity,
  expiration can be followed by up to one recovery interval of cleanup delay.
- Empty database connections are released after 60 seconds; the pool has no
  permanently preallocated minimum. Real traffic reconnects normally.
- The recap scheduler's five-minute **in-memory** time check is unchanged. It
  does no SQL off-window, and caches a completed week's check instead of repeatedly
  querying until Sunday midnight. Failures still retry, and registering/deleting
  a GroupMe group invalidates that completion cache.
- Anonymous `/app/api/me` calls reject a missing session cookie before opening
  the database. Merely displaying the sign-in page no longer wakes Postgres.

## Health and deployment

`/ready` checks model configuration, live worker tasks, and known worker/probe
failures without database I/O. `/health` remains basic process liveness. A sleeping
database is not an unhealthy database. Database initialization still must succeed
before application startup finishes.

`/ready/db` is an explicit database-connectivity probe and worker wake. Never use
it in a periodic uptime check. After **every** blue-green deploy, first verify the
new `/version`, then call `/ready/db` once the old fleet has stopped. CI does this
automatically. This closes the race where an old instance commits a final receipt
after the new instance's initial queue scan. Manual deploys must use the same step.

Admin bearer-protected controls:

- `GET /admin/workers/status` reports in-memory scan counts, last/next scan times,
  and failures. It does not query or wake Postgres.
- `POST /admin/messages/wake` wakes both workers for operator recovery.
- The existing outbox retry endpoint now signals delivery after durable requeue.
- `GET /admin/messages/status` still intentionally queries queue state; do not
  poll it continuously when measuring idle compute.

Local events are hints, not a distributed broker. Normal receipt processing is
immediate on the receiving process; startup, explicit deploy wake, and sparse
recovery preserve the durable safety net. In an unusual missed-hint case with no
further traffic or restart, recovery can take up to the configured interval.

## Fly and cron

Keep the existing one-running-Machine floor for this rollout. An idle Python
process can keep its retry/recap timers without keeping Neon compute awake.
No external cron service is needed for the database savings, and no billing plan
or database sizing change is required.

Fly already autostarts on incoming traffic. If the running-Machine floor is later
reduced to zero, an external scheduler must wake the app for recaps and recovery,
because in-process timers do not run on a stopped Machine. Do not make that change
without testing scheduled delivery and downtime/retry semantics separately.

With a five-minute suspension tail and 0.25 CU, one hourly recovery wake has a
rough no-traffic baseline of 15 CU-hours per 30-day month, rather than 180 for an
always-awake compute. Actual traffic, pending retries, other clients/branches and
autoscaling add usage. This is an estimate, not a billing guarantee.

## Validation

Use disposable PostgreSQL tests to check idle scan counts, enqueue wakeups,
scan/wait races, restart deadlines, blocked sending heads, cleanup batches and
non-querying health checks. On production, compare `/admin/workers/status` across
an idle interval longer than five minutes without opening SQL consoles or making
DB probes. Then explicitly probe cold connectivity and check recovery. Do not
force-suspend/restart Neon to test this on production.
