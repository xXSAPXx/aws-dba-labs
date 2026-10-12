"""labapp scenarios: incidents on demand, for DBA drills.

A scenario plays a person or a tool doing something that goes wrong in production: a transaction left open,
a migration started at the wrong moment, a privilege revoked, a replication slot left behind, a table
dropped by mistake. It runs inside the labapp-workload
service, as the database user that person or tool would use, ends by itself after its time limit, and is
marked on the PMM dashboards.

    labapp scenario list | run NAME [--seconds N] [--confirm TABLE] | status | stop [NAME]

Every scenario states which protection it switches off (`switches_off`) and what it leaves behind (`leaves`),
so the staged mistake is never taken for the lab's normal setup. Both are shown by `list` and `run`, logged,
and written into the annotation.

A scenario that destroys data does not undo it. It only starts when it is confirmed with the name of the
table it destroys (`confirm`).
"""

import random
import threading
import time

from common import LOG, connect, short_error


def session_alive(conn):
    """False once the server has closed the connection, e.g. after pg_terminate_backend().

    Sends nothing, so an idle session keeps showing its last statement in pg_stat_activity.
    """
    try:
        conn.pgconn.consume_input()
    except Exception:
        return False
    return not conn.closed and not conn.broken


# ---- the scenarios ---------------------------------------------------------------------------------
# Each takes a Run and returns a short text saying how it ended.


def forgotten_transaction(run):
    """A support engineer fixes customer records in psql and never commits."""
    customers = int(run.settings.get("customers", 50000))
    conn = run.connect("support", "psql")
    conn.execute("BEGIN")
    locked = conn.execute(
        "UPDATE shop.customers SET last_seen_at = last_seen_at WHERE id < %s",
        (run.shop.customer_lo + customers,),
    ).rowcount
    LOG.info("scenario %s: %d customer rows locked, the transaction stays open", run.name, locked)
    while not run.wait(2):
        if not session_alive(conn):
            return f"its session was terminated after {run.age():.0f} s ({locked} rows had been locked)"
    conn.execute("ROLLBACK")
    return f"rolled back after {run.age():.0f} s ({locked} rows had been locked)"


def migration_behind_report(run):
    """A migration's ALTER TABLE waits behind a long report; the application queues behind the ALTER."""
    report = run.connect("analytics", "labapp-bi_extract")

    def run_report():  # the BI extract's own queries, in one transaction, for the scenario's whole time
        try:
            run.jobs.bi_extract(report, {"target": run.seconds})
        except Exception as exc:
            LOG.info("scenario %s: the report ended: %s", run.name, short_error(exc))

    report_thread = threading.Thread(target=run_report, name=f"scenario-{run.name}-report", daemon=True)
    report_thread.start()
    if run.wait(3):  # give the report time to read shop.orders and take its lock
        return "stopped before the migration started"

    migration = run.connect("migrator", "liquibase")
    migration.execute("SET lock_timeout = 0")  # the staged mistake: see switches_off below
    migration.execute("BEGIN")
    outcome = []

    def run_alter():
        try:
            migration.execute("ALTER TABLE shop.orders ADD COLUMN labapp_scenario_column integer")
            outcome.append(None)
        except Exception as exc:
            outcome.append(short_error(exc))

    alter_started = time.time()
    alter_thread = threading.Thread(target=run_alter, name=f"scenario-{run.name}-alter", daemon=True)
    alter_thread.start()
    while alter_thread.is_alive() and not run.wait(1):
        pass
    waited = time.time() - alter_started

    if alter_thread.is_alive():  # time limit or `labapp scenario stop`: withdraw the ALTER
        migration.cancel()
        alter_thread.join(10)
        result = f"the ALTER was withdrawn after waiting {waited:.0f} s"
    elif outcome == [None]:
        result = f"the ALTER got its lock after {waited:.0f} s, when the report ended or was killed"
    else:
        result = f"the migration's session ended after {waited:.0f} s ({outcome[0]})"
    try:
        migration.execute("ROLLBACK")  # the column is never committed
    except Exception:
        pass
    if report_thread.is_alive():
        try:
            report.cancel()
        except Exception:
            pass
        report_thread.join(10)
    return result + "; the schema is unchanged"


