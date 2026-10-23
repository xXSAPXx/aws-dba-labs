# Lab: mysql-single

A single Percona Server for MySQL 8.4 server in a private subnet, monitored by PMM 3, under a steady sysbench load. It is built for practising backups: XtraBackup full and incremental backups, MySQL Shell dumps and binlog backups, with a local backup disk and an S3 bucket. The tools are installed; taking the backups is yours to do.

A replica and ProxySQL come later; the binlogs and GTIDs are already on for them.

## What it deploys

- **VPC** `10.0.1.0/24`: one public subnet, two private subnets in two availability zones, and a NAT gateway. It does not overlap with postgresql-single, so both labs can run at the same time.
- **mysql-source**: Percona Server for MySQL 8.4 in private subnet 1, on a t3.small. Two extra encrypted gp3 disks:
  - **`/var/lib/mysql`** (20 GB): the data and the binlogs, as on the production servers.
  - **`/backup`** (20 GB): a local copy of the backups before they go to S3.
  
  Also installed: Percona XtraBackup 8.4, MySQL Shell, Percona Toolkit and the AWS CLI.
- **mysql-pmm**: PMM 3 (Docker) in the public subnet, also the SSH jump host and the load generator (the MySQL client and sysbench). SSH and the PMM UI accept connections only from your `admin_cidr`.
- **Backup bucket** and an IAM role for `mysql-source`: it may read, write and delete in `s3://<bucket>/mysql/` and nowhere else, only from the VPC. The bucket is destroyed with the lab. The bucket's settings are the same as in [postgresql-single](../postgresql-single/README.md#backups-the-s3-bucket).

Both servers run Rocky Linux 10. Change the types with `mysql_instance_type` and `pmm_instance_type` in `terraform.tfvars`, and the disks with `mysql_data_volume_size` and `mysql_backup_volume_size`.

**Cost:** about $0.12 per hour in us-east-1 while the lab runs (NAT gateway $0.045, t3.medium $0.042, t3.small $0.021, two public IPs $0.010, 70 GB of gp3 disks $0.008). CPU above the t3 baseline adds a few cents per hour with the default load. Backups in S3 cost $0.023 per GB-month: cents for a lab session. Always run `terraform destroy` when you're done.

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
- creates the sysbench tables (about 2 GB, several minutes on the first run) and starts the load.

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

`skip_name_resolve` is on, so accounts are matched by IP: `dba` and `sbtest` only from the PMM server's private IP.

## How MySQL is set up

The settings are in `/etc/my.cnf.d/lab.cnf`, sized from the server's RAM and commented. Re-running the playbook overwrites manual changes there; `SET PERSIST` survives.

| What | Where / value |
|---|---|
| Data | `/var/lib/mysql` (its own disk) |
| Binlogs | `/var/lib/mysql/mysql-bin.NNNNNN`, kept 3 days, `sync_binlog = 1` |
| GTIDs | on (`gtid_mode`, `enforce_gtid_consistency`), ready for a replica |
| Error log | `/var/log/mysqld.log` |
| Slow log | `/var/lib/mysql/mysql-source-slow.log`, statements over 0.5 s |
| Buffer pool | about half the RAM |
| Durability | `innodb_flush_log_at_trx_commit = 1` and `sync_binlog = 1` |

PMM's Query Analytics reads the statements from the Performance Schema.

## The load

sysbench `oltp_read_write` runs on `mysql-pmm` as the service `sysbench-workload`. It runs 50 transactions a second over 8 connections; each transaction is 14 reads and 4 writes. Its 8 tables of 1,000,000 rows are about 2 GB, more than the buffer pool, so some reads go to disk.

```bash
journalctl -u sysbench-workload -f      # one line per minute: tps, qps, latency, errors
sudo systemctl stop sysbench-workload   # stop the load; start brings it back
```

Change the rate or the size with `sysbench_rate`, `sysbench_threads`, `sysbench_tables` and `sysbench_table_size` in `ansible/group_vars/pmm_server.yml`, then re-run the playbook. A new size needs the old tables removed first: `sysbench oltp_read_write --mysql-host=mysql-source ... cleanup`. The load keeps going through a MySQL restart: sysbench counts the errors and reconnects.

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

## Not yet tested

This lab has not been deployed yet. The first deploy checks three things:
- **XtraBackup 8.4.0-7 and the newest Percona Server 8.4:** that XtraBackup accepts the server's version.
- **sysbench over TLS:** its login with MySQL 8.4's default authentication.
- **The generated passwords:** that the server accepts them as they are.
