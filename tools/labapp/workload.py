"""labapp workload: the shop's application traffic (the labapp-workload service).

Open loop, like real users: requests arrive at the target rate whether or not the database keeps up. When it
slows down, requests queue for a free worker and the response time includes that wait. Workers (one connection
each, as shop_app) run them as business transactions. Long-running reports and batch jobs run on their own
schedules with their own roles (shop_reporting, shop_analytics, shop_batch).

Target rate = base rate x daily wave (one cycle per `wave.period`) x flash sale multiplier (0 while paused).
Control it live with `labapp load ...` (a local HTTP endpoint, 127.0.0.1 only). Incidents on demand, for
DBA drills, are in scenarios.py (`labapp scenario ...`).
"""

import json
import math
import queue
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg.types.json import Jsonb

from common import LOG, Annotations, connect, short_error
from scenarios import SCENARIOS, Scenarios

# Response time buckets (seconds): 5 ms .. 30 s.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# PostgreSQL error codes -> result labels. Connection errors (no code, 08xxx, 57P0x) are "connection".
ERROR_KINDS = {
    "55P03": "lock_timeout",
    "57014": "canceled",  # statement_timeout, or pg_cancel_backend()
    "40P01": "deadlock",
    "40001": "serialization",
    "25P03": "idle_in_tx_timeout",
}

PAYMENT_METHODS = ["card", "card", "card", "paypal", "bank_transfer", "gift_card"]
FIRST_NAMES = ["Anna", "Ben", "Clara", "David", "Elena", "Felix", "Greta", "Hugo", "Ivana", "Jonas"]
LAST_NAMES = ["Petrov", "Schmidt", "Garcia", "Rossi", "Novak", "Jansen", "Dubois", "Kowalski", "Silva", "Larsen"]


class OutOfStock(Exception):
    """A business outcome, not an error: the checkout found no stock and rolled back."""


def classify(exc):
    state = getattr(exc, "sqlstate", None)
    if state in ERROR_KINDS:
        return ERROR_KINDS[state]
    if state is None or state.startswith("08") or state in ("57P01", "57P02", "57P03"):
        return "connection"  # includes sessions killed with pg_terminate_backend()
    return "other"


# ---- metrics -------------------------------------------------------------------------------------


class Metrics:
    def __init__(self):
        from prometheus_client import Counter, Gauge, Histogram

        try:
            from prometheus_client import disable_created_metrics

            disable_created_metrics()
        except ImportError:
            pass
        self.tx = Counter("labapp_tx", "Shop transactions by type and result", ["tx", "result"])
        self.latency = Histogram(
            "labapp_tx_latency_seconds", "Response time as users see it: queue wait + transaction",
            ["tx"], buckets=LATENCY_BUCKETS,
        )
        self.queue_depth = Gauge("labapp_tx_queue_depth", "Requests waiting for a free worker")
        self.base_rate = Gauge("labapp_load_base_rate", "Base request rate (requests per second)")
        self.target_rate = Gauge("labapp_load_target_rate", "Current target rate: base x wave x flash sale")
        self.flash_sale = Gauge("labapp_load_flash_sale", "1 during a flash sale")
        self.paused = Gauge("labapp_load_paused", "1 while the workload is paused")
        # The label is job_name, not job: Prometheus keeps "job" for the scrape and would rename ours to exported_job.
        self.job_active = Gauge("labapp_job_active", "1 while a long-running report or batch job runs", ["job_name"])
        self.job_runs = Counter("labapp_job_runs", "Report and batch job runs by result", ["job_name", "result"])
        self.job_duration = Gauge("labapp_job_last_duration_seconds", "Duration of the last run", ["job_name"])
        self.scenario_active = Gauge("labapp_scenario_active", "1 while a scenario (labapp scenario run) runs", ["scenario"])


# ---- shop data -----------------------------------------------------------------------------------


