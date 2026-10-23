# Lab: mysql-single

A single Percona Server for MySQL 8.4 server in a private subnet, monitored by PMM 3, under the production traffic of a simulated sportsbook: players betting on live matches, an odds feed, and the back office's batch jobs. It is built for practising backups: XtraBackup full and incremental backups, MySQL Shell dumps and binlog backups, with a local backup disk and an S3 bucket. The tools are installed; taking the backups is yours to do.

A replica and ProxySQL come later; the binlogs and GTIDs are already on for them.

## What it deploys

- **VPC** `10.0.1.0/24`: one public subnet, two private subnets in two availability zones, and a NAT gateway. It does not overlap with postgresql-single, so both labs can run at the same time.
- **mysql-source**: Percona Server for MySQL 8.4 in private subnet 1, on a t3.medium (4 GB: unlike PostgreSQL, MySQL keeps its cache in its own memory, so 2 GB is too little). Two extra encrypted gp3 disks:
  - **`/var/lib/mysql`** (20 GB): the data and the binlogs, as on the production servers.
  - **`/backup`** (20 GB): a local copy of the backups before they go to S3.
  
  Also installed: Percona XtraBackup 8.4, MySQL Shell, Percona Toolkit, the AWS CLI, and Dolphie to watch MySQL live.
