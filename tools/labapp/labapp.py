#!/usr/bin/env python3
"""labapp: the lab's simulated application.

Commands:
  labapp probe --config FILE          Run the availability probe (the labapp-probe service).
  labapp outages [--log FILE] [--last N]
                                      List the outages the probe measured.
  labapp workload --config FILE       Run the shop workload (the labapp-workload service), see workload.py.
  labapp load status | rate N | pause | resume | flash-sale [SECONDS] | run JOB
                                      Control the running workload.
  labapp scenario list | run NAME [--seconds N] | status | stop [NAME]
                                      Incidents on demand for DBA drills, see scenarios.py.

The probe checks every target from the application's side:
  write    On a persistent connection, a heartbeat row is written every `interval` seconds.
           A failed write is downtime, also when the server still accepts reads (e.g. a demoted primary).
  connect  Every `connect_interval` seconds, a new connection runs SELECT 1, like a reconnecting application.

An outage starts at the first failed write check and ends when a write check succeeds again, so
durations are accurate to about one `interval` (longer if the server stops answering at all, up to `timeout`).
Metrics are served in Prometheus format for PMM, outages are appended to a JSON-lines log, and each outage
is marked as an annotation on the PMM (Grafana) dashboards.

Probe config (JSON):
  {
    "interval": 0.25, "connect_interval": 1.0, "timeout": 2.0,
    "metrics_address": "0.0.0.0", "metrics_port": 9300,
    "outage_log": "/var/log/labapp/outages.jsonl",
    "targets": [{"name": "postgresql-source", "dsn": "host=... dbname=... user=... password=..."}],
    "grafana": {"url": "https://127.0.0.1/graph", "user": "admin", "password": "...", "verify_tls": false}
  }
"""

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from common import LOG, Annotations, connect, short_error, utc

DEFAULT_OUTAGE_LOG = "/var/log/labapp/outages.jsonl"
DEFAULT_CONTROL_URL = "http://127.0.0.1:9301"

# Latency buckets (seconds): 1 ms .. 5 s.
BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS labapp_probe (
    probe      text PRIMARY KEY,
    seq        bigint NOT NULL,
    updated_at timestamptz NOT NULL
)"""

WRITE_SQL = """
INSERT INTO labapp_probe (probe, seq, updated_at) VALUES (%s, %s, now())
ON CONFLICT (probe) DO UPDATE SET seq = EXCLUDED.seq, updated_at = EXCLUDED.updated_at
RETURNING seq"""


class ProbeMetrics:
    """Prometheus metrics of the probe, scraped by PMM."""

    def __init__(self):
        from prometheus_client import Counter, Gauge, Histogram

        try:
            from prometheus_client import disable_created_metrics

            disable_created_metrics()  # no extra *_created series
        except ImportError:
            pass

        checks = ["target", "check"]
        self.up = Gauge("labapp_probe_up", "1 if the last check succeeded, 0 if it failed", checks)
        self.latency = Histogram(
            "labapp_probe_latency_seconds", "Duration of successful checks", checks, buckets=BUCKETS
        )
        self.checks = Counter("labapp_probe_checks", "Checks run, by result", checks + ["result"])
        self.outages = Counter("labapp_probe_outages", "Completed write outages", ["target"])
        self.downtime = Counter("labapp_probe_downtime_seconds", "Total duration of completed write outages", ["target"])
        self.current = Gauge(
            "labapp_probe_current_outage_seconds", "Duration of the ongoing write outage (0 when up)", ["target"]
        )
        self.last = Gauge("labapp_probe_last_outage_seconds", "Duration of the last completed write outage", ["target"])

    def register(self, target):
        """Create every series at 0, so dashboards show zeros instead of 'no data' before the first outage."""
        for check in ("write", "connect"):
            for result in ("ok", "error"):
                self.checks.labels(target, check, result)
        for metric in (self.outages, self.downtime, self.current, self.last):
            metric.labels(target)


class Target:
    """One database endpoint, checked by a write thread and a connect thread."""

    def __init__(self, name, dsn, cfg, metrics, annotations):
        self.name = name
        self.dsn = dsn
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
                    conn = connect(self.dsn, self.timeout, "labapp-probe", statement_timeout=self.timeout)
                    conn.execute(CREATE_SQL)
                seq += 1
                conn.execute(WRITE_SQL, (self.probe_id, seq)).fetchone()
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

    def connect_loop(self, stop):
        while not stop.is_set():
            tick, started = time.monotonic(), time.time()
            try:
                with connect(self.dsn, self.timeout, "labapp-probe-connect", statement_timeout=self.timeout) as conn:
                    conn.execute("SELECT 1").fetchone()
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
            self._track_outage(ok, when, error)

    def _track_outage(self, ok, when, error):
        # Tagged with the target too: PMM's dashboards show annotations tagged with their service name.
        tags = ["labapp", "outage", self.name]
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
                self._append_outage_log(record)
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

    def _append_outage_log(self, record):
        try:
            with open(self.outage_log, "a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
        except OSError as exc:
            LOG.error("could not write %s: %s", self.outage_log, exc)


# ---- commands ------------------------------------------------------------------------------------


def stop_event():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return stop


def cmd_probe(args):
    from prometheus_client import start_http_server

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    metrics = ProbeMetrics()
    start_http_server(int(cfg.get("metrics_port", 9300)), addr=cfg.get("metrics_address", "0.0.0.0"))
    annotations = Annotations(cfg["grafana"]) if cfg.get("grafana") else None
    stop = stop_event()

    targets = []
    for t in cfg["targets"]:
        target = Target(t["name"], t["dsn"], cfg, metrics, annotations)
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


def cmd_outages(args):
    try:
        with open(args.log, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        records = []
    if not records:
        print("No outages measured yet.")
        return

    first = max(0, len(records) - args.last)
    print(f"{'#':>3}  {'target':<20} {'start (UTC)':<24} {'end (UTC)':<24} {'duration':>9}  first error")
    for number, r in enumerate(records[first:], start=first + 1):
        print(
            f"{number:>3}  {r['target']:<20} {r['start'][:23]:<24} {r['end'][:23]:<24} "
            f"{r['duration_s']:>8.3f}s  {r['error']}"
        )
    total = sum(r["duration_s"] for r in records)
    print(f"\n{len(records)} outages, {total:.3f} s of write downtime in total.")


def cmd_workload(args):
    import workload

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    workload.run(cfg, stop_event())


def cmd_load(args):
    """Talk to the running workload's control endpoint (local only)."""
    path = {
        "status": "/status",
        "rate": f"/rate/{args.value}",
        "pause": "/pause",
        "resume": "/resume",
        "flash-sale": f"/flash-sale/{args.value or ''}".rstrip("/"),
        "run": f"/run/{args.value}",
    }[args.action]
    if args.action in ("rate", "run") and not args.value:
        sys.exit(f"labapp load {args.action} needs a value")
    method = "GET" if args.action == "status" else "POST"
    try:
        with urllib.request.urlopen(urllib.request.Request(args.url + path, method=method), timeout=5) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code}: {exc.read().decode().strip()}")
    except OSError as exc:
        sys.exit(f"The workload is not reachable at {args.url} ({exc}). Is labapp-workload running?")