class Shop:
    """Ids the transactions pick from, loaded once at start. Popularity is skewed like the generated data."""

    def load(self, conn):
        one = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
        self.product_lo, self.product_n = one("SELECT min(id), count(*) FROM shop.products")
        self.customer_lo, self.customer_n = one(
            "SELECT min(id), count(*) FROM shop.customers WHERE email LIKE 'customer%@example.com'"
        )
        self.categories = [r[0] for r in conn.execute("SELECT id FROM shop.categories WHERE parent_id IS NOT NULL")]
        self.warehouses = [r[0] for r in conn.execute("SELECT id FROM shop.warehouses")]
        self.carriers = [r[0] for r in conn.execute("SELECT id FROM shop.carriers")]
        self.countries = [r[0] for r in conn.execute("SELECT code FROM shop.countries")]
        self.hot_products = 20

    def product(self, flash=False):
        if flash and random.random() < 0.7:  # a flash sale concentrates traffic on a few products
            return self.product_lo + random.randrange(self.hot_products)
        return self.product_lo + int(self.product_n * random.random() ** 3)

    def customer(self):
        return self.customer_lo + int(self.customer_n * random.random() ** 1.5)


# ---- the shop's transactions (as shop_app) --------------------------------------------------------


def tx_product_page(conn, shop, flash):
    pid = shop.product(flash)
    with conn.transaction():
        conn.execute(
            """SELECT p.id, p.sku, p.name, p.price, p.description, c.name, b.name
               FROM shop.products p
               JOIN shop.categories c ON c.id = p.category_id
               JOIN shop.brands b ON b.id = p.brand_id
               WHERE p.id = %s""", (pid,)).fetchone()
        conn.execute("SELECT name, value FROM shop.product_attributes WHERE product_id = %s", (pid,)).fetchall()
        conn.execute(
            """SELECT rating, title, created_at FROM shop.product_reviews
               WHERE product_id = %s ORDER BY created_at DESC LIMIT 5""", (pid,)).fetchall()
        conn.execute(
            "SELECT coalesce(sum(quantity - reserved), 0) FROM shop.inventory WHERE product_id = %s", (pid,)
        ).fetchone()


def tx_category(conn, shop, flash):
    conn.execute(
        """SELECT id, name, price FROM shop.products
           WHERE category_id = %s AND active ORDER BY price, id LIMIT 20 OFFSET %s""",
        (random.choice(shop.categories), random.choice([0, 0, 0, 20, 40])),
    ).fetchall()


def tx_login(conn, shop, flash):
    n = shop.customer() - shop.customer_lo + 1
    with conn.transaction():
        row = conn.execute(
            "SELECT id, status FROM shop.customers WHERE email = %s", (f"customer{n}@example.com",)
        ).fetchone()
        if row:
            conn.execute("UPDATE shop.customers SET last_seen_at = now() WHERE id = %s", (row[0],))
            conn.execute(
                "INSERT INTO shop.audit_log (actor, action, entity, entity_id) VALUES (%s, 'login', 'customer', %s)",
                (f"customer:{row[0]}", row[0]),
            )


def tx_cart(conn, shop, flash):
    customer = shop.customer() if random.random() < 0.8 else None  # 20% guests
    with conn.transaction():
        cart = None
        if customer is not None:
            cart = conn.execute(
                """SELECT id FROM shop.carts WHERE customer_id = %s AND status = 'open'
                   ORDER BY updated_at DESC LIMIT 1""", (customer,)).fetchone()
        cart_id = cart[0] if cart else conn.execute(
            "INSERT INTO shop.carts (customer_id) VALUES (%s) RETURNING id", (customer,)).fetchone()[0]
        conn.execute(
            """INSERT INTO shop.cart_items AS ci (cart_id, product_id, quantity) VALUES (%s, %s, %s)
               ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = ci.quantity + EXCLUDED.quantity""",
            (cart_id, shop.product(flash), random.randint(1, 3)),
        )
        conn.execute("UPDATE shop.carts SET updated_at = now() WHERE id = %s", (cart_id,))