ACTS_WITHIN = 0.4  # dropped-table first tries in this part of its time limit


def dropped_table(run):
    """A cleanup script meant for staging is run against production."""
    # The moment is random and is not logged or annotated: finding it is part of the drill. The scenario
    # stays "running" for its whole time limit, so its end on the dashboards does not give the moment away.
    # The first attempt falls in the first part of the time limit (ACTS_WITHIN). The rest is for trying again
    # while a report holds the table: by default 6 minutes, more than the longest report takes (5 minutes).
    retry = float(run.settings.get("retry_seconds", 20))
    if run.wait(random.uniform(0, run.seconds * ACTS_WITHIN)):
        return "stopped before it dropped anything"
    conn = run.connect("migrator", "psql")
    attempts = 0
    while True:
        attempts += 1
        try:
            conn.execute("DROP TABLE shop.orders CASCADE")
            break
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "55P03":  # not a lock timeout: cancelled, session terminated, ...
                return f"nothing was dropped: attempt {attempts} of the DROP was ended ({short_error(exc)})"
            # A long transaction holds the table, and shop_migrator's lock_timeout ended the attempt.
            if run.wait(retry):
                return f"nothing was dropped: shop.orders was held by another transaction at all {attempts} attempts"
    conn.close()
    while not run.wait(5):
        pass
    return "shop.orders IS DROPPED and stays dropped. The way back is a restore, or rebuilding the shop database"


def revoked_privilege(run):
    """A security cleanup revokes a privilege the application still needs."""
    cleanup = run.connect("migrator", "psql")
    cleanup.execute("REVOKE SELECT ON shop.orders FROM shop_app")
    cleanup.close()  # the person who ran it is gone; what stays is the missing privilege
    LOG.info("scenario %s: shop_app may no longer read shop.orders", run.name)

    def granted():
        """True once shop_app may read the table again. Asked in a session that says what it is."""
        nonlocal watch
        if watch is None or not session_alive(watch):
            watch = run.connect("migrator", "labapp-scenario")
        return watch.execute("SELECT has_table_privilege('shop_app', 'shop.orders', 'SELECT')").fetchone()[0]

    watch = None
    while not run.wait(3):
        try:
            if granted():
                return f"the privilege was granted again after {run.age():.0f} s"
        except Exception as exc:  # the watching session was terminated: connect again at the next look
            LOG.info("scenario %s: could not check the privilege: %s", run.name, short_error(exc))
            watch = None
    undo = run.connect("migrator", "labapp-scenario")
    undo.execute("GRANT SELECT ON shop.orders TO shop_app")
    return f"the scenario granted the privilege again after {run.age():.0f} s"


SLOT = "debezium"  # the name Debezium gives its replication slot by default


