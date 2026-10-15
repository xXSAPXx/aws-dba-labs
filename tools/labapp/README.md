# labapp: the lab's simulated application

`labapp` plays the application in every lab. It runs on the PMM server, connects to the databases over the network like an application server, and measures what the application experiences while you do DBA work: restarts, failovers, migrations, maintenance, broken configurations.

It is installed and started by the Ansible role [`labapp`](../../ansible/roles/labapp/), as two services:

- **`labapp-probe`** checks every database continuously and measures downtime and latency.
- **`labapp-workload`** is the shop's traffic: users browsing and buying, long-running reports and batch jobs, against the [shop database](../../schema/shop/README.md).

Both report to one PMM dashboard, **Lab → Lab: Application**.

## The probe

The `labapp-probe` service checks every database target continuously:

| Check | How | Default |
|---|---|---|
| **write** | Writes a heartbeat row (`INSERT ... ON CONFLICT`) on a persistent connection | every 0.25 s |
| **connect** | Opens a new connection and runs `SELECT 1`, like a reconnecting application | every 1 s |

A failed **write** check is downtime, also when the server still accepts reads (for example a primary demoted to replica). An outage starts at the first failed write and ends at the next successful one, so durations are accurate to about 0.25 s. Every connection attempt and statement gives up after 2 s, so a hung or unreachable server counts as down instead of stalling the probe.

For each outage, the probe:

- writes a line to `/var/log/labapp/outages.jsonl`;
- logs its start and end to the journal (`journalctl -u labapp-probe`);
- adds an annotation (a red time range) in PMM, on the **Lab: Application** dashboard and on PMM's own dashboards of that service.

The probe's own table, `labapp_probe` in the `labapp` database, holds one heartbeat row per probe and target.

## The workload

The `labapp-workload` service plays the shop's users, reporting tools and batch jobs. It is **open loop**, like real users: requests arrive at the target rate whether or not the database keeps up. 24 workers, one connection each as `shop_app`, run them as business transactions. When the database slows down, requests queue for a free worker, and the response time includes that wait. A request that waited 30 s counts as `expired` (the user gave up); once 5,000 requests are waiting, new ones are `dropped`.

**Target rate** = base rate (100 requests/s) × an hourly wave (±40%, a "day" every hour) × a flash sale (3× for 3 minutes, every 30 minutes). Over an hour that is 60 to 140 requests per second, and up to about 400 in a flash sale.

### Transactions

| Transaction | Share | What it does |
|---|---|---|
| `product_page` | 33% | A product with its category and brand, attributes, last 5 reviews and stock |
| `cart` | 15% | Adds a product to the customer's open cart, or to a new one (20% are guests) |
| `category` | 14% | A page of a category's products, sorted by price |
| `login` | 10% | Finds the customer by email, updates `last_seen_at`, writes `audit_log` |
| `checkout` | 8% | Takes 1–3 products off the stock, then inserts the order, its lines, stock movements and payment, closes the cart and writes `audit_log`, in one transaction of about ten statements. No stock left: rolled back as `out_of_stock` (a business outcome, not an error) |
| `checkout_db` | half of the checkouts | The same checkout in one call: `SELECT shop.place_order(...)`, and the database does the rest. No explicit transaction: in autocommit the call is a transaction of its own. Out of stock: error code `SH001`, counted as `out_of_stock` |
| `payment` | 8% | Takes the oldest pending payment (`FOR UPDATE SKIP LOCKED`): captured, so the order is paid and invoiced, or 3% declined, so the order is cancelled |
| `order_history` | 6% | The customer's last 10 orders |
| `shipping` | 5% | Ships the oldest paid order, or delivers the oldest shipment in transit |
| `signup` | 1% | A new customer with an address |

Traffic is skewed like the generated data: a few products and customers get most of it. During a flash sale, 70% of the products picked are the 20 hot ones, so their `inventory` rows become hot rows.

`checkout` locks the stock rows in the order the customer added the products, not sorted, like many real applications: two checkouts of the same products can deadlock. Deadlocks and serialization failures are retried once.

