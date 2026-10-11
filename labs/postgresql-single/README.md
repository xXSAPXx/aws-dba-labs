# Lab: postgresql-single

A single Percona PostgreSQL 17 server in a private subnet, monitored by PMM 3. Use it to practise configuration, query analysis with pg_stat_monitor and PMM, and the basics before the HA labs. pgBackRest and pg_repack are installed.

## What it deploys

- **VPC** `10.0.0.0/24`: one public subnet, two private subnets in two availability zones, and a NAT gateway.
- **postgresql-source**: Percona PostgreSQL 17 with pg_stat_monitor and data checksums, in private subnet 1. Its data lives on a separate encrypted gp3 volume (20 GB by default, mounted at `/var/lib/pgsql`). The configuration is sized from the server's RAM and CPUs (`conf.d/01-lab.conf`, every setting commented), and `pg_hba.conf` lets each user reach only its own database, only from the PMM server.
- **pmm-server**: PMM 3 (Docker) in the public subnet, also the SSH jump host and the application side: the labapp probe and shop workload, and the load tools (psql, pgbench). SSH and the PMM UI accept connections only from your `admin_cidr`.

Both servers run Rocky Linux 10 (the newest official image at deploy time): PostgreSQL on a t3.small, the PMM server on a t3.medium because it also generates the load. Change them with `postgresql_instance_type` and `pmm_instance_type` in `terraform.tfvars`; Rocky Linux 10 needs a current type such as t3 or m7i, not t2.

t3 instances are burstable: each may use 20% of its vCPUs continuously and more in bursts, paid for with CPU credits, and a new one starts with none. The lab runs them in `unlimited` mode, so they are never throttled and CPU above the 20% is billed instead. With `cpu_credits = "standard"` in `terraform.tfvars` the price is fixed, but a server without credits is throttled, which looks like a slow database or a slow application in PMM. For heavy load tests, use a non-burstable type for PostgreSQL such as `m7i.large`.

**Cost:** about $0.12 per hour in us-east-1 while the lab runs (NAT gateway $0.045, t3.medium $0.042, t3.small $0.021, two public IPs $0.010, 40 GB of gp3 disks $0.004). CPU above the t3 baseline adds well under a cent per hour with the default workload, and at most $0.16 per hour if both servers run flat out. Always run `terraform destroy` when you're done.

Terraform (`terraform/`) builds the infrastructure. Ansible (`ansible/`) installs and configures everything on the servers; [how the Ansible part works](../../ansible/README.md).

## Before your first deploy