def abandoned_slot(run):
    """The change data capture connector stops reading. Its replication slot stays and holds the WAL."""
    connector = run.connect("cdc", "Debezium Streaming")
    while True:
        try:
            connector.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (SLOT,))
            break
        except Exception as exc:
            state = getattr(exc, "sqlstate", None)
            if state == "42710":  # the slot is already there, from an earlier run
                break
            if state != "55P03":
                raise
            # A new logical slot waits for the transactions that are writing right now. Behind a long one
            # (a batch job, a forgotten transaction) the server-wide lock_timeout ends the wait: try again.
            if run.wait(10):
                return "stopped before the slot was created"
    connector.close()  # the connector is gone: from now on nothing reads the slot
    LOG.info("scenario %s: replication slot %s created, nothing reads it", run.name, SLOT)

    def held_mb():
        """MB of WAL the slot holds back, or None once the slot is gone."""
        nonlocal watch
        if watch is None or not session_alive(watch):
            watch = run.connect("cdc", "labapp-scenario")
        row = watch.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) FROM pg_replication_slots WHERE slot_name = %s",
            (SLOT,),
        ).fetchone()
        return None if row is None else float(row[0] or 0) / 1048576

    watch, held = None, 0.0
    while not run.wait(5):
        try:
            now = held_mb()
        except Exception as exc:  # the watching session was terminated: connect again at the next look
            LOG.info("scenario %s: could not look at the slot: %s", run.name, short_error(exc))
            watch = None
            continue
        if now is None:
            return f"the slot was dropped after {run.age():.0f} s; it held {held:.0f} MB of WAL"
        held = now
    undo = run.connect("cdc", "labapp-scenario")
    undo.execute("SELECT pg_drop_replication_slot(%s)", (SLOT,))
    return f"the scenario dropped the slot after {run.age():.0f} s; it held {held:.0f} MB of WAL"


SCENARIOS = {
    "forgotten-transaction": {
        "summary": "A support engineer updates customers in psql and leaves the transaction open.",
        "runs_as": "shop_support, application psql",
        "does": [
            "BEGIN; UPDATE shop.customers for the most active customers (50,000 by default); then nothing: no COMMIT.",
            "The session sits idle in its transaction and keeps the row locks.",
            "Logins of those customers wait for the locks and fail after shop_app's lock_timeout of 2 s.",
        ],
        "switches_off": (
            "Nothing. shop_support has no idle_in_transaction_session_timeout, "
            "so nothing ends this transaction but you, or the scenario's time limit."
        ),
        "leaves": "Nothing. The transaction is rolled back.",
        "seconds": 600,
        "connections": ["support"],
        "run": forgotten_transaction,
    },
    "migration-behind-report": {
        "summary": "A migration's ALTER TABLE waits behind a long report, and the application queues behind the ALTER.",
        "runs_as": "shop_migrator, application liquibase; the report is shop_analytics, application labapp-bi_extract",
        "does": [
            "A BI extract reads shop.orders in one transaction and keeps running.",
            "ALTER TABLE shop.orders ADD COLUMN ... needs the table to itself, so it waits for the report.",
            "Every statement on shop.orders queues behind the waiting ALTER: checkouts, payments, shipping, order history.",
        ],
        "switches_off": (
            "SET lock_timeout = 0 in the migration's session. shop_migrator's own lock_timeout is 5 s: "
            "with it, the ALTER gives up after 5 s and the application recovers by itself."
        ),
        "leaves": "Nothing. The ALTER runs in a transaction that is rolled back: the schema never changes.",
        "seconds": 300,
        "connections": ["analytics", "migrator"],
        "run": migration_behind_report,
    },
    "revoked-privilege": {
        "summary": "A security cleanup revokes a privilege the application still needs: REVOKE SELECT ON shop.orders FROM shop_app.",
        "runs_as": "shop_migrator, application psql",
        "does": [
            "REVOKE SELECT ON shop.orders FROM shop_app, run as the owner of the table. Then that session leaves.",
            "Every statement of the application that reads orders fails: permission denied for table orders.",
            "That includes UPDATE ... WHERE: checkouts, payments, shipping and order history stop working.",
            "The REVOKE is in the audit log, with its time, user, application and client address.",
        ],
        "switches_off": "Nothing. The owner of a table may revoke any privilege on it.",
        "leaves": "Nothing. At the time limit the scenario grants the privilege again, unless you already did.",
        "seconds": 600,
        "connections": ["migrator"],
        "run": revoked_privilege,
    },
    "abandoned-slot": {
        "summary": "The change data capture connector stops and leaves its replication slot behind. Nothing reads it, and it holds the WAL.",
        "runs_as": "debezium, application Debezium Streaming",
        "does": [
            "Creates the logical replication slot \"debezium\" in the shop database, as the connector does when it is set up.",
            "Then the connector is gone, as if Kafka were down: nothing reads the slot.",
            "PostgreSQL keeps all WAL the slot has not read: about 0.7 GB per hour at the lab's normal load. Nothing fails.",
            "pg_wal on disk only grows once that is more than the 2 GB PostgreSQL keeps anyway (max_wal_size).",
            "The slot is in pg_replication_slots: not active, its restart_lsn standing still. PMM collects it too.",
            "No audit line: a slot is created by a function call, which pgAudit does not log.",
        ],
        "switches_off": "Nothing. max_slot_wal_keep_size is unlimited by default: nothing stops a slot from holding WAL.",
        "leaves": (
            "Nothing. At the time limit the scenario drops the slot, unless you already did. "
            "PostgreSQL removes the WAL it held at the next checkpoint."
        ),
        "seconds": 1800,
        "connections": ["cdc"],
        "run": abandoned_slot,
    },
    "dropped-table": {
        "summary": "A cleanup script meant for staging is run against production: DROP TABLE shop.orders CASCADE.",
        "runs_as": "shop_migrator, application psql",
        "does": [
            "At a random moment: DROP TABLE shop.orders CASCADE. When exactly is for you to find out.",
            "CASCADE also removes the foreign keys that seven other tables have to orders.",
            "Checkouts, payments, shipping, order history and the reports fail: the table does not exist.",
            "If a report holds the table, the DROP gives up after shop_migrator's lock_timeout of 5 s and tries again "
            "every 20 s until the time limit. Each attempt holds up the statements on orders for those 5 s.",
            "Cancel the DROP or terminate its session while it waits, and nothing is dropped.",
        ],
        "switches_off": "Nothing. Whoever acts as the owner of a table may drop it. The only protection is a backup.",
        "leaves": (
            "THE TABLE STAYS DROPPED. Neither the time limit nor `labapp scenario stop` brings it back. "
            "The way back is a restore. The lab has no backups yet: until it does, rebuild the shop database "
            "(lab README, \"Reset the shop database\", about 8 minutes)."
        ),
        "seconds": 600,
        "acts_within": ACTS_WITHIN,
        "connections": ["migrator"],
        "confirm": "orders",  # the scenario only starts with: --confirm orders
        "run": dropped_table,
    },
}


