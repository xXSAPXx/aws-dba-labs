#!/usr/bin/env python3
"""sportsbook: a sportsbook's production traffic on MySQL.

Commands:
  sportsbook workload --config FILE   Run the traffic (the sportsbook-workload service), see workload.py.
  sportsbook load status | rate N | pause | resume | kickoff [MATCHES] | settlement-mode MODE | run JOB
                                      Control the running workload.

Workload config (JSON):
  {
    "rate": 80, "workers": 32, "timeout": 5,
    "metrics_address": "0.0.0.0", "metrics_port": 9312, "control_port": 9311,
    "db": {"app": {"host": "mysql-source", "user": "sb_app", "password": "...", "database": "sportsbook"},
           "feed": {...}, "batch": {...}, "reporting": {...}},
    "session": {"app": {"innodb_lock_wait_timeout": 3, "max_execution_time": 5000}, ...},
    "mix": {}, "surge_mix": {}, "wave": {"period": 3600, "amplitude": 0.4}, "surge": {"multiplier": 3, "duration": 120},
    "feed": {"kickoff_every": 600, "kickoff_first": 60, "kickoff_matches": 6, "match_seconds": 900, ...},
    "jobs": {"settlement": {"every": 30, "first": 30, "mode": "single_transaction"}, ...},
    "grafana": {"url": "https://127.0.0.1/graph", "user": "admin", "password": "...", "verify_tls": false}
  }
"""

import argparse
import json
import logging
import signal
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_CONTROL_URL = "http://127.0.0.1:9311"


def stop_event():
    """Set by SIGTERM (systemctl stop) or Ctrl+C."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def cmd_workload(args):
    import workload

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    workload.run(cfg, stop_event())


def cmd_load(args):
    """Talk to the running workload's control endpoint (local only)."""
    if args.action in ("rate", "settlement-mode", "run") and not args.value:
        sys.exit(f"sportsbook load {args.action} needs a value")
    path = "/" + args.action + (f"/{args.value}" if args.value else "")
    method = "GET" if args.action == "status" else "POST"
    try:
        with urllib.request.urlopen(urllib.request.Request(args.url + path, method=method), timeout=5) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code}: {exc.read().decode().strip()}")
    except OSError as exc:
        sys.exit(f"The workload is not reachable at {args.url} ({exc}). Is sportsbook-workload running?")


def main():
    parser = argparse.ArgumentParser(prog="sportsbook", description="A sportsbook's production traffic on MySQL.")
    sub = parser.add_subparsers(dest="command", required=True)

    work = sub.add_parser("workload", help="run the traffic")
    work.add_argument("--config", required=True, help="JSON config file")
    work.add_argument("--debug", action="store_true", help="log every failed request")
    work.set_defaults(func=cmd_workload)

    load = sub.add_parser("load", help="control the running workload")
    load.add_argument("action", choices=["status", "rate", "pause", "resume", "kickoff", "settlement-mode", "run"])
    load.add_argument("value", nargs="?", help="rate: requests per second; kickoff: matches to kick off now; "
                      "settlement-mode: single_transaction or batched; run: job name")
    load.add_argument("--url", default=DEFAULT_CONTROL_URL, help=f"control endpoint (default: {DEFAULT_CONTROL_URL})")
    load.set_defaults(func=cmd_load)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    args.func(args)


if __name__ == "__main__":
    main()
