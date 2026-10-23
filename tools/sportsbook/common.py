"""Shared helpers of the sportsbook application: MySQL connections and transactions, error kinds,
Grafana annotations."""

import base64
import json
import logging
import ssl
import threading
import urllib.request
from contextlib import contextmanager
from queue import Queue

LOG = logging.getLogger("sportsbook")

# MySQL error numbers -> the result labels of the metrics.
ERROR_KINDS = {
    1205: "lock_timeout",          # waited longer than innodb_lock_wait_timeout for a row lock
    1213: "deadlock",              # InnoDB rolled the transaction back to break a deadlock
    3024: "query_timeout",         # a SELECT ran longer than max_execution_time
    1317: "killed",                # KILL QUERY
    1040: "too_many_connections",  # max_connections
    1203: "too_many_connections",  # max_user_connections
    1226: "too_many_connections",  # the account's own MAX_USER_CONNECTIONS
    1044: "denied",
    1045: "denied",
    1142: "denied",                # a privilege is missing (e.g. revoked from the role)
    1143: "denied",
    1146: "missing_table",
    3819: "check_violation",       # a CHECK constraint, e.g. a wallet balance below zero
}
# Lost or refused connections, the server shutting down, KILL of the connection: the application reconnects.
CONNECTION_ERRORS = {1053, 1927, 2003, 2006, 2013, 2055, 4031}


def classify(exc):
    import pymysql

    if isinstance(exc, (pymysql.err.InterfaceError, ConnectionError, TimeoutError)):
        return "connection"
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code in ERROR_KINDS:
        return ERROR_KINDS[code]
    if code in CONNECTION_ERRORS:
        return "connection"
    return "other"


def short_error(exc):
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first_line}"[:200]


def connect(db, program, session=None, timeout=5.0, read_timeout=30.0):
    """A MySQL connection in autocommit: a statement outside an explicit transaction commits at once.

    db: {"host", "port", "user", "password", "database"}. program: the name MySQL keeps for the connection
    (performance_schema.session_connect_attrs, program_name). session: SET SESSION values on every
    (re)connect, e.g. {"innodb_lock_wait_timeout": 3}. read_timeout: seconds to wait for an answer, None: forever.
    """
    import pymysql

    init = None
    if session:
        init = "SET SESSION " + ", ".join(f"{name} = {value}" for name, value in session.items())
    return pymysql.connect(
        host=db["host"],
        port=int(db.get("port", 3306)),
        user=db["user"],
        password=db["password"],
        database=db.get("database", "sportsbook"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=timeout,
        read_timeout=read_timeout,
        write_timeout=timeout * 6,
        init_command=init,
        program_name=program,
    )


@contextmanager
def transaction(conn):
    """BEGIN ... COMMIT, or ROLLBACK on any exception (also a business outcome). Yields a cursor."""
    conn.begin()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass  # the connection is gone: the server rolled back already
        raise


class Annotations:
    """Marks events (kick-offs, settlement runs, reports) on the PMM (Grafana) dashboards.

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
            except Exception as exc:  # PMM down or busy: the traffic goes on regardless
                LOG.warning("could not write the Grafana annotation: %s", short_error(exc))