def minutes(seconds):
    return f"{round(seconds / 60, 1):g} min" if seconds >= 60 else f"{seconds} s"


def describe(name, seconds=None):
    """What a scenario does, as text for the command line."""
    spec = SCENARIOS[name]
    seconds = seconds or spec["seconds"]
    limit = minutes(seconds)
    header = f"ends by itself after {limit}"
    if "acts_within" in spec:
        header = f"acts at a random moment within {minutes(round(seconds * spec['acts_within']))}, ends after {limit}"
    lines = [
        f"{name}  ({header})",
        f"  {spec['summary']}",
        f"  Runs as:       {spec['runs_as']}",
    ]
    lines += [f"  {'What happens:' if i == 0 else '':<14} - {step}" for i, step in enumerate(spec["does"])]
    lines.append(f"  Switches off:  {spec['switches_off']}")
    lines.append(f"  Leaves behind: {spec['leaves']}")
    if spec.get("confirm"):
        lines.append(f"  To run it:     labapp scenario run {name} --confirm {spec['confirm']}")
    return "\n".join(lines)


# ---- running them ----------------------------------------------------------------------------------


class Run:
    """One running scenario: its settings, its connections, and when it has to end."""

    def __init__(self, name, seconds, settings, scenarios):
        self.name = name
        self.seconds = seconds
        self.settings = settings
        self.scenarios = scenarios
        self.shop = scenarios.shop
        self.jobs = scenarios.jobs
        self.started = time.time()
        self.ends = self.started + seconds
        self.stop = threading.Event()  # set by `labapp scenario stop`
        self.connections = []

    def connect(self, role, application_name):
        conn = connect(self.scenarios.dsn[role], self.scenarios.timeout, application_name)
        self.connections.append(conn)
        return conn

    def age(self):
        return time.time() - self.started

    def wait(self, seconds):
        """Wait up to `seconds`. True when the scenario has to end: stopped, time limit, or the service stops."""
        self.stop.wait(max(0.0, min(seconds, self.ends - time.time())))
        return self.stop.is_set() or self.scenarios.service_stop.is_set() or time.time() >= self.ends