def cmd_scenario(args):
    """List the scenarios, or start, show and stop them in the running workload (local only)."""
    from scenarios import SCENARIOS, describe, minutes

    if args.action == "list":
        print("\n\n".join(describe(name) for name in SCENARIOS))
        print("\nStart one with: labapp scenario run NAME   (it says again what it switches off)")
        return
    if args.action == "run" and args.name not in SCENARIOS:
        sys.exit(f"labapp scenario run needs one of: {', '.join(SCENARIOS)}")
    if args.action == "stop" and args.name not in (None, *SCENARIOS):
        sys.exit(f"Unknown scenario {args.name}. Scenarios: {', '.join(SCENARIOS)}")
    method, path = {
        "run": ("POST", f"/scenario/run/{args.name}" + (f"/{args.seconds}" if args.seconds else "")),
        "status": ("GET", "/scenarios"),
        "stop": ("POST", "/scenario/stop" + (f"/{args.name}" if args.name else "")),
    }[args.action]
    try:
        with urllib.request.urlopen(urllib.request.Request(args.url + path, method=method), timeout=5) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code}: {exc.read().decode().strip()}")
    except OSError as exc:
        sys.exit(f"The workload is not reachable at {args.url} ({exc}). Is labapp-workload running?")

    if args.action == "run":
        left = reply["ends_by_itself_in_seconds"]
        print(describe(args.name, left))
        print(f"\nStarted. It ends by itself in {minutes(left)}. To end it now: labapp scenario stop {args.name}")
        return
    if args.action == "stop":
        print("Stopping: " + (", ".join(reply["stopping"]) or "nothing was running"))
    running = reply["running"] if args.action == "status" else []
    for run in running:
        print(f"{run['scenario']}: running for {run['running_for_seconds']} s, "
              f"ends by itself in {run['ends_by_itself_in_seconds']} s")
        print(f"  Switches off:  {run['switches_off']}")
    if args.action == "status" and not running:
        print("No scenario is running. See: labapp scenario list")


def main():
    parser = argparse.ArgumentParser(prog="labapp", description="The lab's simulated application.")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="run the availability probe")
    probe.add_argument("--config", required=True, help="JSON config file")
    probe.add_argument("--debug", action="store_true", help="also log failed connect checks")
    probe.set_defaults(func=cmd_probe)

    outages = sub.add_parser("outages", help="list the outages the probe measured")
    outages.add_argument("--log", default=DEFAULT_OUTAGE_LOG, help=f"outage log (default: {DEFAULT_OUTAGE_LOG})")
    outages.add_argument("--last", type=int, default=20, help="show the last N outages (default: 20)")
    outages.set_defaults(func=cmd_outages)

    work = sub.add_parser("workload", help="run the shop workload")
    work.add_argument("--config", required=True, help="JSON config file")
    work.add_argument("--debug", action="store_true", help="log every failed transaction")
    work.set_defaults(func=cmd_workload)

    load = sub.add_parser("load", help="control the running workload")
    load.add_argument("action", choices=["status", "rate", "pause", "resume", "flash-sale", "run"])
    load.add_argument("value", nargs="?", help="rate: requests per second; flash-sale: seconds; run: job name")
    load.add_argument("--url", default=DEFAULT_CONTROL_URL, help=f"control endpoint (default: {DEFAULT_CONTROL_URL})")
    load.set_defaults(func=cmd_load)

    scenario = sub.add_parser("scenario", help="incidents on demand, for DBA drills")
    scenario.add_argument("action", choices=["list", "run", "status", "stop"])
    scenario.add_argument("name", nargs="?", help="scenario name, see: labapp scenario list")
    scenario.add_argument("--seconds", type=int, help="run: end by itself after this many seconds")
    scenario.add_argument("--url", default=DEFAULT_CONTROL_URL, help=f"control endpoint (default: {DEFAULT_CONTROL_URL})")
    scenario.set_defaults(func=cmd_scenario)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    args.func(args)


if __name__ == "__main__":
    main()
