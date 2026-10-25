# How the Ansible part works

Terraform builds the servers. Ansible installs and configures everything on them. This page explains how the Ansible files fit together, using the [postgresql-single](../labs/postgresql-single/) lab as the example. You don't need to know Ansible to run a lab, only to change one.

## The entry point: site.yml

Each lab has one playbook, `labs/<lab>/ansible/site.yml`. It says which roles to apply to which servers, in order:

```yaml
- hosts: all                 # 1. every server
  roles: [common]

- hosts: all                 # 2. both servers at the same time, each with its own roles
  strategy: free
  roles:
    - { role: liquibase,         when: "'pmm_server' in group_names" }   # installed, not run yet
    - { role: pmm_server,        when: "'pmm_server' in group_names" }
    - { role: postgresql_client, when: "'pmm_server' in group_names" }
    - { role: data_volume,       when: "'postgresql' in group_names" }
    - { role: postgresql,        when: "'postgresql' in group_names" }
    - { role: postgresql_tools,  when: "'postgresql' in group_names" }

- hosts: pmm_server          # 3. the database is configured: start its migrations in the background
  tasks:
    - import_role: { name: liquibase, tasks_from: start.yml }

- hosts: postgresql          # 4. the PostgreSQL server registers with PMM, which is now running
  roles: [pmm_client]

- hosts: pmm_server          # 5. the application side: collect the migrations, start the application
  tasks:
    - import_role: { name: liquibase, tasks_from: finish.yml }
    - import_role: { name: labapp }
```

A **role** is a folder of related steps, like one chapter of a runbook. Ansible connects to each server over SSH and runs the steps from top to bottom.

Normally Ansible runs each step on every server of a play and waits for all of them before the next step. Step 2 switches that off with `strategy: free`: the two servers don't need each other for their own installation, so each works through its own roles without waiting for the other. Steps 3 to 5 are separate because they need both servers to be ready.

The migrations start only in step 3, after the database server is configured: a restart for a changed setting in step 2 would cut a running update off. On a new lab they also generate the data, which takes minutes, so they run in the background during step 4 and step 5 waits for them.

A variable that one play's role defines by default is not known in another play. A value that two plays need, such as the monitoring user's name, therefore belongs in the lab's `group_vars`.

## How Ansible finds the servers

- **`inventory.ini`** is written by `terraform apply`. It lists the servers in groups, and `hosts:` in `site.yml` refers to these group names:
  ```ini
  [pmm_server]
  pmm-server private_ip=10.0.0.5

  [postgresql]
  postgresql-source private_ip=10.0.0.37
  ```
  Ansible connects by those names. The SSH config that Terraform also writes (`~/.ssh/aws-postgresql-lab.conf`) supplies the IP, user, key and jump host.
- **`ansible.cfg`** tells Ansible where the inventory and the roles are. That's why you run `ansible-playbook` from the lab's `ansible/` folder.

## The roles

| Role | Lives in | What it does |
|---|---|---|
| `common` | `ansible/roles/` | Hostname, `/etc/hosts` entries for every lab server, EPEL, admin tools; turns off the Cockpit web console the Rocky images come with (port 9090) |
| `data_volume` | `ansible/roles/` | Formats and mounts the database server's EBS volumes: the data volume, and extra ones such as a backup disk (before the database is installed) |
| `percona_release` | `ansible/roles/` | Installs `percona-release`, which manages the Percona repositories |
| `pmm_server` | `ansible/roles/` | Docker, the PMM 3 container, the PMM admin password |
| `prometheus_grafana` | `ansible/roles/` | Prometheus and Grafana in Docker Compose next to PMM: Prometheus finds the lab's servers by their tags (EC2 service discovery) and scrapes their node_exporter, Grafana has it as its data source |
| `node_exporter` | `ansible/roles/` | node_exporter from EPEL on the database server, with its textfile collector in `/var/lib/node_exporter/textfile_collector`, where the backup scripts write their metrics |
| `pmm_client` | `ansible/roles/` | PMM client, registers the server with PMM, adds the databases to monitor; for PostgreSQL, [custom queries and a dashboard](roles/pmm_client/README.md) that record who blocks whom |
| `postgresql_tools` | `ansible/roles/` | Tools on the database server. Live: pg_activity (overview of sessions, waits, blocking) and pg_top (per-PID query, EXPLAIN, locks). From the server log: pgBadger (report of slow statements, lock waits, errors) |
| `postgresql_client` | `ansible/roles/` | psql and pgbench on the load generator, database passwords in `~/.pgpass` |
| `liquibase` | `ansible/roles/` | Java, Liquibase and the lab's JDBC drivers on the PMM server (PostgreSQL by default, MySQL Connector/J in the MySQL lab); copies a changelog from the repo (e.g. `schema/shop`, `schema/sportsbook`) and applies the pending migrations; `liquibase-<db>` wrapper command |
| `labapp` | `ansible/roles/` | The lab's simulated application ([tools/labapp](../tools/labapp/README.md)): the availability probe and the shop workload as services, their metrics in PMM, the "Lab: Application" dashboard |
| `postgresql` | `labs/postgresql-single/ansible/roles/` | Percona PostgreSQL 17, its configuration, pg_stat_monitor, the `pmm` monitoring user |
| `mysql_client` | `ansible/roles/` | The MySQL client on the load generator, the admin login in `~/.my.cnf` |
| `mysql_tools` | `ansible/roles/` | Tools on the MySQL server. Live: Dolphie (a real-time dashboard: processlist, transactions and locks, InnoDB, replication, metric graphs) |
| `sysbench` | `ansible/roles/` | sysbench on the load generator: creates its tables once, then a steady load at a fixed rate as the `sysbench-workload` service |
| `sportsbook` | `ansible/roles/` | The sportsbook application ([tools/sportsbook](../tools/sportsbook/README.md)) as the `sportsbook-workload` service, its metrics in PMM |
| `mysql` | `labs/mysql-single/ansible/roles/` | Percona Server for MySQL 8.4, its configuration (`/etc/my.cnf.d/lab.cnf`), the root password, users and databases; XtraBackup, MySQL Shell, Percona Toolkit and the AWS CLI for the backups |

