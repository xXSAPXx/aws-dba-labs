# aws-dba-labs

Hands-on database labs on AWS that simulate a production environment. Each lab is deployed with Terraform, put under load, broken and fixed like a real incident, then torn down so you only pay for the hours you use.

## How a lab works

1. **Deploy:** `terraform apply` builds the network and the servers, then `ansible-playbook site.yml` installs and configures the databases and monitoring (PMM).
2. **Load:** generate test traffic so the database is under stress.
3. **Break / fix:** kill nodes, fill disks, restore from backup, and watch it all in PMM. Re-running the playbook resets the configuration to a known-good state.
4. **Tear down:** `terraform destroy` removes everything.

## Labs

| Lab | What it deploys | Status |
|---|---|---|
| [postgresql-single](labs/postgresql-single/) | Percona PostgreSQL 17 + PMM 3 | Available |
| [mysql-single](labs/mysql-single/) | Percona Server for MySQL 8.4 + PMM 3 under a simulated sportsbook's traffic, backup disk and S3 bucket for XtraBackup and binlog backups | Available |
| postgresql-patroni | Patroni HA cluster with etcd, pgBackRest backups to S3 | Planned |
| pxc | Percona XtraDB Cluster | Planned |
| mongodb-replicaset | MongoDB replica set | Planned |

## Repository layout

```
modules/              Shared Terraform modules, used by every lab
  vpc/                VPC, public + private subnets, NAT gateway, routing, S3 gateway endpoint
  pmm_server/         PMM 3 server instance, also the SSH jump host
  db_server/          A database server: instance, security group, data volume, optional backup volume
  iam_roles/          A database server's IAM role (instance profile): its folder of the backup bucket
  ec2_discovery_role/ A monitoring server's IAM role (instance profile): list the EC2 instances, for Prometheus
  s3_for_backups/     S3 bucket for backups: versioned, encrypted, private, TLS only
ansible/roles/        Shared Ansible roles, used by every lab (how it works: ansible/README.md)
  common/             Hostname, /etc/hosts, EPEL, admin tools, Cockpit off
  data_volume/        Formats and mounts a database server's EBS volumes (data, backup)
  percona_release/    Percona repository tool
  pmm_server/         PMM 3 in Docker, admin password
  prometheus_grafana/ Prometheus and Grafana in Docker next to PMM, EC2 service discovery
  pmm_client/         PMM client, registration, monitored services, lock blocking metrics
  postgresql_client/  psql and pgbench on the load generator
  mysql_client/       The MySQL client on the load generator, the admin login in ~/.my.cnf
  mysql_tools/        Tools on the MySQL server: Dolphie
  sysbench/           sysbench tables and a steady sysbench load as a service
  sportsbook/         The sportsbook application: players, odds feed, batch jobs, PMM metrics
  postgresql_tools/   Tools on the database server: pg_activity, pg_top, pgBadger
  liquibase/          Applies a database's SQL migrations from the repo
  labapp/             The simulated application: availability probe, shop workload, PMM dashboard
schema/shop/          The shop database (PostgreSQL): Liquibase migrations, data generation, exercises
schema/sportsbook/    The sportsbook database (MySQL): Liquibase migrations, data generation
tools/labapp/         The shop's simulated application (Python)
tools/sportsbook/     The sportsbook's simulated application (Python)
labs/<lab>/
  terraform/          The lab's Terraform root, with its own state file
  ansible/            The lab's playbook (site.yml) and lab-specific roles
```

## Before your first lab

- A Linux or macOS shell. On Windows, use Ubuntu on WSL and run all lab commands there:
  ```powershell
  wsl --install -d Ubuntu
  ```
  Then, inside Ubuntu, turn on Linux file permissions for the Windows drive, so Ansible and ssh accept files under `/mnt/c`:
  ```bash
  printf '[automount]\noptions = "metadata,umask=22,fmask=11"\n' | sudo tee -a /etc/wsl.conf
  ```
  and restart Ubuntu from PowerShell: `wsl --terminate Ubuntu`.
- An AWS account and the AWS CLI configured with credentials.
- Terraform 1.10 or newer.
- Ansible: `pipx install --include-deps ansible`.
- An EC2 key pair in `us-east-1`, with its private key in `~/.ssh` ([key setup](modules/pmm_server/README.md#prepare-the-ssh-key)).
- An S3 bucket for Terraform state (one-time setup):
  ```bash
  aws s3api create-bucket --bucket <your-bucket> --region us-east-1
  aws s3api put-bucket-versioning --bucket <your-bucket> --versioning-configuration Status=Enabled
  ```
  Then initialize each lab with `terraform init -backend-config="bucket=<your-bucket>"`.

## Cost

Labs are meant to run for a few hours and then be destroyed. Always run `terraform destroy` when you finish: the NAT gateway alone costs about $1 a day if left running. Every resource is tagged with `Lab = <lab name>`, so you can find leftovers and track spend per lab.

## Proposed Architecture
<img width="550" alt="PG_infra" src="https://github.com/user-attachments/assets/f33a2212-26e5-40c1-a6e1-b06265be7d83" />
