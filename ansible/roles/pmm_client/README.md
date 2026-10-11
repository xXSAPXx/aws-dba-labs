# PostgreSQL lock blocking for PMM

PostgreSQL keeps no history of lock waits, and PMM shows none for it: Query Analytics has no lock time for PostgreSQL, and the standard dashboards only count locks by mode. After a pile-up you know that sessions waited, not who was in their way.

This role adds that to PMM with two files that also work outside the lab:

| File | What it is |
|---|---|
| [templates/lock-blocking.yml.j2](templates/lock-blocking.yml.j2) | Three custom queries for PMM's PostgreSQL exporter |
| [files/postgresql-lock-blocking-dashboard.json](files/postgresql-lock-blocking-dashboard.json) | The dashboard "PostgreSQL lock blocking" |

Tested on PMM 3.9.1 (server and client) with Percona Server for PostgreSQL 17.

## How it works

Every 5 seconds (PMM's high resolution) the exporter samples `pg_stat_activity` and records who is blocking whom.

- **Root blocker.** PostgreSQL reports sessions queued on the same row as blocking each other: 59 sessions waiting behind one blocker gave 975 "blocked by" pairs in a test. The queries follow each queue back to its root, the session that is in the way and is not waiting on a lock itself.
- **Persisting blockers only.** A root blocker is reported once its transaction has been open for 1 second. A healthy database has many lock waits of a few milliseconds: 20 clients updating one row at 407 transactions per second showed a "blocker" in 28 of 30 unfiltered samples, and in none with the filter.
- **The filter is on the blocker, not on the waiting sessions.** A session that gives up after its `lock_timeout` never waits long. With a 2 s timeout, counting only sessions that had waited over 1 s gave between 0 and 12 for a blocker that held 12 sessions up the whole time.
- **Once per server.** The exporter runs a custom query in every database unless it is marked `master: true`. These queries cover the whole server, so they are marked.
- **Totals come from the waiting side.** A session that waits behind several root blockers is counted under each of them: three reports holding a table that 21 sessions waited for gave 3 × 21. Per blocker that is right, but the blockers must not be added up. The waiting metrics have one entry per waiting session, so they do add up, and the dashboard takes its totals from them.

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `pg_lock_blocking_sessions` | `datname`, `blocking_user`, `blocking_application`, `blocking_state` | Sessions waiting behind these root blockers, directly or in a queue |
| `pg_lock_blocking_blockers` | same | How many sessions of that user and application are root blockers |
| `pg_lock_blocking_transaction_seconds` | same | How long the oldest of their transactions has been open |
| `pg_lock_blocking_statement_sessions` | the same, plus `blocking_query` | The same count per statement of the root blocker, for the ten that block the most |
| `pg_lock_blocking_statement_transaction_seconds` | the same, plus `blocking_query` | How long that blocker's transaction has been open |
| `pg_lock_waiting_sessions` | `datname`, `waiting_user`, `waiting_application` | Every session waiting on a lock, however briefly |
| `pg_lock_waiting_blocked_sessions` | same | Of those, the sessions behind a persisting root blocker. Each is counted once: sum this for a total |

The first three and the last two have few, stable series: use them for graphs. The statement metrics carry the statement text (first 100 characters) as a label.

## The dashboard

| Row | Shows |
|---|---|
| **Now** | Sessions waiting, sessions blocked by a persisting blocker, blocking sessions, the oldest blocking transaction, and the blocked session-seconds of the time range |
| **Who blocks** | Sessions blocked over time by blocking user and by application; blocked session-seconds by user; the age of each user's oldest blocking transaction |
| **What they run** | The blocking statements, with the most sessions each blocked, its blocked session-seconds and how long its transaction was open |
| **Who waits** | Sessions held up by persisting blockers over time, by waiting user and by application, against every session waiting |
| **PostgreSQL's own counters** | PMM's standard metrics: locks held by mode, deadlocks |

- **Blocked session-seconds** is sessions blocked × seconds: the waiting a user or statement caused. 20 sessions blocked for 3 s and 5 sessions blocked for 12 s are both 60. It ranks blockers by impact rather than by peak.
- **The per-blocker charts are not stacked,** and their lines can add up to more than the total: a session behind two blockers is in both lines. The two totals in the first row count each waiting session once, and cover all blockers whatever the **Blocking user** filter says.
- **To see one user's statements,** click the user in a "by blocking user" panel and choose **Show only this user**, or pick it in the **Blocking user** filter. Every panel, including the statements table, then shows only that user.
- **Sample interval** must equal PMM's high resolution (5 s by default). The session-seconds are the samples multiplied by it.

## Install it in another PMM

1. **The query file.** In `lock-blocking.yml.j2`, replace the three `{{ pmm_client_lock_blocking_min_seconds }}` with the threshold in seconds, for example `1`. Or copy the finished file from a lab database server: `/usr/local/percona/pmm/collectors/custom-queries/postgresql/high-resolution/lab-lock-blocking.yml`.
2. **Where it goes.** On every host where a PMM agent runs the PostgreSQL exporter, into `/usr/local/percona/pmm/collectors/custom-queries/postgresql/high-resolution/`, owner `pmm-agent`, mode `0640`. The exporter picked it up within 20 seconds, without a restart of the agent.
3. **Permissions.** PMM's monitoring user must be a member of `pg_monitor`, or it can't see other users' sessions and statements.
4. **The dashboard.** In PMM: **Dashboards → New → Import**, and upload the JSON file.
5. **Check it.** Hold a row in one session and update it from another:
   ```sql
   -- session 1
   BEGIN; UPDATE some_table SET some_column = some_column WHERE id = 1;
   -- session 2: waits. After a few seconds the dashboard shows session 1's user blocking 1 session.
   UPDATE some_table SET some_column = some_column WHERE id = 1;
   -- session 1
   ROLLBACK;
   ```

## Cost

Measured with `pg_stat_monitor` on the lab (2 vCPUs, about 100 requests per second):

| | |
|---|---|
| Runs | 12 per minute for each of the three queries, in one database |
| Time per run | 0.8 to 1.4 ms on average, 5 ms at worst during a pile-up |
| 59 sessions waiting behind one blocker | about 4 ms per run |

The cost grows with the square of a queue's length on one row, and each waiting session costs one call of `pg_blocking_pids()`, which briefly locks PostgreSQL's lock manager.

## Limits, and what is not tested yet

- **It samples.** A blocker that comes and goes between two samples is missed, and session-seconds are accurate to the sample interval.
- **The statement is the blocker's current or last one,** which is not always the one that took the lock. A batch job that locked rows with a quick `UPDATE` shows the long query it runs afterwards.
- **Statement text is a label.** Statements with literal values make a new series each time they block. The filter keeps this to real incidents, but it has not been run against a production workload.
- **Prepared transactions (two-phase commit) are not shown as blockers.** PostgreSQL reports them without a session. Their victims still appear under "Who waits".
- **Not tested:** PostgreSQL older than 17 (the queries need version 10 or newer), replicas, servers with many databases, more than 59 waiting sessions, and PMM 2.
- **The "Show only this user" link** assumes PMM serves Grafana under `/graph`.