Roles in `ansible/roles/` are shared by every lab, like the Terraform modules in `modules/`. A role that only one lab needs lives in that lab's own `ansible/roles/`.

## Inside a role

Each folder in a role has one job. The `postgresql` role:

```
roles/postgresql/
  tasks/main.yml        The steps. The only folder every role has.
  defaults/main.yml     The settings: PostgreSQL version, packages, postgresql.conf values.
  templates/            Config files with placeholders: pg_hba.conf.j2, 01-lab.conf.j2
  handlers/main.yml     "Restart PostgreSQL" / "Reload PostgreSQL", run only when needed
  meta/main.yml         Dependencies: run percona_release before this role
```

**Tasks** describe the state you want, not the command to run:

```yaml
- name: Install PostgreSQL, pgBackRest, pg_repack and pg_stat_monitor
  ansible.builtin.dnf:
    name: "{{ postgresql_packages }}"   # the list comes from defaults/main.yml
    state: present                      # "make sure it's installed"
```

If the packages are already installed, the task does nothing and reports `ok`. Otherwise it installs them and reports `changed`.

**Templates** are config files with blanks that Ansible fills in. In `pg_hba.conf.j2`, the line

```
host    all    all    {{ postgresql_hba_vpc_cidr }}    scram-sha-256
```

becomes `host all all 10.0.0.0/24 scram-sha-256` on the server.

**Handlers** run only if the task that notifies them actually changed something:

```yaml
- name: Write the lab settings              # writes conf.d/01-lab.conf
  ansible.builtin.template: ...
  notify: Restart PostgreSQL                # restarts only if the file changed
```

Run the playbook again with nothing changed, and PostgreSQL is not restarted.

## Where the values come from

| File | What's in it | Applies to |
|---|---|---|
| `inventory.ini` (written by Terraform) | Private IPs, `vpc_cidr_block` | Each server / all servers |
| `group_vars/all.yml` | Passwords, generated on the first run into `credentials/` | All servers |
| `group_vars/postgresql.yml` | What PMM should monitor | Servers in the `[postgresql]` group |
| `roles/<role>/defaults/main.yml` | The role's settings | Wherever the role runs |

If the same variable is set in two places, `group_vars` wins over a role's defaults. For example, to turn on query plans in PMM, set `pg_stat_monitor.pgsm_enable_query_plan: true` in the `postgresql` role defaults (or override `postgresql_settings` in `group_vars/postgresql.yml`), then run the playbook again.

## Running it again is safe

Every task checks the current state first, so running `ansible-playbook site.yml` again changes only what differs from the expected configuration. That makes it the reset button for break/fix: it restores packages, configuration files, users, services and the PMM registration. It does not restore data you deleted.

## Useful commands

Run these from the lab's `ansible/` folder:

```bash
ansible-playbook site.yml --list-tasks          # every step, in order, without running anything
ansible-playbook site.yml --check --diff        # dry run: show what would change
ansible-playbook site.yml -l postgresql-source  # run on one server only
```

`--list-tasks` is the best place to start: it prints every step, grouped by play and role.
