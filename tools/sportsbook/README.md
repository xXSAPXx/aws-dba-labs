# sportsbook: a sportsbook's production traffic on MySQL

`sportsbook` plays a sports betting platform against the [sportsbook database](../../schema/sportsbook/README.md) in the [mysql-single](../../labs/mysql-single/) lab: players, the odds feed of live matches, and the back office's batch jobs and reports. It runs on the PMM server, connects over the network like an application server, and reports to PMM. It is the MySQL counterpart of the shop's [labapp](../labapp/README.md).

The Ansible role [`sportsbook`](../../ansible/roles/sportsbook/) installs it and starts it as the service **`sportsbook-workload`**.

## Three kinds of traffic

| Traffic | Connection | What it does to the database |
|---|---|---|
| **Players** | 32 workers as `sb_app` | Many short transactions, open loop: they arrive at the target rate whether or not the database keeps up |
| **Odds feed** | 1 connection as `sb_feed` | A stream of small writes: odds moves on hot rows, goals that suspend markets, kick-offs and full time |
| **Batch jobs and reports** | Their own connections as `sb_batch` and `sb_reporting` | Big transactions on schedules: settlement, bonuses, the leaderboard; a long report in one snapshot |

**Target rate** = base rate (80 requests per second) × an hourly wave (±40%) × a **kick-off surge**: when matches kick off, three times as many players for 2 minutes, mostly on the live pages and betting. Matches kick off every 10 minutes, six at a time, and last 15 minutes (lab time). Over an hour that is about 50 to 110 requests per second, and up to about 330 in a surge.

When the database slows down, requests queue for a free worker and the response time includes the wait. A request that waited 30 s counts as `expired` (the player gave up); with 5,000 waiting, new ones are `dropped`.

## The players

| Request | Share | Share in a surge | What it does |
|---|---|---|---|
| `home` | 20% | 10% | A competition's next 20 matches with the match-result odds (top leagues get more traffic) |
| `event_page` | 18% | 20% | One match with all its markets and odds |
| `live` | 10% | 20% | Every match in play with score and odds |
| `place_bet` | 16% | 26% | A single (70%) or an accumulator of 2-4 legs. **Locks the player's wallet row first**, checks status, maximum stake, daily loss limit, balance, and that every leg is still open at the odds the player saw (within 2%). Then bet, legs, wallet and ledger in one transaction |
| `login` | 8% | 10% | Finds the player, inserts a session, updates `last_login_at`, writes the audit log |
| `bet_history` | 7% | 3% | The player's last 20 bets with their legs |
| `wallet` | 6% | 3% | Balances and the last 20 ledger rows |
| `deposit` | 5% | 5% | Two transactions, like a real payment: the request (a pending payment, daily deposit limit checked), then the provider's callback: approved (wallet and ledger) or 5% declined |
| `cashout` | 3% | 1% | Takes an early payout on an open single of a live match. Locks the wallet, then the bet |
| `leaderboard` | 3% | 1% | This week's top 100 |
| `withdrawal` | 2% | 1% | Locks the wallet, takes the money out at once (verified players only) and leaves a pending withdrawal for the batch job |
| `signup` | 2% | 0 | A new player with limits and an empty wallet |

Players are skewed like the generated data: a few heavy bettors place most bets, so their wallet rows are hot. The odds a player "sees" come from the application's cache (refreshed every 3 seconds), so they can be stale: then the bet fails with `odds_changed`, as on a real site. Deadlocks are retried once.

Each request ends with one result:

| Result | Meaning |
|---|---|
| `ok` | Done |
| `insufficient_funds`, `odds_changed`, `market_suspended`, `limit_reached`, `blocked`, `kyc_required`, `declined`, `nothing_to_do` | **Business outcomes**, not errors: the request was refused or found nothing to do, and rolled back |
| `lock_timeout` | Waited longer than `innodb_lock_wait_timeout` for a row lock (3 s for the players) |
| `query_timeout` | A page's `SELECT` ran longer than `max_execution_time` (5 s for the players) |
| `deadlock` | Failed again after the one retry |
| `killed` | `KILL QUERY` |
| `too_many_connections` | `max_connections` or the user's `MAX_USER_CONNECTIONS` |
| `denied`, `missing_table`, `check_violation` | A privilege missing, a table gone, a `CHECK` that refused a value |
| `connection` | Server down or restarting, or the session was killed |
| `other` | Any other error |
| `expired`, `dropped` | Waited 30 s for a free worker / the queue was full |

## The odds feed

Once a second, on its own connection (`sb_feed`):

- **Kick-off:** every 10 minutes the next 6 scheduled matches go live, and the players surge.
- **Odds moves:** about 15 a second, mostly on live matches: `UPDATE selections` plus a row in `odds_history`.
- **Goals:** about 2.6 per match. The score changes and the match's markets are **suspended for 15 seconds**; bets on them fail with `market_suspended`. Then new odds, and the markets open again.
- **Full time:** after 15 minutes the match is finished, its markets closed, and every selection won or lost. Settlement pays out next.
- **New fixtures** at the end of the schedule when fewer than 300 are left.

## Batch jobs and reports

