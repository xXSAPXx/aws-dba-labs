"""Shared helpers of labapp: connections, error messages, Grafana annotations."""

import base64
import json
import logging
import ssl
import threading
import urllib.request
from datetime import datetime, timezone
from queue import Queue

LOG = logging.getLogger("labapp")


def utc(ts):
    """Unix time -> ISO 8601 UTC with milliseconds."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def short_error(exc):
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first_line}"[:200]


def connect(dsn, timeout, application_name, statement_timeout=None):
    """Connection with bounded waits for connecting and for dead TCP peers.

    statement_timeout (seconds): only when given; otherwise the role's own settings apply (like a real application).
    """
    import psycopg

    options = {}
    if statement_timeout is not None:
        options["options"] = f"-c statement_timeout={int(statement_timeout * 1000)}"
    return psycopg.connect(
        dsn,
        autocommit=True,
        application_name=application_name,
        connect_timeout=max(2, round(timeout)),  # libpq treats values below 2 s as 2 s
        keepalives=1,
        keepalives_idle=5,
        keepalives_interval=1,
        keepalives_count=3,
        tcp_user_timeout=int(timeout * 1000),
        **options,
    )


class Annotations:
    """Marks events (outages, flash sales, batch jobs) on the lab's Grafana dashboards.

    Runs in its own thread, so it never delays the caller. A key links the start of an event to its end:
    add() creates a point annotation, finish() turns it into a time range (or creates the range).
    """

    def __init__(self, cfg):
        self.url = cfg["url"].rstrip("/") + "/api/annotations"
        token = base64.b64encode(f'{cfg["user"]}:{cfg["password"]}'.encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.ssl = ssl.create_default_context()
        if not cfg.get("verify_tls", True):
            self.ssl.check_hostname = False
            self.ssl.verify_mode = ssl.CERT_NONE
        self.queue = Queue()
        self.ids = {}  # key -> annotation id
        threading.Thread(target=self._run, name="annotations", daemon=True).start()

    def add(self, key, start, text, tags):
        self.queue.put(("add", key, start, None, text, tags))

    def finish(self, key, start, end, text, tags):
        self.queue.put(("finish", key, start, end, text, tags))

    def _request(self, method, path, body):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), headers=self.headers, method=method
        )
        with urllib.request.urlopen(request, timeout=5, context=self.ssl) as response:
            return json.load(response)

    def _run(self):
        while True:
            kind, key, start, end, text, tags = self.queue.get()
            try:
                if kind == "add":
                    body = {"time": int(start * 1000), "tags": tags, "text": text}
                    self.ids[key] = self._request("POST", "", body).get("id")
                else:
                    body = {"time": int(start * 1000), "timeEnd": int(end * 1000), "tags": tags, "text": text}
                    annotation_id = self.ids.pop(key, None)
                    if annotation_id:
                        self._request("PATCH", f"/{annotation_id}", body)
                    else:
                        self._request("POST", "", body)
            except Exception as exc:  # Grafana down or busy: measuring goes on regardless
                LOG.warning("could not write the Grafana annotation: %s", short_error(exc))