- **mysql-pmm**: PMM 3 (Docker) in the public subnet, also the SSH jump host and the application side: the [sportsbook application](../../tools/sportsbook/README.md), Liquibase with the [sportsbook database's migrations](../../schema/sportsbook/README.md), and the MySQL client. Next to PMM, [Prometheus and Grafana](#prometheus-and-grafana) in Docker. SSH and the UIs accept connections only from your `admin_cidr`.
- **Backup bucket** and an IAM role for `mysql-source`: it may read, write and delete in `s3://<bucket>/mysql/` and nowhere else, only from the VPC. The bucket is destroyed with the lab. The bucket's settings are the same as in [postgresql-single](../postgresql-single/README.md#backups-the-s3-bucket).

Both servers run Rocky Linux 10. Change the types with `mysql_instance_type` and `pmm_instance_type` in `terraform.tfvars`, and the disks with `mysql_data_volume_size` and `mysql_backup_volume_size`.

**Cost:** about $0.15 per hour in us-east-1 while the lab runs (NAT gateway $0.045, two t3.medium $0.083, two public IPs $0.010, 70 GB of gp3 disks $0.008). CPU above the t3 baseline adds a few cents per hour with the default load. Backups in S3 cost $0.023 per GB-month: cents for a lab session. Always run `terraform destroy` when you're done.

## Before your first deploy

Run all commands from a Linux or macOS shell. On Windows, use Ubuntu on WSL (see the [root README](../../README.md#before-your-first-lab)).

1. Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` and set `aws_key_pair`, `admin_cidr` and `ssh_private_key_path`.
2. Add this line at the top of `~/.ssh/config` (this lab has its own file and host names, next to postgresql-single's):
   ```
   Include aws-mysql-single.conf
   ```
3. Install the Ansible collections the lab needs (once, and after they change):
   ```bash
   ansible-galaxy collection install -r labs/mysql-single/ansible/requirements.yml
   ```

## Deploy

```bash
cd labs/mysql-single/terraform
terraform init
terraform apply

cd ../ansible
ansible-playbook site.yml
```

`terraform apply` also writes the SSH config (`~/.ssh/aws-mysql-single.conf`) and the Ansible inventory (`ansible/inventory.ini`). The playbook:
- installs MySQL, PMM and the tools, and generates the lab passwords into `ansible/credentials/`;
- registers MySQL in PMM;
- builds the sportsbook database with Liquibase. The first run also generates its data (about 1 GB), which runs in the background while MySQL registers with PMM;
- starts the sportsbook application.

## Connect

```bash
ssh mysql-pmm                # PMM server / jump host / load generator
ssh mysql-source             # MySQL, through the jump host
```

- **On `mysql-pmm`:** `mysql` logs you in to `mysql-source` as `dba`, from `~/.my.cnf`.
- **On `mysql-source`:** `sudo mysql` logs you in as `root` over the socket, from `/root/.my.cnf`.

| User | From | Can | Password in `ansible/credentials/` |
|---|---|---|---|
| `root` | the server itself (socket) | everything | `mysql_root_password` |
| `dba` | the PMM server | everything | `mysql_dba_password` |
| `sbtest` | the PMM server | the `sbtest` database | `mysql_sbtest_password` |
| `pmm` | the server itself | read statistics, for PMM | `mysql_pmm_password` |
| `backup` | the server itself (socket) | what XtraBackup needs and no more | `mysql_backup_password` |
| `sb_migrator` | the PMM server | owns the `sportsbook` database, runs its migrations | `mysql_sb_migrator_password` |
| `sb_app`, `sb_feed`, `sb_batch`, `sb_reporting`, `sb_support` | the PMM server | their role's privileges in `sportsbook` ([who may do what](../../schema/sportsbook/README.md#who-may-do-what)) | `mysql_sb_<name>_password` |

`skip_name_resolve` is on, so accounts are matched by IP: everyone but `root`, `pmm` and `backup` logs in only from the PMM server's private IP.

### Prometheus and Grafana

Next to PMM on `mysql-pmm`, in Docker (one Compose project in `/opt/prometheus-grafana`): the stack your own exporters and backup scripts will report to.

| | URL (`terraform output`) | Login |
|---|---|---|
| Grafana 13.2 | `grafana_url`: `http://<pmm public IP>:3000` | `admin`, password in `ansible/credentials/grafana_admin_password` |
| Prometheus 3.15 | `prometheus_url`: `http://<pmm public IP>:9090` | none; only your IP reaches it |

- **Prometheus** scrapes every 15 s and keeps 1 day. It scrapes itself and Grafana; no alert rules yet.
- **The lab's servers** are found through the EC2 API by their `Lab` tag (EC2 service discovery): no IP in the configuration, and a new server (a replica, ProxySQL) is found by itself. The PMM server's IAM role (`mysql-single-monitoring`) may only list the instances. Until the exporters are installed, the servers show under *Status → Service discovery* with their labels, and nothing is scraped from them.
- **Grafana** has Prometheus as its data source, and no dashboards yet.
- On the server: `docker logs prometheus`, `docker logs grafana`. Changes to the configuration belong in the role (`ansible/roles/prometheus_grafana`): the playbook overwrites them, checks `prometheus.yml` with `promtool` and restarts what changed.

### Watch MySQL live: Dolphie

[Dolphie](https://github.com/charles-001/dolphie) is a terminal dashboard of the server, refreshed every second: the processlist, open transactions and their locks, InnoDB, replication, and graphs of the server's metrics. PMM shows the history; Dolphie shows this second. On `mysql-source`:

```bash
sudo -i dolphie                                          # dashboard and processlist; ? opens the command list
sudo -i dolphie --panels dashboard,processlist,graphs,metadata_locks
sudo -i dolphie --show-trxs-only                         # only sessions with an open transaction
sudo -i dolphie --filters time=2                         # only statements running 2 s or longer
```

It logs in as `root` over the socket, from `/root/.my.cnf`. To keep a record of an incident, start it with `--record --replay-dir /root/dolphie-replays`, and look through it later with `--replay-file`.

## How MySQL is set up

The settings are in `/etc/my.cnf.d/lab.cnf`, sized from the server's RAM and commented. Re-running the playbook overwrites manual changes there; `SET PERSIST` survives.

| What | Where / value |
|---|---|
| Data | `/var/lib/mysql` (its own disk) |
| Binlogs | `/var/lib/mysql/mysql-bin.NNNNNN`, kept 3 days, `sync_binlog = 1` |
| GTIDs | on (`gtid_mode`, `enforce_gtid_consistency`), ready for a replica |
| Error log | `/var/log/mysqld.log` |
| Slow log | `/var/lib/mysql/mysql-source-slow.log`, statements over 0.5 s |
| Buffer pool | about half the RAM (1.6 GB on the t3.medium: the 1 GB of sportsbook data fits) |
| Temporary tables | `temptable_max_ram` about 1/16 of the RAM (192 MB), instead of 8.4's default of at least 1 GB, which stalled a 2 GB server |
| Durability | `innodb_flush_log_at_trx_commit = 1` and `sync_binlog = 1` |
| Password policy | `validate_password` on, policy `LOW`: 8 characters or more. MEDIUM (the default) also requires a digit, both cases and a special character, which the generated passwords may lack |

PMM's Query Analytics reads the statements from the Performance Schema.

## The traffic: a sportsbook

The [sportsbook application](../../tools/sportsbook/README.md) runs on `mysql-pmm` as the service `sportsbook-workload`:

- **Players:** about 80 requests a second, more after each kick-off. They browse, follow live odds, log in, bet, cash out, deposit and withdraw.
- **An odds feed:** matches kick off every 10 minutes and last 15. Odds move, goals suspend markets, and full time decides every bet.
- **Batch jobs and reports:**
  - settlement pays out the winners in one big transaction per match;
  - withdrawals, bonuses and the leaderboard run on schedules;
  - a regulatory report holds one snapshot open for 5 minutes.

```bash
sportsbook load status                  # rates, live matches, running jobs
sportsbook load kickoff 10              # kick off 10 matches now: the players surge
journalctl -u sportsbook-workload -f
```

Every bet, win, deposit and withdrawal is in the ledger, and the wallets always add up to it. The same check after a restore shows whether the restored copy is consistent ([the database](../../schema/sportsbook/README.md)).

**sysbench on demand:** `ansible-playbook sysbench.yml` adds a synthetic `oltp_read_write` load in its own `sbtest` database (8 tables of 1,000,000 rows, 50 transactions a second), for example to compare throughput before and after a configuration change. `sudo systemctl stop sysbench-workload` on `mysql-pmm` stops it.

## Backups

Taking backups is yours to practise. The lab gives you:

- **The tools on `mysql-source`:** `xtrabackup` (Percona XtraBackup 8.4: full and incremental physical backups, while MySQL runs), `mysqlsh` (MySQL Shell: `util.dumpInstance()` and `util.loadDump()`), and `aws` (the AWS CLI).
- **A backup user** with only the privileges XtraBackup needs: `backup`, over the socket. `sudo xtrabackup` without `--user` logs in as root from `/root/.my.cnf` instead.
- **The local backup disk** at `/backup`, and the server's S3 folder. Both are in every login shell as `$BACKUP_DIR` and `$BACKUP_S3` (`s3://<bucket>/mysql`), also after `sudo -i`.
- **S3 access through the server's IAM role:** no keys on the server. `aws s3 ls "$BACKUP_S3/"` works from the start.

The bucket keeps whatever you put there until the lab is destroyed: delete old backups yourself. Overwritten and deleted files stay recoverable as older versions for 7 days.

**The binlogs** are where the binlog backup script in `dba-ops-scripts` expects them: in `/var/lib/mysql`, named `mysql-bin.NNNNNN`. That script uploads to Google Cloud Storage; porting it and the full backup script to S3 is the next step for this lab.

## Destroy

```bash
cd labs/mysql-single/terraform
terraform destroy
```

It also deletes the backup bucket with everything in it, and removes the SSH config and the inventory.

## Tested

The first deploy (23 October 2026, Percona Server 8.4.11-11 on a t3.medium) checked:
- **XtraBackup 8.4.0-7** backs up the newer server as the `backup` user: a full backup in 14 seconds, under load. Prepare and restore are yours to practise.
- **The sportsbook's migrations and data:** all 47 changesets, about 1 GB.
- **The sportsbook application,** over a full match cycle:
  - 80 requests a second plus the kick-off surges, 99.99% of them under 0.5 s;
  - no deadlocks, lock timeouts or connection errors;
  - matches kick off, score, finish and are settled;
  - every batch job and the regulatory report run, and every player's wallet agrees with the ledger.
- **Running the playbook again** changes nothing on the MySQL server.

Not tested yet:
- **How long the data takes on a new lab:** the first deploy generated it in two parts, around a memory stall on a t3.small (since fixed: the t3.medium and the temporary table limit).
- **sysbench over TLS** (on demand): its login with MySQL 8.4's default authentication.