A job never runs twice at once; if the previous run is still going, the next one is skipped.

| Job | Role | Default | What it does | What it does to the database |
|---|---|---|---|---|
| `settlement` | `sb_batch` | every 30 s | Pays out the matches at full time: every bet on them won or lost, winners' wallets credited, ledger rows | **One transaction per match** by default (`mode: single_transaction`): every winner's wallet row stays locked to the end, so their new bets wait and fail with `lock_timeout`; one big binlog event. `mode: batched` commits every 500 bets |
| `withdrawals` | `sb_batch` | every 2 min | Pays out pending withdrawals older than a minute (`FOR UPDATE SKIP LOCKED`); 3% declined, money back | Short |
| `bonus_awards` | `sb_batch` | every 10 min | The hourly mission: whoever staked 50 this hour gets 5 on the bonus balance, once per hour | One transaction for every winner, wallets locked in player order: it can deadlock with settlement, which locks them in bet order |
| `leaderboard` | `sb_batch` | every 5 min | This week's top 1,000 by points | A scan of the week's bets; `REPLACE INTO` |
| `session_cleanup` | `sb_batch` | every 10 min | Deletes expired sessions | `DELETE ... LIMIT 5000` in a loop |
| `regulatory_report` | `sb_reporting` | every 15 min, ~5 min | Gross gaming revenue per day and country, turnover per competition, bets of self-excluded players, deposits above 2,000 in 24 hours, open liability per match, and a wallet-ledger reconciliation | **One `REPEATABLE READ` snapshot for 5 minutes**: InnoDB can't purge the row versions it might still read, so the history list length grows |

The reconciliation checks that the wallets hold exactly what the ledger adds up to, within one snapshot. Its result is in the job's log line and annotation: `wallets and ledger agree` or `MISMATCH`.

Settlement, bonus awards and the report are marked on the PMM dashboards when they do something; kick-offs too.

## Commands

On the PMM server:

```bash
sportsbook load status                              # rates, live matches, queued requests, running jobs
sportsbook load rate 40                             # base rate: 40 requests per second
sportsbook load pause                               # stop the players (the feed and the jobs go on)
sportsbook load resume
sportsbook load kickoff 10                          # kick off 10 matches now
sportsbook load settlement-mode batched             # settle in batches of 500 (single_transaction: one per match)
sportsbook load run regulatory_report               # run a job or report now
journalctl -u sportsbook-workload -f
```

`sportsbook load` changes last until the service restarts.

## Configuration

The role's defaults (`ansible/roles/sportsbook/defaults/main.yml`), overridable in the lab's `group_vars`:

| Variable | Default | |
|---|---|---|
| `sportsbook_rate` | `80` | Base requests per second |
| `sportsbook_workers` | `32` | Connections as `sb_app` |
| `sportsbook_session` | see the file | `SET SESSION` per part of the application: `innodb_lock_wait_timeout`, `max_execution_time` |
| `sportsbook_mix`, `sportsbook_surge_mix` | the shares above | Request weights, normal and in a surge |
| `sportsbook_wave` | `{period: 3600, amplitude: 0.4}` | The traffic cycle |
| `sportsbook_surge` | `{multiplier: 3, duration: 120}` | After a kick-off |
| `sportsbook_feed` | see the file | Kick-off interval and size, match length, goals, suspension, odds moves |
| `sportsbook_jobs` | the schedule above | `every`, `first`; `settlement` also takes `mode` and `batch_size`, `regulatory_report` `target` |

## Metrics

Served on port 9312 (`/metrics`), collected by PMM as the external service `sportsbook-workload`.

| Metric | Meaning |
|---|---|
| `sportsbook_tx_total{tx, result}` | Player requests by type and result |
| `sportsbook_tx_latency_seconds{tx}` | Histogram of the response time: queue wait + transaction |
| `sportsbook_tx_queue_depth` | Requests waiting for a free worker |
| `sportsbook_load_base_rate`, `sportsbook_load_target_rate` | Base and current target rate |
| `sportsbook_load_surge`, `sportsbook_load_paused` | 1 during a kick-off surge, 1 while paused |
| `sportsbook_events{status}` | Matches scheduled, live, and finished but not settled yet |
| `sportsbook_feed_actions_total{action}` | Kick-offs, odds moves, goals, reopened markets, full times, new fixtures |
| `sportsbook_feed_errors_total{kind}` | Odds feed failures |
| `sportsbook_money_total{flow}` | EUR moved: stake, win, cashout, deposit, withdrawal, bonus. Stakes minus wins and cash-outs is the gross gaming revenue |
| `sportsbook_settled_bets_total{result}` | Bets settled won / lost |
| `sportsbook_job_active{job_name}`, `sportsbook_job_runs_total{job_name, result}`, `sportsbook_job_last_duration_seconds{job_name}` | Batch jobs and reports |

## Not built yet

- **Incident scenarios on demand**, like labapp's: a wrong settlement `UPDATE` to recover with point-in-time recovery, an `ALTER TABLE bets` during a peak, a forgotten transaction, a connection storm.
- **A PMM dashboard** for these metrics.
- **Redis or Valkey** for the odds cache, sessions, rate limits and the leaderboard.