def tx_checkout(conn, shop, flash):
    customer = shop.customer()
    items = {}
    for _ in range(random.choice([1, 1, 2, 2, 3])):
        items[shop.product(flash)] = random.randint(1, 2)
    warehouse = random.choice(shop.warehouses)
    with conn.transaction():
        prices = dict(conn.execute("SELECT id, price FROM shop.products WHERE id = ANY(%s)", (list(items),)).fetchall())
        # Stock rows are locked in the order the customer added the products (not sorted), like many real
        # applications: two checkouts of the same products in a different order can deadlock.
        for pid, qty in items.items():
            if conn.execute(
                """UPDATE shop.inventory SET quantity = quantity - %s, updated_at = now()
                   WHERE warehouse_id = %s AND product_id = %s AND quantity >= %s RETURNING quantity""",
                (qty, warehouse, pid, qty),
            ).fetchone() is None:
                raise OutOfStock()
        total = sum(prices[p] * q for p, q in items.items())
        order_id = conn.execute(
            """INSERT INTO shop.orders (customer_id, shipping_address_id, status, total_amount)
               VALUES (%s, (SELECT id FROM shop.customer_addresses
                            WHERE customer_id = %s AND kind = 'shipping' ORDER BY id LIMIT 1), 'pending', %s)
               RETURNING id""", (customer, customer, total)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO shop.order_items (order_id, product_id, quantity, unit_price) VALUES (%s, %s, %s, %s)",
                [(order_id, p, q, prices[p]) for p, q in items.items()],
            )
            cur.executemany(
                """INSERT INTO shop.stock_movements (warehouse_id, product_id, quantity, reason, order_id)
                   VALUES (%s, %s, %s, 'sale', %s)""",
                [(warehouse, p, -q, order_id) for p, q in items.items()],
            )
        conn.execute(
            """INSERT INTO shop.payments (order_id, amount, method, status, provider_ref)
               VALUES (%s, %s, %s, 'pending', %s)""",
            (order_id, total, random.choice(PAYMENT_METHODS), f"PSP-{uuid.uuid4().hex[:16]}"),
        )
        conn.execute(
            "INSERT INTO shop.order_status_history (order_id, from_status, to_status) VALUES (%s, NULL, 'pending')",
            (order_id,),
        )
        conn.execute(
            "UPDATE shop.carts SET status = 'converted', updated_at = now() WHERE customer_id = %s AND status = 'open'",
            (customer,),
        )
        conn.execute(
            """INSERT INTO shop.audit_log (actor, action, entity, entity_id, details)
               VALUES (%s, 'checkout', 'order', %s, %s)""",
            (f"customer:{customer}", order_id, Jsonb({"items": len(items), "total": str(total)})),
        )


