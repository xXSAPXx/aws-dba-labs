"""labapp scenarios: incidents on demand, for DBA drills.

A scenario plays a person or a tool doing something that goes wrong in production: a transaction left open,
a migration started at the wrong moment. It runs inside the labapp-workload service, as the database user that
person or tool would use, ends by itself after its time limit, and is marked on the PMM dashboards.

    labapp scenario list | run NAME [--seconds N] | status | stop [NAME]

Every scenario states which protection it switches off (`switches_off`), so the staged mistake is never taken
for the lab's normal setup. That text is shown by `list` and `run`, logged, and written into the annotation.
"""

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
            "The ALTER runs in a transaction that is rolled back: the schema never changes.",
        ],
        "switches_off": (
            "SET lock_timeout = 0 in the migration's session. shop_migrator's own lock_timeout is 5 s: "
            "with it, the ALTER gives up after 5 s and the application recovers by itself."
        ),
        "seconds": 300,
        "connections": ["analytics", "migrator"],
        "run": migration_behind_report,
    },
}


def minutes(seconds):
    return f"{seconds / 60:g} min" if seconds >= 60 else f"{seconds} s"


def describe(name, seconds=None):
    """What a scenario does, as text for the command line."""
    spec = SCENARIOS[name]
    lines = [
        f"{name}  (ends by itself after {minutes(seconds or spec['seconds'])})",
        f"  {spec['summary']}",
        f"  Runs as:       {spec['runs_as']}",
    ]
    lines += [f"  {'What happens:' if i == 0 else '':<14} - {step}" for i, step in enumerate(spec["does"])]
    lines.append(f"  Switches off:  {spec['switches_off']}")
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

    def start(self, name, seconds=None):
        """Start a scenario in its own thread. Returns (HTTP status, body)."""
        spec = SCENARIOS.get(name)
        if spec is None:
            return 404, {"error": f"unknown scenario {name}", "scenarios": sorted(SCENARIOS)}
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
        return {
            "scenario": run.name,
            "running_for_seconds": round(run.age()),
            "ends_by_itself_in_seconds": max(0, round(run.ends - time.time())),
            "switches_off": SCENARIOS[run.name]["switches_off"],
        }

    def _run(self, run):
        spec = SCENARIOS[run.name]
        tags = ["labapp", "workload", "scenario", run.name]
        self.metrics.scenario_active.labels(run.name).set(1)
        LOG.info("scenario %s started, ends by itself after %d s. Switches off: %s",
                 run.name, run.seconds, spec["switches_off"])
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
