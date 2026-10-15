# The shop database

An online shop's database: 28 tables, about 40 foreign keys, hot rows, big append-only tables and a partitioned audit log. It's built and changed only through **Liquibase migrations** in this folder, like a production database owned by an application team. The [labapp workload](../../tools/labapp/README.md#the-workload) runs the shop's traffic, reports and batch jobs against it while you do DBA work.

## How it's built

```
changelog.yaml          master changelog: applies every file in changes/ in name order
changes/0001-...sql     schema migrations ("formatted SQL" changesets)
changes/1001-...sql     data generation (context "data", sized by the parameter shop_scale)
```

Ansible copies this folder to the PMM server and runs `liquibase update` as `shop_migrator`. Liquibase connects to PostgreSQL over the network, like an application's deploy pipeline, and records every changeset it ran in `DATABASECHANGELOG`, so each one runs exactly once.

Liquibase's own tables are pinned to the `public` schema (`liquibase-schema-name` in its settings). Otherwise Liquibase uses the first schema of the session's `search_path`, and `0001-search-path` (which puts `shop` first) would make it lose track of its history.

## Roles

| Role | Login | Purpose |
|---|---|---|
| `shop_owner` | no | Owns every shop object. Nobody logs in as the owner |
| `shop_migrator` | yes | Runs the migrations. Acts as `shop_owner` automatically (`ALTER ROLE ... SET role`), `lock_timeout` 5 s |
| `shop_app` | yes | The application: `SELECT/INSERT/UPDATE/DELETE` only. `statement_timeout` 5 s, `lock_timeout` 2 s, `idle_in_transaction_session_timeout` 30 s, 60 connections |
| `shop_reporting` | yes | Reports: `SELECT` only, `statement_timeout` 60 s, `work_mem` 32 MB, 10 connections |
| `shop_analytics` | yes | Long-running reports and exports: `SELECT` only, `statement_timeout` 15 min, `work_mem` 64 MB, 5 connections |
| `shop_batch` | yes | Scheduled batch jobs: `SELECT`, plus `UPDATE` on `inventory` and `carts`, `DELETE` on `carts` and `cart_items`, `INSERT` on `stock_movements`. `statement_timeout` 10 min, 5 connections |
| `shop_support` | yes | People, not programs: support staff who look up anything and fix customer records by hand. `SELECT`, plus `UPDATE` on `customers` and `customer_addresses`. `statement_timeout` 60 s, 5 connections, and no limit on how long a transaction may stay open |
| `pgwatch` | yes | The second monitoring, next to PMM ([pgwatch demo](../../labs/postgresql-single/README.md#pgwatch-next-to-pmm-a-try-out)): `pg_monitor` only, which reads statistics and no data. 20 connections: about 2 for every database it watches |
| `debezium` | yes | A program: the change data capture connector, which sends the order tables' changes to Kafka. `SELECT` on `orders`, `order_items` and `payments`, and `REPLICATION` to read the WAL through a logical replication slot. 3 connections |

The workload uses each role for its part: the application as `shop_app`, the reports and batch jobs as the next three. `shop_support`, `shop_migrator` and `debezium` are who the [scenarios](../../tools/labapp/README.md#scenarios-incidents-on-demand) play. Grants come from default privileges (`0001-schema-and-access.sql`, `0010-analytics-and-batch-access.sql`, `0011-support-access.sql`), so new tables get them automatically. Only these roles (and `pmm`) can connect to the `shop` database, only from the PMM server.

Every other session, DBAs included, has the server-wide `lock_timeout` of 5 s (`conf.d/01-lab.conf`): a session that waits longer for a lock gives up instead of queueing, so others don't pile up behind it. For long maintenance, `SET lock_timeout = 0` in your session.

## Tables

| Area | Tables | Notes |
|---|---|---|
| Reference | `countries`, `carriers`, `warehouses` | Small, referenced everywhere |
| Catalog | `categories` (tree), `brands`, `suppliers`, `products`, `product_prices`, `product_attributes`, `product_reviews` | |
| Stock | **`inventory`**, **`stock_movements`** | 🔥 Every order updates the stock of its products, and a few popular products get most orders · 📈 append-only ledger |
| Customers | **`customers`**, `customer_addresses`, `wishlists` | 🔥 frequent updates (`last_seen_at`) |
| Carts | **`carts`**, **`cart_items`** | 🔥 constant inserts, updates and deletes: a bloat generator |
| Orders | **`orders`**, **`order_items`**, **`order_status_history`**, **`payments`**, `refunds`, `invoices`, `coupons`, `order_coupons` | 🔥📈 the biggest and busiest tables |
| Delivery | `shipments`, **`shipment_events`** | 📈 append-only, time-ordered, BRIN index on `created_at` |
| Platform | **`audit_log`** | 📈 partitioned by month (the last 12 and the next 3 months) |

Popularity is skewed like real traffic: about 21% of all order lines are for the top 1% of products. Ids grow with time, as in a real table.

**Size:** `shop_scale: 1` (the default, `group_vars/all.yml`) generates about 600,000 orders and 1.5 million order lines. The scale only applies when the data is first generated.

## Migrations: the workflow

1. Add a file in `changes/`, numbered after the last one, e.g. `changes/0014-add-orders-channel.sql`:
   ```sql
   --liquibase formatted sql

   --changeset yourname:0014-add-orders-channel
   --comment: Where the order came from.
   ALTER TABLE shop.orders ADD COLUMN channel text NOT NULL DEFAULT 'web';
   --rollback ALTER TABLE shop.orders DROP COLUMN channel;
   ```
2. Apply it, from `labs/postgresql-single/ansible`:
   ```bash
   ansible-playbook migrate.yml
   ```
3. Watch the cost on the **Lab: Application** dashboard and in PMM while the workload runs: downtime, queued requests, lock timeouts.

On the PMM server, `liquibase-shop` runs Liquibase directly:

```bash
liquibase-shop status --verbose     # pending changesets
liquibase-shop update-sql           # the SQL that update would run, without running it
liquibase-shop history              # what ran, and when
liquibase-shop rollback-count 1     # undo the last changeset (its --rollback)
```

Rules, as in production:

- **Never edit an applied changeset.** Liquibase stores a checksum per changeset and refuses to continue when one changes. Fix things with a new changeset.
- **`CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY` can't run in a transaction:** add `runInTransaction:false` to the changeset line.
- **A migration that can't get its lock within 5 s fails** (`lock_timeout` of `shop_migrator`) instead of queueing and blocking the application behind it. Retry it, or find what holds the lock.

## Database code: triggers, a function, a procedure

Part of the shop's logic runs in the database (migrations `2001` to `2004`). They are numbered after the data (`1xxx`), so a new lab loads its generated data before any trigger exists: a trigger during the load would rewrite the generated timestamps and history.

| Object | Kind | What it does | Used by |
|---|---|---|---|
| `shop.set_updated_at()` | Trigger, `BEFORE UPDATE`, on `orders`, `payments`, `carts` and `inventory` | Sets `updated_at` on every update of a row, whoever runs it | Every update. `inventory` is the hottest table: every checkout pays for this trigger |
| `shop.record_order_status()` | Trigger, `AFTER INSERT OR UPDATE OF status`, on `orders` | Writes `order_status_history`: one row per status an order gets | Checkout, payment, shipping |
| `shop.place_order(customer, warehouse, items, method, provider_ref)` | Function | The whole checkout in one call: stock, order, lines, stock movements, payment, cart, audit log. Out of stock raises error code `SH001` and rolls everything back | The workload's `checkout_db`, a share of the checkouts |
| `shop.cleanup_carts(batch_size)` | Procedure | Abandons idle carts and deletes old ones, with a commit after each batch | The workload's `cart_cleanup` job |

- **Code changesets run on change** (`runOnChange:true`): edit the code in its file, and the next `migrate.yml` deploys the new version with `CREATE OR REPLACE`. That is the one exception to "never edit an applied changeset" below. A function body contains semicolons, so those changesets also carry `splitStatements:false` and hold one statement each.
- **Who may call them:** functions and procedures can be called by everyone (`PUBLIC`) until that is revoked. `place_order` is for `shop_app` only, `cleanup_carts` for `shop_batch` only. Both run with the caller's rights (`SECURITY INVOKER`, the default).
- **A procedure that commits must be called outside a transaction block:** `CALL shop.cleanup_carts(5000, 0, 0)` in autocommit, never after a `BEGIN`.
- **Where to see them:** `\df shop.*` lists the functions and procedures, `\d shop.orders` the table's triggers. `pg_stat_user_functions` has calls, total and own time per function (`track_functions = 'pl'`).
- **Query Analytics shows the call, not what's inside.** `SELECT shop.place_order(...)` is one statement there. The statements inside appear only with `pg_stat_monitor.pgsm_track = 'all'` (default `top`), for example `ALTER ROLE shop_app SET pg_stat_monitor.pgsm_track = 'all'` and a restart of the workload so its sessions reconnect.

## Exercises: the built-in flaws

The schema is mostly clean, with six typical production problems built in on purpose. Find them with PMM, pg_activity and the catalog, then fix them with migrations while the workload runs, without downtime on the dashboard.

1. **An integer primary key close to its limit.** `customers.id` is an `integer` (max 2,147,483,647) and its identity starts at 2,140,000,000, so about 7 million new customers are left. Six tables reference it with `integer` columns.
   ```sql
   SELECT last_value, 2147483647 - last_value AS left FROM pg_sequences WHERE sequencename = 'customers_id_seq';
   ```
   Goal: `bigint` everywhere, without locking `customers` and `orders` for minutes.

2. **A foreign key without an index.** `order_items.product_id` references `products`, but nothing indexes it: every delete or key change in `products` scans all of `order_items`, under lock.
   ```sql
   SELECT conrelid::regclass AS table_name, conname
   FROM pg_constraint c
   WHERE contype = 'f'
     AND NOT EXISTS (SELECT 1 FROM pg_index i
                     WHERE i.indrelid = c.conrelid AND (i.indkey::int2[])[0:cardinality(c.conkey) - 1] = c.conkey);
   ```
   Goal: the index, built without blocking writes.

3. **The biggest, fastest-growing table isn't partitioned.** `orders` grows every second and old orders are rarely read. Goal: monthly partitions like `audit_log`, converted online.

4. **A duplicate index.** `customers_email_idx` indexes the same column as the unique constraint `customers_email_key`: every write pays for both, and nothing reads the duplicate.
   ```sql
   SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid)) FROM pg_stat_user_indexes WHERE relname = 'customers';
   ```
   Goal: drop it without blocking writes.

5. **A table without a primary key.** `shipment_events` has none. Logical replication (for example an upgrade to PostgreSQL 18 with almost no downtime) can't replicate updates and deletes of such a table. Goal: a primary key, or a replica identity, before you need it.

6. **A frequent query without a fitting index.** One of the application's queries reads a whole table every time it runs. Find it in PMM **Query Analytics** while the workload runs (sort by load, compare rows examined with rows sent). Goal: an index built without blocking writes, and the query's time dropping in Query Analytics.

**Also on the calendar:** `audit_log` has partitions for the next 3 months and no default partition. When the last one runs out, every insert fails, so creating future partitions is a recurring job.
