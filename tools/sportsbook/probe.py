"""sportsbook probe: the database's availability and latency, as the application sees it (the sportsbook-probe service).

Two checks per target, each in its own thread:
  write    On a persistent connection, a heartbeat row is written every `interval` seconds. A failed write is
           downtime, also when the server still answers reads (read-only, a full disk, a commit that hangs).
  connect  Every `connect_interval` seconds, a new connection runs SELECT 1, like a reconnecting application.

An outage starts at the first failed write and ends at the next successful one, so durations are accurate to about
one `interval` (longer when the server stops answering at all: up to `timeout`). Each outage goes to a JSON-lines
log (`sportsbook outages` lists them), to the metrics, and to Grafana as an annotation.
The MySQL counterpart of labapp's probe (tools/labapp/labapp.py).
"""

import json
import socket
import threading
import time

from common import LOG, Annotations, connect, short_error, utc

DEFAULT_OUTAGE_LOG = "/var/log/sportsbook/outages.jsonl"

# Latency buckets (seconds): 1 ms .. 5 s.
BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# Created only when the write finds no table (error 1146), so a reconnect runs no DDL (and adds no line to the
# audit log, which logs DDL).
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS heartbeat (
    probe      VARCHAR(100) NOT NULL PRIMARY KEY,
    seq        BIGINT NOT NULL,
    updated_at DATETIME(6) NOT NULL
)"""

WRITE_SQL = """
INSERT INTO heartbeat (probe, seq, updated_at) VALUES (%s, %s, NOW(6)) AS new
ON DUPLICATE KEY UPDATE seq = new.seq, updated_at = new.updated_at"""


class ProbeMetrics:
    def __init__(self):
        from prometheus_client import Counter, Gauge, Histogram

        try:
            from prometheus_client import disable_created_metrics

            disable_created_metrics()  # no extra *_created series
        except ImportError:
            pass

        checks = ["target", "check"]
        self.up = Gauge("sportsbook_probe_up", "1 if the last check succeeded, 0 if it failed", checks)
        self.latency = Histogram(
            "sportsbook_probe_latency_seconds", "Duration of successful checks", checks, buckets=BUCKETS
        )
        self.checks = Counter("sportsbook_probe_checks", "Checks run, by result", checks + ["result"])
        self.outages = Counter("sportsbook_probe_outages", "Completed write outages", ["target"])
        self.downtime = Counter(
            "sportsbook_probe_downtime_seconds", "Total duration of completed write outages", ["target"]
        )
        self.current = Gauge(
            "sportsbook_probe_current_outage_seconds", "Duration of the ongoing write outage (0 when up)", ["target"]
        )
        self.last = Gauge(
            "sportsbook_probe_last_outage_seconds", "Duration of the last completed write outage", ["target"]
        )

    def register(self, target):
        """Create every series at 0, so dashboards show zeros instead of 'no data' before the first outage."""
        for check in ("write", "connect"):
            for result in ("ok", "error"):
                self.checks.labels(target, check, result)
        for metric in (self.outages, self.downtime, self.current, self.last):
            metric.labels(target)


class Target:
    """One MySQL server, checked by a write thread and a connect thread."""

    def __init__(self, name, db, cfg, metrics, annotations):
        self.name = name
        self.db = db
        self.interval = float(cfg.get("interval", 0.25))
        self.connect_interval = float(cfg.get("connect_interval", 1.0))
        self.timeout = float(cfg.get("timeout", 2.0))
        self.outage_log = cfg.get("outage_log", DEFAULT_OUTAGE_LOG)
        self.metrics = metrics
        self.annotations = annotations
        self.probe_id = f"{socket.gethostname()}/{name}"
        self.lock = threading.Lock()
        self.outage_start = None
        self.outage_error = None
        self.failed_checks = 0
        self.last_ok = None
        metrics.register(name)

    # ---- checks ---------------------------------------------------------------------------------

    def write_loop(self, stop):
        conn, seq = None, 0
        while not stop.is_set():
            tick, started = time.monotonic(), time.time()
            try:
                if conn is None:
                    conn = connect(self.db, "sportsbook-probe", None, self.timeout, self.timeout)
                seq += 1
                self.write(conn, seq)
                self.check_done("write", True, time.monotonic() - tick, time.time())
            except Exception as exc:
                self.check_done("write", False, None, started, short_error(exc))
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
            stop.wait(max(0.0, self.interval - (time.monotonic() - tick)))

    def write(self, conn, seq):
        with conn.cursor() as cur:
            try:
                cur.execute(WRITE_SQL, (self.probe_id, seq))
            except Exception as exc:
                if not (exc.args and exc.args[0] == 1146):  # 1146: no such table, on the first run
                    raise
                cur.execute(CREATE_SQL)
                cur.execute(WRITE_SQL, (self.probe_id, seq))

    def connect_loop(self, stop):
        while not stop.is_set():
            tick, started = time.monotonic(), time.time()
            try:
                with connect(self.db, "sportsbook-probe-connect", None, self.timeout, self.timeout) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                self.check_done("connect", True, time.monotonic() - tick, time.time())
            except Exception as exc:
                self.check_done("connect", False, None, started, short_error(exc))
            stop.wait(max(0.0, self.connect_interval - (time.monotonic() - tick)))

    # ---- results --------------------------------------------------------------------------------

    def check_done(self, check, ok, duration, when, error=None):
        """`when`: completion time of a successful check, start time of a failed one."""
        self.metrics.checks.labels(self.name, check, "ok" if ok else "error").inc()
        self.metrics.up.labels(self.name, check).set(1 if ok else 0)
        if ok:
            self.metrics.latency.labels(self.name, check).observe(duration)
        elif check == "connect":
            LOG.debug("%s: connect check failed: %s", self.name, error)
        if check == "write":
            self.track_outage(ok, when, error)

    def track_outage(self, ok, when, error):
        tags = ["sportsbook", "outage", self.name]
        with self.lock:
            if not ok:
                if self.outage_start is None:
                    self.outage_start, self.outage_error, self.failed_checks = when, error, 0
                    LOG.warning("%s: write outage started at %s: %s", self.name, utc(when), error)
                    if self.annotations:
                        self.annotations.add(
                            (self.name, when), when, f"Write outage started on {self.name}: {error}", tags
                        )
                self.failed_checks += 1
                self.metrics.current.labels(self.name).set(time.time() - self.outage_start)
                return

            if self.outage_start is not None:
                duration = when - self.outage_start
                record = {
                    "target": self.name,
                    "start": utc(self.outage_start),
                    "end": utc(when),
                    "duration_s": round(duration, 3),
                    "last_ok": utc(self.last_ok) if self.last_ok else None,
                    "failed_checks": self.failed_checks,
                    "error": self.outage_error,
                }
                LOG.warning(
                    "%s: write outage ended after %.3f s (%d failed checks): %s",
                    self.name, duration, self.failed_checks, self.outage_error,
                )
                self.append_outage_log(record)
                self.metrics.outages.labels(self.name).inc()
                self.metrics.downtime.labels(self.name).inc(duration)
                self.metrics.last.labels(self.name).set(duration)
                if self.annotations:
                    self.annotations.finish(
                        (self.name, self.outage_start), self.outage_start, when,
                        f"Write outage on {self.name}: {duration:.1f} s ({self.outage_error})", tags,
                    )
                self.outage_start = None
            self.last_ok = when
            self.metrics.current.labels(self.name).set(0)

    def append_outage_log(self, record):
        try:
            with open(self.outage_log, "a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
        except OSError as exc:
            LOG.error("could not write %s: %s", self.outage_log, exc)


# ---- entry points ---------------------------------------------------------------------------------


def run(cfg, stop):
    from prometheus_client import start_http_server

    metrics = ProbeMetrics()
    start_http_server(int(cfg.get("metrics_port", 9313)), addr=cfg.get("metrics_address", "0.0.0.0"))
    annotations = Annotations(cfg["grafana"]) if cfg.get("grafana") else None

    targets = []
    for t in cfg["targets"]:
        target = Target(t["name"], t["db"], cfg, metrics, annotations)
        targets.append(target)
        for check, loop in (("write", target.write_loop), ("connect", target.connect_loop)):
            threading.Thread(target=loop, args=(stop,), name=f"{t['name']}-{check}", daemon=True).start()
        LOG.info(
            "probing %s: write every %.2f s, connect every %.2f s, timeout %.1f s",
            target.name, target.interval, target.connect_interval, target.timeout,
        )

    while not stop.is_set():
        stop.wait(1)
    for target in targets:
        if target.outage_start is not None:
            LOG.warning("%s: probe stopped during an outage that started at %s", target.name, utc(target.outage_start))
    LOG.info("stopped")


def list_outages(path, last):
    try:
        with open(path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        records = []
    if not records:
        print("No outages measured yet.")
        return

    first = max(0, len(records) - last)
    print(f"{'#':>3}  {'target':<20} {'start (UTC)':<24} {'end (UTC)':<24} {'duration':>9}  first error")
    for number, r in enumerate(records[first:], start=first + 1):
        print(
            f"{number:>3}  {r['target']:<20} {r['start'][:23]:<24} {r['end'][:23]:<24} "
            f"{r['duration_s']:>8.3f}s  {r['error']}"
        )
    total = sum(r["duration_s"] for r in records)
    print(f"\n{len(records)} outages, {total:.3f} s of write downtime in total.")