def tx_payment(conn, shop, flash):
    with conn.transaction():
        row = conn.execute(
            """SELECT id, order_id FROM shop.payments WHERE status = 'pending'
               ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
        if row is None:
            return
        payment_id, order_id = row
        if random.random() < 0.03:  # declined by the payment provider
            conn.execute("UPDATE shop.payments SET status = 'failed', updated_at = now() WHERE id = %s", (payment_id,))
            conn.execute("UPDATE shop.orders SET status = 'cancelled', updated_at = now() WHERE id = %s", (order_id,))
            conn.execute(
                "INSERT INTO shop.order_status_history (order_id, from_status, to_status) VALUES (%s, 'pending', 'cancelled')",
                (order_id,),
            )
            return
        conn.execute("UPDATE shop.payments SET status = 'captured', updated_at = now() WHERE id = %s", (payment_id,))
        conn.execute(
            "UPDATE shop.orders SET status = 'paid', updated_at = now() WHERE id = %s AND status = 'pending'", (order_id,)
        )
        conn.execute(
            "INSERT INTO shop.order_status_history (order_id, from_status, to_status) VALUES (%s, 'pending', 'paid')",
            (order_id,),
        )
        conn.execute(
            """INSERT INTO shop.invoices (order_id, number) VALUES (%s, 'INV-' || to_char(now(), 'YYYY') || '-' || lpad(%s::text, 9, '0'))
               ON CONFLICT (order_id) DO NOTHING""", (order_id, order_id))


def tx_shipping(conn, shop, flash):
    with conn.transaction():
        if random.random() < 0.5:  # ship the oldest paid order
            row = conn.execute(
                """SELECT id FROM shop.orders WHERE status = 'paid'
                   ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row is None:
                return
            order_id = row[0]
            conn.execute("UPDATE shop.orders SET status = 'shipped', updated_at = now() WHERE id = %s", (order_id,))
            conn.execute(
                "INSERT INTO shop.order_status_history (order_id, from_status, to_status) VALUES (%s, 'paid', 'shipped')",
                (order_id,),
            )
            shipment_id = conn.execute(
                """INSERT INTO shop.shipments (order_id, warehouse_id, carrier_id, tracking_number, status, shipped_at)
                   VALUES (%s, %s, %s, %s, 'in_transit', now()) RETURNING id""",
                (order_id, random.choice(shop.warehouses), random.choice(shop.carriers), f"TRK{uuid.uuid4().hex[:12]}"),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO shop.shipment_events (shipment_id, event, location)
                   VALUES (%s, 'label_created', 'warehouse'), (%s, 'picked_up', 'warehouse')""",
                (shipment_id, shipment_id),
            )
        else:  # deliver the oldest shipment in transit
            row = conn.execute(
                """SELECT id, order_id FROM shop.shipments
                   WHERE status = 'in_transit' AND shipped_at < now() - interval '2 minutes'
                   ORDER BY shipped_at LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
            if row is None:
                return
            shipment_id, order_id = row
            conn.execute(
                "UPDATE shop.shipments SET status = 'delivered', delivered_at = now() WHERE id = %s", (shipment_id,)
            )
            conn.execute("UPDATE shop.orders SET status = 'delivered', updated_at = now() WHERE id = %s", (order_id,))
            conn.execute(
                "INSERT INTO shop.order_status_history (order_id, from_status, to_status) VALUES (%s, 'shipped', 'delivered')",
                (order_id,),
            )
            conn.execute(
                "INSERT INTO shop.shipment_events (shipment_id, event, location) VALUES (%s, 'delivered', 'customer')",
                (shipment_id,),
            )


def tx_order_history(conn, shop, flash):
    conn.execute(
        """SELECT o.id, o.status, o.total_amount, o.created_at, count(oi.id) AS items
           FROM shop.orders o LEFT JOIN shop.order_items oi ON oi.order_id = o.id
           WHERE o.customer_id = %s
           GROUP BY o.id ORDER BY o.created_at DESC LIMIT 10""", (shop.customer(),)).fetchall()


def tx_signup(conn, shop, flash):
    country = random.choice(shop.countries)
    with conn.transaction():
        customer = conn.execute(
            """INSERT INTO shop.customers (email, first_name, last_name, country_code)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (f"signup-{uuid.uuid4().hex[:16]}@example.com", random.choice(FIRST_NAMES), random.choice(LAST_NAMES), country),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO shop.customer_addresses (customer_id, kind, line1, city, postal_code, country_code, is_default)
               VALUES (%s, 'shipping', %s, 'Sofia', '1000', %s, true)""",
            (customer, f"{random.randint(1, 250)} Main Street", country),
        )


TRANSACTIONS = {
    "product_page": tx_product_page,
    "category": tx_category,
    "login": tx_login,
    "cart": tx_cart,
    "checkout": tx_checkout,
    "payment": tx_payment,
    "shipping": tx_shipping,
    "order_history": tx_order_history,
    "signup": tx_signup,
}

DEFAULT_MIX = {
    "product_page": 33, "category": 14, "login": 10, "cart": 15, "checkout": 8,
    "payment": 8, "shipping": 5, "order_history": 6, "signup": 1,
}


# ---- long-running reports and batch jobs ------------------------------------------------------------
# Each runs in its own thread with its own connection. Reports run real SQL and, when the data is too
# small to take the target time, top up with pg_sleep inside the same statement or transaction, so the
# snapshot and the locks are held for the whole duration. The padding adapts to the measured query time.


def padded(sql):
    """Wrap a report query so the statement takes at least the padding (%(pad)s seconds) longer."""
    return f"WITH report AS MATERIALIZED ({sql}), pad AS MATERIALIZED (SELECT pg_sleep(%(pad)s)) SELECT report.* FROM report, pad"


SALES_DASHBOARD_SQL = """
SELECT s.day, s.category_id, s.revenue, s.orders, rank() OVER (PARTITION BY s.day ORDER BY s.revenue DESC) AS rank
FROM (
    SELECT date_trunc('day', o.created_at) AS day, c.parent_id AS category_id,
           sum(oi.quantity * oi.unit_price) AS revenue, count(DISTINCT o.id) AS orders
    FROM shop.orders o
    JOIN shop.order_items oi ON oi.order_id = o.id
    JOIN shop.products p ON p.id = oi.product_id
    JOIN shop.categories c ON c.id = p.category_id
    WHERE o.created_at >= now() - interval '90 days' AND o.status <> 'cancelled'
    GROUP BY 1, 2
) s"""

LIFETIME_VALUE_SQL = """
SELECT c.id, c.email, c.country_code, count(o.id) AS orders,
       coalesce(sum(o.total_amount), 0) AS lifetime_value, max(o.created_at) AS last_order_at
FROM shop.customers c
LEFT JOIN shop.orders o ON o.customer_id = c.id AND o.status <> 'cancelled'
GROUP BY c.id
ORDER BY lifetime_value DESC"""

BI_EXTRACT_STEPS = [
    """SELECT date_trunc('month', created_at) AS month, status, count(*) AS orders, sum(total_amount) AS revenue
       FROM shop.orders GROUP BY 1, 2""",
    """SELECT product_id, sum(quantity) AS units, sum(quantity * unit_price) AS revenue
       FROM shop.order_items GROUP BY 1 ORDER BY 3 DESC LIMIT 1000""",
    """SELECT carrier_id, avg(delivered_at - shipped_at) AS avg_delivery,
              percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM delivered_at - shipped_at)) AS p95_seconds
       FROM shop.shipments WHERE delivered_at IS NOT NULL GROUP BY 1""",
    """SELECT warehouse_id, reason, count(*) AS movements, sum(quantity) AS units
       FROM shop.stock_movements GROUP BY 1, 2""",
    """SELECT date_trunc('day', created_at) AS day, event, count(*) AS events
       FROM shop.shipment_events WHERE created_at > now() - interval '30 days' GROUP BY 1, 2""",
]

STOCK_RECOUNT_SQL = """
SELECT warehouse_id, product_id, sum(quantity) AS ledger_quantity
FROM shop.stock_movements GROUP BY 1, 2"""


class Jobs:
    def __init__(self, cfg, shop, metrics, annotations, stop):
        self.cfg = cfg
        self.shop = shop
        self.metrics = metrics
        self.annotations = annotations
        self.stop = stop
        self.timeout = float(cfg.get("timeout", 2.0))
        self.dsn = cfg["dsn"]
        self.lock = threading.Lock()
        self.active = {}  # job -> start time
        self.estimate = {}  # (job, step) -> measured query seconds, for the padding
        self.run_fn = {
            "sales_dashboard": ("reporting", self.sales_dashboard),
            "lifetime_value_export": ("analytics", self.lifetime_value_export),
            "bi_extract": ("analytics", self.bi_extract),
            "cart_cleanup": ("batch", self.cart_cleanup),
            "stock_recount": ("batch", self.stock_recount),
            "restock": ("batch", self.restock),
        }
        self.annotated = {"bi_extract", "cart_cleanup", "stock_recount"}
        for job in self.run_fn:
            for result in ("ok", "error"):
                metrics.job_runs.labels(job, result)
            metrics.job_active.labels(job).set(0)

    def start(self, job):
        """Start a job in its own thread, unless it is already running. Returns False if it was."""
        with self.lock:
            if job in self.active:
                return False
            self.active[job] = time.time()
        threading.Thread(target=self._run, args=(job,), name=f"job-{job}", daemon=True).start()
        return True

    def _run(self, job):
        role, fn = self.run_fn[job]
        started = self.active[job]
        settings = self.cfg["jobs"].get(job, {})
        self.metrics.job_active.labels(job).set(1)
        tags = ["labapp", "workload", job]
        if job in self.annotated and self.annotations:
            self.annotations.add((job, started), started, f"{job} started", tags)
        result, detail = "ok", ""
        try:
            with connect(self.dsn[role], self.timeout, f"labapp-{job}") as conn:
                detail = fn(conn, settings) or ""
        except Exception as exc:
            result, detail = "error", short_error(exc)
        duration = time.time() - started
        LOG.info("%s: %s after %.1f s %s", job, result, duration, detail)
        self.metrics.job_runs.labels(job, result).inc()
        self.metrics.job_duration.labels(job).set(duration)
        self.metrics.job_active.labels(job).set(0)
        if job in self.annotated and self.annotations:
            self.annotations.finish((job, started), started, time.time(), f"{job}: {result} after {duration:.0f} s {detail}", tags)
        with self.lock:
            del self.active[job]

    def _padded_query(self, conn, key, sql, target, params=None):
        """Run a report query padded to about `target` seconds; learn its real duration for the next run."""
        pad = max(0.0, target - self.estimate.get(key, 0.0))
        begin = time.monotonic()
        conn.execute(padded(sql), {**(params or {}), "pad": pad}).fetchall()
        self.estimate[key] = max(0.0, time.monotonic() - begin - pad)

    # ---- reports --------------------------------------------------------------------------------

    def sales_dashboard(self, conn, settings):
        self._padded_query(conn, ("sales_dashboard", 0), SALES_DASHBOARD_SQL, float(settings.get("target", 30)))

    def lifetime_value_export(self, conn, settings):
        """A slow client: the export is read through a cursor in batches, with the 'CSV writing' in between."""
        target, batch = float(settings.get("target", 60)), 5000
        batches = max(1, math.ceil(self.shop.customer_n / batch))
        rows = 0
        with conn.transaction():
            conn.execute("DECLARE lifetime_value NO SCROLL CURSOR FOR " + LIFETIME_VALUE_SQL)
            while not self.stop.is_set():
                begin = time.monotonic()
                fetched = conn.execute(f"FETCH {batch} FROM lifetime_value").fetchall()
                if not fetched:
                    break
                rows += len(fetched)
                time.sleep(max(0.0, target / batches - (time.monotonic() - begin)))
        return f"({rows} rows)"

    def bi_extract(self, conn, settings):
        """One REPEATABLE READ transaction: the same snapshot for every step, held for the whole extract."""
        target = float(settings.get("target", 300))
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            for step, sql in enumerate(BI_EXTRACT_STEPS):
                if self.stop.is_set():
                    break
                self._padded_query(conn, ("bi_extract", step), sql, target / len(BI_EXTRACT_STEPS))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- batch jobs -----------------------------------------------------------------------------

    def cart_cleanup(self, conn, settings):
        with conn.transaction():
            abandoned = conn.execute(
                """UPDATE shop.carts SET status = 'abandoned', updated_at = now()
                   WHERE status = 'open' AND updated_at < now() - interval '30 minutes'""").rowcount
        with conn.transaction():
            deleted = conn.execute(
                """DELETE FROM shop.carts
                   WHERE status IN ('abandoned', 'converted') AND updated_at < now() - interval '45 minutes'""").rowcount
        return f"({abandoned} carts abandoned, {deleted} deleted)"

    def stock_recount(self, conn, settings):
        """Recount the stock against the ledger.

        mode single_transaction (default, the bad way): every stock row stays locked for the whole recount, so
        checkouts wait and fail with lock_timeout. mode batched: small transactions, locks held only briefly.
        """
        target = float(settings.get("target", 90))
        if settings.get("mode", "single_transaction") == "batched":
            self._padded_query(conn, ("stock_recount", 0), STOCK_RECOUNT_SQL, 0)
            chunks = [(w, lo) for w in self.shop.warehouses
                      for lo in range(self.shop.product_lo, self.shop.product_lo + self.shop.product_n, 1000)]
            for warehouse, lo in chunks:
                if self.stop.is_set():
                    break
                with conn.transaction():
                    conn.execute(
                        """UPDATE shop.inventory SET updated_at = now()
                           WHERE warehouse_id = %s AND product_id >= %s AND product_id < %s""",
                        (warehouse, lo, lo + 1000))
                time.sleep(target / len(chunks))
            return "(batched)"
        conn.execute("BEGIN")
        try:
            locked = conn.execute("UPDATE shop.inventory SET updated_at = now()").rowcount  # locks every stock row
            self._padded_query(conn, ("stock_recount", 0), STOCK_RECOUNT_SQL, target)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return f"(single transaction, {locked} rows locked)"

    def restock(self, conn, settings):
        with conn.transaction():
            rows = conn.execute(
                """UPDATE shop.inventory SET quantity = quantity + 500, updated_at = now()
                   WHERE quantity < 50 RETURNING warehouse_id, product_id""").fetchall()
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO shop.stock_movements (warehouse_id, product_id, quantity, reason)
                       VALUES (%s, %s, 500, 'purchase')""", rows)
        return f"({len(rows)} stock rows refilled)"


# ---- the load generator ---------------------------------------------------------------------------


class Load:
    def __init__(self, cfg, shop, metrics, jobs, annotations, stop):
        self.cfg = cfg
        self.shop = shop
        self.metrics = metrics
        self.jobs = jobs
        self.annotations = annotations
        self.stop = stop
        self.timeout = float(cfg.get("timeout", 2.0))
        self.base_rate = float(cfg.get("rate", 30))
        self.paused = False
        self.flash_until = 0.0
        self.max_wait = float(cfg.get("max_wait", 30))
        self.requests = queue.Queue(maxsize=int(cfg.get("queue_limit", 5000)))
        mix = cfg.get("mix") or DEFAULT_MIX
        self.tx_names, self.tx_weights = list(mix), list(mix.values())
        self.wave = cfg.get("wave", {"period": 3600, "amplitude": 0.4})
        self.flash = cfg.get("flash_sale", {})
        self.started = time.monotonic()
        for name in TRANSACTIONS:
            for result in ("ok", "out_of_stock", "lock_timeout", "canceled", "deadlock", "connection", "other",
                           "expired", "dropped"):
                metrics.tx.labels(name, result)

    def target_rate(self):
        if self.paused:
            return 0.0
        elapsed = time.monotonic() - self.started
        wave = 1 + float(self.wave.get("amplitude", 0)) * math.sin(2 * math.pi * elapsed / float(self.wave.get("period", 3600)))
        flash = float(self.flash.get("multiplier", 3)) if self.flash_active() else 1.0
        return self.base_rate * wave * flash

    def flash_active(self):
        return time.time() < self.flash_until

    def start_flash_sale(self, seconds):
        start = time.time()
        self.flash_until = start + seconds
        LOG.info("flash sale for %d s", seconds)
        if self.annotations:
            self.annotations.finish(("flash", start), start, start + seconds, f"Flash sale: {seconds} s", ["labapp", "workload", "flash_sale"])

    def arrivals(self):
        """Poisson arrivals at the target rate: they don't wait for the database, like real users."""
        next_at = time.monotonic()
        while not self.stop.is_set():
            rate = self.target_rate()
            self.metrics.target_rate.set(rate)
            if rate <= 0:
                self.stop.wait(0.2)
                next_at = time.monotonic()
                continue
            next_at += random.expovariate(rate)
            delay = next_at - time.monotonic()
            if delay > 0:
                self.stop.wait(delay)
            elif delay < -1:  # far behind (e.g. after a pause): don't burst to catch up
                next_at = time.monotonic()
            tx = random.choices(self.tx_names, self.tx_weights)[0]
            try:
                self.requests.put_nowait((tx, time.monotonic()))
            except queue.Full:
                self.metrics.tx.labels(tx, "dropped").inc()

    def worker(self):
        conn = None
        while not self.stop.is_set():
            try:
                tx, queued_at = self.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if time.monotonic() - queued_at > self.max_wait:  # the user gave up waiting
                self.metrics.tx.labels(tx, "expired").inc()
                continue
            result = "ok"
            for attempt in (1, 2):
                try:
                    if conn is None or conn.closed:
                        conn = connect(self.cfg["dsn"]["app"], self.timeout, "labapp-shop")
                    TRANSACTIONS[tx](conn, self.shop, self.flash_active())
                    result = "ok"
                    break
                except OutOfStock:
                    result = "out_of_stock"
                    break
                except Exception as exc:
                    result = classify(exc)
                    LOG.debug("%s: %s", tx, short_error(exc))
                    if result == "connection" and conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                    if result in ("deadlock", "serialization") and attempt == 1:
                        continue  # one retry, like most applications
                    break
            self.metrics.tx.labels(tx, result).inc()
            self.metrics.latency.labels(tx).observe(time.monotonic() - queued_at)

    def scheduler(self):
        """Starts reports, batch jobs and flash sales on their schedules; updates the gauges."""
        start = time.monotonic()
        next_run = {job: start + float(s.get("first", s.get("every", 600))) for job, s in self.cfg["jobs"].items()}
        next_flash = start + float(self.flash.get("first", 1200)) if self.flash.get("every") else None
        while not self.stop.is_set():
            now = time.monotonic()
            for job, settings in self.cfg["jobs"].items():
                if settings.get("every") and now >= next_run[job]:
                    self.jobs.start(job)  # skipped while the previous run is still going
                    next_run[job] = now + float(settings["every"])
            if next_flash is not None and now >= next_flash:
                self.start_flash_sale(int(self.flash.get("duration", 180)))
                next_flash = now + float(self.flash["every"])
            self.metrics.queue_depth.set(self.requests.qsize())
            self.metrics.base_rate.set(self.base_rate)
            self.metrics.flash_sale.set(1 if self.flash_active() else 0)
            self.metrics.paused.set(1 if self.paused else 0)
            self.stop.wait(1)

    def status(self):
        return {
            "base_rate": self.base_rate,
            "target_rate": round(self.target_rate(), 1),
            "paused": self.paused,
            "flash_sale_seconds_left": max(0, round(self.flash_until - time.time())),
            "queued_requests": self.requests.qsize(),
            "running_jobs": sorted(self.jobs.active),
            "jobs": sorted(self.jobs.run_fn),
        }


# ---- control endpoint (127.0.0.1 only) ------------------------------------------------------------


def control_server(load, scenarios, port):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/status":
                self._reply(200, {**load.status(), "running_scenarios": [s["scenario"] for s in scenarios.status()]})
            elif self.path == "/scenarios":
                self._reply(200, {"running": scenarios.status(), "scenarios": sorted(SCENARIOS)})
            else:
                self._reply(404, {"error": "unknown path"})

        def _scenario(self, action, name, seconds):
            if action == "run" and name:
                return self._reply(*scenarios.start(name, int(seconds) if seconds else None))
            if action == "stop":
                return self._reply(200, {"stopping": scenarios.stop(name), "running": scenarios.status()})
            return self._reply(400, {"error": "use: scenario/run/NAME[/SECONDS] | scenario/stop[/NAME]",
                                     "scenarios": sorted(SCENARIOS)})

        def do_POST(self):  # noqa: N802
            parts = [p for p in self.path.split("/") if p]
            if parts[:1] == ["scenario"]:
                return self._scenario(*(parts[1:] + [None, None, None])[:3])
            action, value = (parts + [None, None])[:2]
            if action == "rate" and value:
                load.base_rate = max(0.0, float(value))
                LOG.info("base rate set to %.1f requests/s", load.base_rate)
            elif action == "pause":
                load.paused = True
            elif action == "resume":
                load.paused = False
            elif action == "flash-sale":
                load.start_flash_sale(int(value or load.flash.get("duration", 180)))
            elif action == "run" and value in load.jobs.run_fn:
                if not load.jobs.start(value):
                    return self._reply(409, {"error": f"{value} is already running"})
            else:
                return self._reply(400, {"error": "use: rate N | pause | resume | flash-sale [SECONDS] | run JOB",
                                         "jobs": sorted(load.jobs.run_fn)})
            self._reply(200, {**load.status(), "running_scenarios": [s["scenario"] for s in scenarios.status()]})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="control", daemon=True).start()


# ---- entry point ----------------------------------------------------------------------------------


def run(cfg, stop):
    from prometheus_client import start_http_server

    metrics = Metrics()
    start_http_server(int(cfg.get("metrics_port", 9302)), addr=cfg.get("metrics_address", "0.0.0.0"))
    annotations = Annotations(cfg["grafana"]) if cfg.get("grafana") else None
    timeout = float(cfg.get("timeout", 2.0))

    shop = Shop()
    while not stop.is_set():  # wait for the shop database (e.g. right after a deploy)
        try:
            with connect(cfg["dsn"]["app"], timeout, "labapp-shop") as conn:
                shop.load(conn)
            break
        except Exception as exc:
            LOG.warning("shop database not ready: %s", short_error(exc))
            stop.wait(5)
    if stop.is_set():
        return
    shop.hot_products = int(cfg.get("flash_sale", {}).get("hot_products", 20))

    jobs = Jobs(cfg, shop, metrics, annotations, stop)
    load = Load(cfg, shop, metrics, jobs, annotations, stop)
    scenarios = Scenarios(cfg, shop, jobs, metrics, annotations, stop)
    control_server(load, scenarios, int(cfg.get("control_port", 9301)))
    workers = int(cfg.get("workers", 24))
    for i in range(workers):
        threading.Thread(target=load.worker, name=f"worker-{i}", daemon=True).start()
    threading.Thread(target=load.arrivals, name="arrivals", daemon=True).start()
    threading.Thread(target=load.scheduler, name="scheduler", daemon=True).start()
    LOG.info(
        "shop workload: %.0f requests/s base, %d workers, %d products, %d customers",
        load.base_rate, workers, shop.product_n, shop.customer_n,
    )
    while not stop.is_set():
        stop.wait(1)
    LOG.info("stopped")