class Scenarios:
    def __init__(self, cfg, shop, jobs, metrics, annotations, stop):
        self.dsn = cfg["dsn"]
        self.timeout = float(cfg.get("timeout", 2.0))
        self.settings = cfg.get("scenarios", {})
        self.shop = shop
        self.jobs = jobs
        self.metrics = metrics
        self.annotations = annotations
        self.service_stop = stop
        self.lock = threading.Lock()
        self.running = {}  # name -> Run
        for name in SCENARIOS:
            metrics.scenario_active.labels(name).set(0)

    def start(self, name, seconds=None, confirm=None):
        """Start a scenario in its own thread. Returns (HTTP status, body)."""
        spec = SCENARIOS.get(name)
        if spec is None:
            return 404, {"error": f"unknown scenario {name}", "scenarios": sorted(SCENARIOS)}
        if spec.get("confirm") and confirm != spec["confirm"]:
            return 409, {"error": f"{name} destroys data and does not undo it. "
                                  f"To run it: labapp scenario run {name} --confirm {spec['confirm']}"}
        missing = [role for role in spec["connections"] if role not in self.dsn]
        if missing:
            return 409, {"error": f"{name} needs a connection for {', '.join(missing)} in the workload configuration (dsn)"}
        settings = self.settings.get(name.replace("-", "_"), {})
        seconds = int(seconds or settings.get("seconds", spec["seconds"]))
        with self.lock:
            if name in self.running:
                return 409, {"error": f"{name} is already running"}
            run = self.running[name] = Run(name, seconds, settings, self)
        threading.Thread(target=self._run, args=(run,), name=f"scenario-{name}", daemon=True).start()
        return 200, self._state(run)

    def stop(self, name=None):
        """Ask one scenario, or all, to end now. Returns the names asked."""
        with self.lock:
            runs = [run for run in self.running.values() if name in (None, run.name)]
        for run in runs:
            run.stop.set()
        return [run.name for run in runs]

    def status(self):
        with self.lock:
            return [self._state(run) for run in self.running.values()]

    @staticmethod
    def _state(run):
        spec = SCENARIOS[run.name]
        return {
            "scenario": run.name,
            "running_for_seconds": round(run.age()),
            "ends_by_itself_in_seconds": max(0, round(run.ends - time.time())),
            "switches_off": spec["switches_off"],
            "leaves": spec["leaves"],
        }

    def _run(self, run):
        spec = SCENARIOS[run.name]
        tags = ["labapp", "workload", "scenario", run.name]
        self.metrics.scenario_active.labels(run.name).set(1)
        LOG.info("scenario %s started, ends by itself after %d s. Switches off: %s Leaves behind: %s",
                 run.name, run.seconds, spec["switches_off"], spec["leaves"])
        if self.annotations:
            self.annotations.add((run.name, run.started), run.started,
                                 f"Scenario {run.name} started. Switches off: {spec['switches_off']}", tags)
        try:
            outcome = spec["run"](run)
        except Exception as exc:
            outcome = f"failed: {short_error(exc)}"
        for conn in run.connections:
            try:
                conn.close()
            except Exception:
                pass
        LOG.info("scenario %s: %s", run.name, outcome)
        self.metrics.scenario_active.labels(run.name).set(0)
        if self.annotations:
            self.annotations.finish((run.name, run.started), run.started, time.time(),
                                    f"Scenario {run.name}: {outcome}. Switched off: {spec['switches_off']}", tags)
        with self.lock:
            del self.running[run.name]