Run all commands from a Linux or macOS shell. On Windows, use Ubuntu on WSL (see the [root README](../../README.md#before-your-first-lab)).

1. Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` and set `aws_key_pair`, `admin_cidr` and `ssh_private_key_path`.
2. Add this line at the top of `~/.ssh/config`:
   ```
   Include aws-postgresql-lab.conf
   ```
3. Install the Ansible collections the lab needs (once, and after they change):
   ```bash
   ansible-galaxy collection install -r labs/postgresql-single/ansible/requirements.yml
   ```

## Deploy

```bash
cd labs/postgresql-single/terraform
terraform init
terraform apply              # about 3 minutes

cd ../ansible
ansible-playbook site.yml    # about 10 minutes on the first run
```

`terraform apply` also writes the SSH config (`~/.ssh/aws-postgresql-lab.conf`) and the Ansible inventory (`ansible/inventory.ini`) with the new IPs. The playbook installs PostgreSQL and PMM, generates the lab passwords into `ansible/credentials/`, registers PostgreSQL in PMM, builds the shop database with Liquibase (the first run also generates its data, about 4 minutes), and starts the application: the labapp probe and the shop workload.

## Connect

```bash
ssh pmm-server               # PMM server / jump host
ssh postgresql-source        # PostgreSQL, through the jump host
```

The PMM server only relays the connection (ProxyJump), so your private key never leaves your machine.

Database logins: on `postgresql-source`, `sudo -u postgres psql`; over the network from the PMM server, your admin user `dba` (password in `~/.pgpass`, also in `ansible/credentials/postgresql_dba_password`):

```bash
ssh pmm-server
psql -h postgresql-source -U dba postgres
```

PMM UI: the `pmm_url` from `terraform output`. Log in as `admin` with the password from `ansible/credentials/pmm_admin_password`.

## The shop database

The lab's application database: an online shop with 28 tables, about 40 foreign keys, hot rows and big tables, about 1.6 GB of generated data, and five documented production flaws to fix as exercises. It's built and changed only through Liquibase migrations: see [schema/shop](../../schema/shop/README.md).

To change the schema, add a migration file to `schema/shop/changes/` and apply only the migrations (seconds, everything else keeps running):

```bash
cd labs/postgresql-single/ansible
ansible-playbook migrate.yml
```

On the PMM server, `liquibase-shop status`, `update-sql`, `history` and `rollback-count` run Liquibase directly. The shop users log in from there too (passwords in `~/.pgpass`):

```bash
psql -h postgresql-source -U shop_app shop         # the application: DML only, 5 s statement timeout
psql -h postgresql-source -U shop_reporting shop   # reports: read-only
```

## The shop workload

From the end of the playbook, the [labapp workload](../../tools/labapp/README.md#the-workload) on the PMM server runs the shop's traffic against the `shop` database, like a production application: about 30 requests per second of browsing, carts, checkouts, payments and shipping, rising and falling in an hourly cycle, with a flash sale every 30 minutes. Long-running reports (30 s, 1 min and 5 min) and batch jobs (cart cleanup, restock, a stock recount that locks every stock row for 90 s) run on schedules that fit a 1–2 hour lab.

Watch it on the **Lab → Lab: Application** dashboard in PMM: throughput against the target rate, response times, errors by kind (lock timeouts, cancels, deadlocks, connection errors), and when each report and batch job ran. Control it from the PMM server:

```bash
ssh pmm-server
labapp load status              # rates, mode, queued requests, running jobs
labapp load rate 60             # more traffic
labapp load flash-sale 300      # a 5-minute flash sale, now
labapp load run stock_recount   # a batch job, now
labapp load pause               # quiet, e.g. to look at one query in isolation
labapp load resume
```

Every change you make to the shop database, from a migration to a restart or a killed session, shows up there as the application sees it.

For a drill, start an incident yourself. A [scenario](../../tools/labapp/README.md#scenarios-incidents-on-demand) plays a person or a tool making a production mistake, says which protection it switches off, and ends by itself:

```bash
labapp scenario list                          # what each one does and what it switches off
labapp scenario run forgotten-transaction     # a support engineer leaves a transaction open
labapp scenario run migration-behind-report   # an ALTER TABLE waits behind a long report, with SET lock_timeout = 0
labapp scenario stop                          # end them now
```

## More load: pgbench

The PMM server acts as the application: it sends load to PostgreSQL over the network, like an application server would. `psql` and `pgbench` are installed there, and the `bench` user's password is in `~/.pgpass` (for `rocky` and `root`) for every PostgreSQL server of the lab, so you only name the server:

```bash
ssh pmm-server
pgbench -h postgresql-source -U bench -i -s 20 bench               # create ~300 MB of test data (once)
pgbench -h postgresql-source -U bench -c 8 -j 2 -T 300 -P 10 bench # 8 clients for 5 minutes, progress every 10 s
psql -h postgresql-source -U bench bench                           # SQL shell as the bench user
```

Watch the effect in PMM: **Dashboards → PostgreSQL → PostgreSQL Instance Summary**, and **Query Analytics**.

For a live view on the database server itself, two tools are installed. Run them as the `postgres` OS user:

```bash
ssh postgresql-source
sudo -iu postgres pg_activity    # overview
sudo -iu postgres pg_top         # drill down into one PID
```

- **[pg_activity](https://github.com/dalibo/pg_activity)**: all sessions with their queries, waits and per-process CPU / memory / IO. `F1` / `F2` / `F3` show running / waiting / blocking queries; select a process with the arrow keys and press `C` to cancel or `K` to terminate it. `h` lists all keys.
- **[pg_top](https://pg_top.gitlab.io/)**: press a key, then enter a PID. `Q` shows its full query, `E` its EXPLAIN plan, `L` the locks it holds. `A` runs EXPLAIN ANALYZE, which **executes the statement again**, so never use it on an UPDATE or DELETE.

### A report from the server log: pgBadger

[pgBadger](https://github.com/darold/pgbadger) is installed on the database server for when you need it. It turns PostgreSQL's log into one HTML report: the slowest and most frequent slow statements (over 500 ms), lock waits, errors, temporary files, checkpoints and autovacuum runs. It only reads the log files, so it costs the database nothing. Nothing runs on a schedule.

```bash
ssh postgresql-source
sudo -iu postgres bash -c 'pgbadger --prefix "%m [%p] %q%u@%d app=%a client=%h " -f stderr --outdir /tmp -o pgbadger.html /var/lib/pgsql/17/data/log/postgresql-*.log'
```

- `--prefix` must match the server's `log_line_prefix` exactly, or pgBadger finds nothing in the log.
- The command runs through `bash -c` as `postgres`, because only that user can read the log directory and expand the `*`.
- For a report you can read in the terminal, use `-o pgbadger.txt`.

The report is one self-contained file. Copy it to your machine and open it in a browser:

```bash
scp postgresql-source:/tmp/pgbadger.html .
```

For locks, look at **Locks → Most frequent waiting queries** and **Events → Most frequent errors/events**. The waiting-queries ranking only counts waits that ended by getting the lock; waits cancelled by a lock timeout are under Events. The log holds one file per weekday, so a report covers at most the last seven days.

## Measure downtime

While the lab runs, the [labapp probe](../../tools/labapp/README.md) on the PMM server writes to PostgreSQL four times a second, like an application would. Whatever you do to the database, it measures what the application experiences: every outage with its exact start, end and duration, and the write and connect latency.

- **In PMM:** the **Lab → Lab: Application** dashboard shows UP / DOWN, the ongoing outage, outages and downtime in the selected time range, and latency, next to the shop workload. Outages are also marked in red on PMM's PostgreSQL dashboards.
- **On the PMM server:** `labapp outages` lists every outage measured:
  ```bash
  ssh pmm-server
  labapp outages
  ```

Try it: `ssh postgresql-source`, run `sudo systemctl restart postgresql-17`, then check `labapp outages`. A restart costs the application about a second of write downtime.

## Locks: who blocks whom

PostgreSQL keeps no history of lock waits, and PMM has no view of them for PostgreSQL: Query Analytics has no lock time, and the dashboards only count locks by type. What the server gives you:

| Where | Shows | History |
|---|---|---|
| pg_activity (`F2` waiting, `F3` blocking), or `pg_stat_activity` with `pg_blocking_pids()` | Blocker and waiting sessions, with their statements | Live only |
| The server log, `/var/lib/pgsql/17/data/log/` (`log_lock_waits`), raw or as a [pgBadger report](#a-report-from-the-server-log-pgbadger) | Every wait longer than 1 s: the waiting statement, the table, the blocker's process ID | Yes. Find the blocker with `sudo grep '\[<pid>\]'` in the same log; it's only there if one of its statements took longer than 500 ms |

The lab adds the missing piece to PMM: the **Lab → PostgreSQL lock blocking** dashboard. Custom queries of the PMM client sample `pg_stat_activity` every 5 seconds and record the sessions at the **root** of lock waits, so PMM keeps the history:

- **Who blocks:** sessions blocked over time by blocking user and by application, and which user caused the most waiting in the selected time range. Click a user to filter the dashboard to it.
- **What they run:** the blocking statements of the selected users, with the most sessions each blocked and how long its transaction was open.
- **Who waits:** the users and applications being held up.

It opens from the link at the top of **Lab: Application**. Look at it after you have tried an exercise yourself: it names the culprit.

How to read it, and how to install the same in another PMM: [PostgreSQL lock blocking](../../ansible/roles/pmm_client/README.md). Switch it off with `pmm_client_lock_blocking_enabled: false`.

## Break, fix, reset

Break whatever you like on the servers. Running `ansible-playbook site.yml` again puts everything Ansible manages back into the known-good state: packages, configuration files, the `pmm` user, services and the PMM registration. It changes only what differs. It does not restore data you deleted.

## Tear down

```bash
cd labs/postgresql-single/terraform
terraform destroy
```

This also removes the SSH config and the inventory. The passwords stay in `ansible/credentials/` and are reused on the next deploy.