**The checkout in the application or in the database.** `labapp_workload_checkout_in_database` (default 50) is the percent of checkout requests done by the database function `shop.place_order()` ([database code](../../schema/shop/README.md#database-code-triggers-a-function-a-procedure)). They are counted as `checkout_db`, so the dashboard shows both side by side: response time, results, lock timeouts. Both take the stock rows in the same order. The function holds them for one round trip instead of about ten. Change the share live with `labapp load checkout-in-db 100` (or `0`).

The status history and `updated_at` are written by the database's triggers, whichever way the checkout runs, and also for payment and shipping.

Each finished request is counted with one result:

| Result | Meaning |
|---|---|
| `ok` | Done |
| `out_of_stock` | Checkout rolled back for lack of stock (not an error) |
| `lock_timeout` | Waited longer than `lock_timeout` for a lock (2 s for `shop_app`) |
| `canceled` | `statement_timeout` (5 s for `shop_app`), or `pg_cancel_backend()` |
| `deadlock`, `serialization` | Failed again after the one retry |
| `idle_in_tx_timeout` | The session was idle in a transaction for too long |
| `connection` | Server down or restarting, or the session was killed (`pg_terminate_backend()`) |
| `other` | Any other database error |
| `expired` | Waited 30 s for a free worker |
| `dropped` | The queue was full |

### Long-running reports and batch jobs

Each job has its own connection and role, and runs on its own schedule. A job never runs twice at once: if the previous run is still going, the next one is skipped.

| Job | Role | Default | What it does | What it does to the database |
|---|---|---|---|---|
| `sales_dashboard` | `shop_reporting` | every 2 min, ~30 s | Revenue per day and category over 90 days, ranked | One long statement |
| `lifetime_value_export` | `shop_analytics` | every 5 min, ~1 min | Every customer's lifetime value, read through a cursor in batches of 5,000 with pauses in between (a slow client) | A transaction open for the whole export |
| `bi_extract` | `shop_analytics` | every 15 min, ~5 min | Five reports in one `REPEATABLE READ` transaction | One snapshot held for 5 minutes, which holds back vacuum on every table |
| `cart_cleanup` | `shop_batch` | every 10 min | Marks carts idle for 30 minutes as abandoned and deletes old ones: `CALL shop.cleanup_carts(5000, 0, 0)`, a procedure that commits after each batch of 5,000 | Dead rows, bloat; locks held for one batch at a time |
| `restock` | `shop_batch` | every 15 min | Adds 500 to every stock row below 50, with ledger rows | A bulk `UPDATE` of `inventory` |
| `stock_recount` | `shop_batch` | every 20 min, ~90 s | Locks every stock row and recounts it against the ledger, in one transaction | All of `inventory` locked for 90 s: checkouts fail with `lock_timeout` |

When the data is too small for a report to take its target time, the query is padded with `pg_sleep` inside the same statement or transaction, so the snapshot and the locks are held for the whole time, as with a real slow query. The padding adapts to the measured query time.

`stock_recount` runs the bad way by default (`mode: single_transaction`). `mode: batched` recounts in small transactions of 1,000 products, holding each lock only briefly.

### Schedule

The cycles fit a 1–2 hour lab. Minutes after the service starts (it starts at the end of the playbook; a restart starts the schedule again):

| | First run | Then every |
|---|---|---|
| `sales_dashboard` | 1 | 2 min |
| `lifetime_value_export` | 2.5 | 5 min |
| `cart_cleanup` | 5 | 10 min |
| `bi_extract` | 7 | 15 min |
| `restock` | 7.5 | 15 min |
| `stock_recount` | 10 | 20 min |
| Flash sale (3 min) | 20 | 30 min |

The flash sales, `bi_extract`, `cart_cleanup` and `stock_recount` are marked in purple on the dashboard.

### What the default load does

Measured on the lab (t3.small database server) at the default 100 requests per second:

| When | What the application sees |
|---|---|
| Normal traffic | No errors, checkout p95 about 25 ms, database CPU about 25% |
| Flash sale alone | 200 to 400 requests per second, nothing queued, database CPU 40 to 70% |
| Stock recount alone | No checkout succeeds for 90 s; about 8 per second fail with `lock_timeout`. Everything else keeps running, nothing queues |
| Minute 50 of every hour: a flash sale and a stock recount start together | The pile-up. Checkouts waiting for the recount's locks tie up almost every worker, the queue fills to its limit of 5,000 within 75 s, requests are dropped, and the p95 of every transaction reaches about 26 s |

The pile-up is the incident to study: the database is under 30% CPU while it happens, so more CPU fixes nothing. Under a flash sale the recount can also lose a deadlock against the checkouts and fail after a few seconds; then there is no pile-up that time.

At `labapp load rate 30` none of this queues: too few checkouts arrive to tie up the workers.

## Scenarios: incidents on demand

A scenario plays a person or a tool doing something that goes wrong in production. You start it when you want a drill, find it with the dashboards and pg_activity, and fix it. Nothing starts one by schedule.

```bash
labapp scenario list                          # every scenario: what it does, what it switches off, what it leaves behind
labapp scenario run forgotten-transaction     # start one
labapp scenario run migration-behind-report --seconds 120
labapp scenario run revoked-privilege
labapp scenario run abandoned-slot
labapp scenario run dropped-table --confirm orders
labapp scenario status                        # what is running, and when it ends by itself
labapp scenario stop                          # end them now (or: stop NAME)
```

| Scenario | Runs as | What happens | Switches off | Leaves behind |
|---|---|---|---|---|
| `forgotten-transaction` | `shop_support`, from `psql` | Updates the 50,000 most active customers and never commits. The session sits idle in its transaction and keeps the row locks, so logins of those customers fail with `lock_timeout` | Nothing. `shop_support` has no `idle_in_transaction_session_timeout`, so nothing ends the transaction but you | Nothing |
| `migration-behind-report` | `shop_migrator`, from `liquibase`, behind a report of `shop_analytics` | An `ALTER TABLE shop.orders` waits for a long report to finish. Every statement on `orders` queues behind the waiting `ALTER`: checkouts, payments, shipping, order history | `SET lock_timeout = 0` in the migration's session. With `shop_migrator`'s own 5 s, the `ALTER` gives up and the application recovers by itself | Nothing |
| `revoked-privilege` | `shop_migrator`, from `psql` | A security cleanup runs `REVOKE SELECT ON shop.orders FROM shop_app`. Every statement of the application that reads `orders` fails with `permission denied for table orders`, also an `UPDATE ... WHERE`: checkouts, payments, shipping, order history | Nothing. The owner of a table may revoke any privilege on it | Nothing |
| `abandoned-slot` | `debezium`, from `Debezium Streaming` | The change data capture connector creates its logical replication slot `debezium` and then stops, as if Kafka were down. Nothing reads the slot, so PostgreSQL keeps all WAL written from then on: about 0.7 GB per hour at the normal load. Nothing fails | Nothing. `max_slot_wal_keep_size` is unlimited by default | Nothing |
| `dropped-table` | `shop_migrator`, from `psql` | `DROP TABLE shop.orders CASCADE`, at a random moment within 4 minutes: a cleanup script meant for staging, run against production. The seven foreign keys other tables have to `orders` go with it. Checkouts, payments, shipping, order history and the reports fail | Nothing. Whoever acts as a table's owner may drop it; the only protection is a backup | **The table stays dropped** |

- **Each scenario says which protection it switches off and what it leaves behind.** The first is the staged mistake, not the lab's normal setup. Both are shown by `list` and `run`, logged, and written into the dashboard annotation.
- **The first four end by themselves and keep no change.** `forgotten-transaction` rolls back after 10 minutes, `migration-behind-report` withdraws its `ALTER` after 5, `revoked-privilege` grants the privilege again after 10, and `abandoned-slot` drops its slot after 30; `--seconds` changes that for one run. The rolled-back update does leave dead rows in `customers` for autovacuum, like any rolled-back update.
- **They also end when you fix them.** Terminate the forgotten session, the migration or the report in PostgreSQL, grant the privilege again (`GRANT SELECT ON shop.orders TO shop_app`), or drop the slot (`SELECT pg_drop_replication_slot('debezium')`), and the scenario notices and finishes.
- **`revoked-privilege` holds no lock and no session of its own,** so the lock dashboard and pg_activity show nothing unusual. What shows it: the application's errors on **Lab: Application**, `permission denied` in the PostgreSQL log, and the `REVOKE` in the [audit log](../../labs/postgresql-single/README.md#audit-who-changed-what). An idle session named `labapp-scenario` is the scenario watching for your fix.
- **`abandoned-slot` hurts nobody at first.** No error, no lock, no audit line: a slot is created by a function call, which pgAudit does not log. Not even the disk shows it for the first hours: PostgreSQL keeps about 2 GB of WAL files anyway (`max_wal_size`), and `pg_wal` only grows once the slot holds more than that. See it with:
  ```sql
  SELECT slot_name, slot_type, database, active, wal_status,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS wal_held
  FROM pg_replication_slots;
  ```
  PMM collects the same number as `pg_replication_slots_pg_wal_lsn_diff` (bytes held, per slot) and `pg_replication_slots_active`. When it ends, the scenario says how much WAL the slot held: about 50 MB after 4 minutes. More writes make it grow faster. Left alone on a real server, a slot like this fills the disk. The setting that prevents it is `max_slot_wal_keep_size`: it gives up the slot instead of the server.
- **On the dashboards:** a scenario shows on the "Running" timeline of **Lab: Application** and as a purple annotation. Who it blocks is on **PostgreSQL lock blocking**.

A scenario needs traffic to hurt: with the workload paused, nobody waits behind it.

### dropped-table destroys data

It is the one scenario that does not undo itself, so it only starts with `--confirm orders`. Without that, it prints what it would do and starts nothing.

- **When it drops the table is for you to find out.** The moment is random, and neither the log of the scenario nor its mark on the dashboards gives it away: the scenario stays "running" for its whole 10 minutes. The audit log has the `DROP TABLE` with its time, user, application and client address ([lab README](../../labs/postgresql-single/README.md#audit-who-changed-what)); the application's errors on **Lab: Application** show when it started to hurt.
- **`labapp scenario stop` only helps before the drop.** Afterwards nothing brings the table back.
- **The database stays up.** The probe keeps reporting UP, because it writes to its own database. Only the shop is broken.
- **The way back is a restore to the moment before the drop.** The lab has no backups yet. Until it does, the only way back is to [rebuild the shop database](../../labs/postgresql-single/README.md#reset-the-shop-database), which takes about 8 minutes and generates new data.
- **If a report holds the table,** the `DROP` gives up after `shop_migrator`'s `lock_timeout` of 5 s and tries again every 20 s. Each attempt holds up the statements on `orders` for those 5 seconds, so the application's lock timeouts come in short bursts. A report holds `orders` about half of the time, and the longest one (`bi_extract`) for 5 minutes: that is why the scenario runs for 10.
- **That waiting `DROP` is your chance to stop it.** It shows in pg_activity and on **PostgreSQL lock blocking** (who waits: `shop_migrator`, `psql`). Cancel it or terminate its session, and the scenario ends with nothing dropped.

## Commands

On the PMM server:

```bash
labapp outages                  # the measured outages: start, end, duration, first error
labapp outages --last 5
journalctl -u labapp-probe -f

labapp load status              # rates, mode, queued requests, running jobs
labapp load rate 60             # base rate: 60 requests per second
labapp load pause               # stop sending requests (reports and batch jobs keep their schedule)
labapp load resume
labapp load flash-sale 300      # a 5-minute flash sale, now
labapp load checkout-in-db 100  # every checkout through shop.place_order() in the database (0: none)
labapp load run stock_recount   # run a report or batch job now
labapp scenario list            # incidents on demand, see above
journalctl -u labapp-workload -f
```

`labapp load` changes last until the workload restarts, which goes back to the configured rate.

Example of `labapp outages` after `systemctl restart postgresql-17` and a 10-second stop:

```
  #  target               start (UTC)              end (UTC)                 duration  first error
  1  postgresql-source    2026-10-09T17:26:38.280  2026-10-09T17:26:39.551     1.271s  AdminShutdown: terminating connection due to administrator command
  2  postgresql-source    2026-10-09T17:26:46.784  2026-10-09T17:26:57.308    10.524s  AdminShutdown: terminating connection due to administrator command

2 outages, 11.795 s of write downtime in total.
```

## The dashboard

**Lab → Lab: Application** in PMM, in four rows:

| Row | Shows |
|---|---|
| **Overview** | Database writes UP / DOWN, the ongoing outage, target rate, completed transactions per second, shop errors, mode (normal / flash sale / paused) |
| **Availability (probe)** | Outages and downtime in the selected time range, the last outage, write latency, the UP / DOWN timeline, write and connect latency, checks per second |
| **Shop workload** | Checkout p95, queued requests, transactions per second by type against the target rate, response time by type, errors by kind and by transaction |
| **Reports and batch jobs** | When each report and batch job ran, and how long the last run took |

Outages (red) and workload events (purple) are marked on every chart. A link at the top opens **PostgreSQL lock blocking**, the lab's second dashboard: who blocks whom in the database (see the [lab README](../../labs/postgresql-single/README.md#locks-who-blocks-whom)).

## Configuration

The role's defaults (`ansible/roles/labapp/defaults/main.yml`), overridable in the lab's `group_vars`:

| Variable | Default | |
|---|---|---|
| `labapp_workload_enabled` | `true` | `false` runs only the probe |
| `labapp_workload_rate` | `100` | Base requests per second |
| `labapp_workload_checkout_in_database` | `50` | Percent of checkouts done by `shop.place_order()` in the database (`checkout_db`) |
| `labapp_workload_workers` | `24` | Connections as `shop_app` |
| `labapp_workload_mix` | the shares above | Transaction weights, e.g. `{product_page: 50, checkout: 20, ...}` |
| `labapp_workload_wave` | `{period: 3600, amplitude: 0.4}` | The traffic cycle |
| `labapp_workload_flash_sale` | every 30 min, first at 20, 3 min, 3× | |
| `labapp_workload_jobs` | the schedule above | `every`, `first` and `target` in seconds; `stock_recount` also takes `mode`, `cart_cleanup` `batch_size` |
| `labapp_workload_scenarios` | 10 min and 5 min | `seconds` each scenario runs before it ends by itself; `forgotten_transaction` also takes `customers` |

## Metrics

The probe serves port 9300 and the workload port 9302 (`/metrics`), collected by PMM as the external services `labapp-probe` and `labapp-workload`.

| Metric | Meaning |
|---|---|
| `labapp_probe_up{target, check}` | 1 if the last check succeeded |
| `labapp_probe_latency_seconds{target, check}` | Histogram of successful check durations |
| `labapp_probe_checks_total{target, check, result}` | Checks run, `result` = ok / error |
| `labapp_probe_outages_total{target}` | Completed write outages |
| `labapp_probe_downtime_seconds_total{target}` | Total write downtime |
| `labapp_probe_current_outage_seconds{target}` | Duration of the ongoing outage, 0 when up |
| `labapp_probe_last_outage_seconds{target}` | Duration of the last completed outage |
| `labapp_tx_total{tx, result}` | Shop transactions by type and result |
| `labapp_tx_latency_seconds{tx}` | Histogram of the response time: queue wait + transaction |
| `labapp_tx_queue_depth` | Requests waiting for a free worker |
| `labapp_load_base_rate`, `labapp_load_target_rate` | Base and current target rate, requests per second |
| `labapp_load_flash_sale`, `labapp_load_paused` | 1 during a flash sale, 1 while paused |
| `labapp_job_active{job_name}` | 1 while a report or batch job runs |
| `labapp_scenario_active{scenario}` | 1 while a scenario runs |
| `labapp_job_runs_total{job_name, result}` | Job runs, `result` = ok / error |
| `labapp_job_last_duration_seconds{job_name}` | Duration of the last run |
