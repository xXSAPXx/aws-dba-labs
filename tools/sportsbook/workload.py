"""sportsbook workload: a sportsbook's production traffic on MySQL (the sportsbook-workload service).

Three kinds of traffic, as in production:

  Players    Open loop, like real users: requests arrive at the target rate whether or not the database keeps
             up. When it slows down, requests queue for a free worker and the response time includes that wait.
             Workers (one connection each, as sb_app) run them as business transactions: browsing, live odds,
             log-ins, bets, cash-outs, deposits, withdrawals, sign-ups.
  Odds feed  One connection (sb_feed): kicks matches off, moves the odds, scores goals (the match's markets are
             suspended for a moment), ends matches with their results, and adds new fixtures.
  Batch      Own connections and roles, on schedules: settlement after full time, withdrawals, bonuses, the
             leaderboard, session clean-up (sb_batch), and a long regulatory report (sb_reporting).

Target rate = base rate x daily wave x kick-off surge (when matches kick off, players pile in) (0 while paused).
Control it live with `sportsbook load ...` (a local HTTP endpoint, 127.0.0.1 only).
"""

import json
import math
import queue
import random
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import LOG, Annotations, classify, connect, short_error, transaction

# Response time buckets (seconds): 5 ms .. 30 s.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# Every result a request can end with: "ok", business outcomes (a refused request, not an error), errors.
OUTCOMES = ("insufficient_funds", "odds_changed", "market_suspended", "limit_reached", "blocked", "kyc_required",
            "declined", "nothing_to_do")
ERRORS = ("lock_timeout", "deadlock", "query_timeout", "killed", "too_many_connections", "denied", "missing_table",
          "check_violation", "connection", "other")

STAKES = [2, 5, 5, 10, 10, 10, 20, 25, 50, 100]
DEPOSITS = [10, 20, 25, 50, 50, 100, 100, 200, 500]
WITHDRAWALS = [20, 50, 100, 200, 500]
CHANNELS = ["mobile", "mobile", "mobile", "app", "app", "web"]
METHODS = ["card", "card", "card", "ewallet", "bank_transfer", "crypto"]
COUNTRIES = ["GB", "DE", "ES", "IT", "IE", "FR", "NL", "PT", "BG", "RO", "GR", "SE", "MT"]
CENT = Decimal("0.01")

LEDGER_SQL = """
INSERT INTO wallet_transactions (player_id, type, account, amount, balance_after, reference_type, reference_id, idempotency_key)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""


class Outcome(Exception):
    """A business outcome, not an error: the request was refused (insufficient funds, odds changed, ...)."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def money(value):
    return Decimal(value).quantize(CENT)


def placeholders(values):
    return ", ".join(["%s"] * len(values))


# ---- metrics -------------------------------------------------------------------------------------


class Metrics:
    def __init__(self):
        from prometheus_client import Counter, Gauge, Histogram

        try:
            from prometheus_client import disable_created_metrics

            disable_created_metrics()
        except ImportError:
            pass
        self.tx = Counter("sportsbook_tx", "Player requests by type and result", ["tx", "result"])
        self.latency = Histogram(
            "sportsbook_tx_latency_seconds", "Response time as players see it: queue wait + transaction",
            ["tx"], buckets=LATENCY_BUCKETS,
        )
        self.queue_depth = Gauge("sportsbook_tx_queue_depth", "Requests waiting for a free worker")
        self.base_rate = Gauge("sportsbook_load_base_rate", "Base request rate (requests per second)")
        self.target_rate = Gauge("sportsbook_load_target_rate", "Current target rate: base x wave x kick-off surge")
        self.surge = Gauge("sportsbook_load_surge", "1 while players pile in after a kick-off")
        self.paused = Gauge("sportsbook_load_paused", "1 while the player traffic is paused")
        self.events = Gauge("sportsbook_events", "Matches by status: scheduled, live, finished (not settled yet)", ["status"])
        self.feed = Counter("sportsbook_feed_actions", "What the odds feed did", ["action"])
        self.feed_errors = Counter("sportsbook_feed_errors", "Odds feed failures by kind", ["kind"])
        self.money = Counter("sportsbook_money", "Money moved, in EUR, by flow", ["flow"])
        self.settled = Counter("sportsbook_settled_bets", "Bets settled after full time, by result", ["result"])
        # The label is job_name, not job: Prometheus keeps "job" for the scrape and would rename ours to exported_job.
        self.job_active = Gauge("sportsbook_job_active", "1 while a batch job or report runs", ["job_name"])
        self.job_runs = Counter("sportsbook_job_runs", "Batch job and report runs by result", ["job_name", "result"])
        self.job_duration = Gauge("sportsbook_job_last_duration_seconds", "Duration of the last run", ["job_name"])


# ---- what the application keeps in memory ------------------------------------------------------------


class Book:
    """Like a real sportsbook's application servers, it caches the catalogue: the generated players, the
    competitions, and the open matches with their selections and odds (refreshed every few seconds). The odds
    a player "sees" come from here, so they can be a few seconds old: a bet then fails with odds_changed."""

    def __init__(self):
        self.players_n = 1
        self.competitions = [1]
        self.live = []  # event ids
        self.upcoming = []  # event ids, soonest first
        self.selections = {}  # event id -> [(selection id, odds, market status)]

    def load_static(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(id) FROM players WHERE username LIKE 'player%'")
            self.players_n = cur.fetchone()[0] or 1
            cur.execute("SELECT id, tier FROM competitions WHERE sport_id = 1")
            # Top leagues get more traffic: tier 1 three times, tier 2 twice, tier 3 once.
            self.competitions = [cid for cid, tier in cur.fetchall() for _ in range(4 - min(tier, 3))]

    def refresh(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM events WHERE status = 'live' ORDER BY kicked_off_at, id")
            live = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT id FROM events WHERE status = 'scheduled' ORDER BY starts_at, id LIMIT 200")
            upcoming = [r[0] for r in cur.fetchall()]
            selections = {}
            ids = live + upcoming
            if ids:
                cur.execute(
                    f"""SELECT m.event_id, s.id, s.odds, m.status FROM markets m JOIN selections s ON s.market_id = m.id
                        WHERE m.event_id IN ({placeholders(ids)}) ORDER BY m.event_id, s.id""", ids)
                for event_id, selection_id, odds, status in cur.fetchall():
                    selections.setdefault(event_id, []).append((selection_id, odds, status))
        self.live, self.upcoming, self.selections = live, upcoming, selections

    def set_odds(self, event_id, selection_id, odds):
        rows = self.selections.get(event_id)
        if rows:
            self.selections[event_id] = [(s, odds if s == selection_id else o, st) for s, o, st in rows]

    def player(self, skew=1.5):
        """A generated player; skew > 1 favours the low ids, the heavy bettors of the generated data."""
        return 1 + int(self.players_n * random.random() ** skew)

    def competition(self):
        return random.choice(self.competitions)

    def upcoming_event(self):
        return self.upcoming[int(len(self.upcoming) * random.random() ** 2)] if self.upcoming else None

    def live_event(self):
        return random.choice(self.live) if self.live else None

    def event(self, live_share):
        if self.live and (random.random() < live_share or not self.upcoming):
            return self.live_event()
        return self.upcoming_event()

    def picks(self, legs, live_share):
        """The selections of a bet: one per match, as a player saw them: [(event id, selection id, odds)]."""
        picks, events = [], set()
        for _ in range(legs * 3):
            event_id = self.event(live_share)
            if event_id is None or event_id in events or not self.selections.get(event_id):
                continue
            selection_id, odds, _status = random.choice(self.selections[event_id])
            picks.append((event_id, selection_id, odds))
            events.add(event_id)
            if len(picks) == legs:
                break
        return picks


# ---- the players' transactions (as sb_app) -------------------------------------------------------------
# Each takes (conn, book, surge) and returns None ("ok") or raises Outcome / a database error.

HOME_SQL = """
SELECT e.id, e.starts_at, e.status, e.home_score, e.away_score, ht.name, aw.name, s.name, s.odds
FROM events e
JOIN teams ht ON ht.id = e.home_team_id
JOIN teams aw ON aw.id = e.away_team_id
JOIN markets m ON m.event_id = e.id AND m.type = 'match_result'
JOIN selections s ON s.market_id = m.id
WHERE e.competition_id = %s AND e.status IN ('scheduled', 'live')
ORDER BY e.starts_at, e.id, s.id
LIMIT 60"""

LIVE_SQL = """
SELECT e.id, e.kicked_off_at, e.home_score, e.away_score, ht.name, aw.name, m.status, s.name, s.odds
FROM events e
JOIN teams ht ON ht.id = e.home_team_id
JOIN teams aw ON aw.id = e.away_team_id
JOIN markets m ON m.event_id = e.id AND m.type = 'match_result'
JOIN selections s ON s.market_id = m.id
WHERE e.status = 'live'
ORDER BY e.kicked_off_at, e.id, s.id
LIMIT 90"""


def tx_home(conn, book, surge):
    """The page of a competition: its next 20 matches with the match-result odds."""
    with conn.cursor() as cur:
        cur.execute(HOME_SQL, (book.competition(),))
        cur.fetchall()


def tx_event_page(conn, book, surge):
    """One match with all its markets and odds."""
    event_id = book.event(live_share=0.6 if surge else 0.4)
    if event_id is None:
        raise Outcome("nothing_to_do")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.starts_at, e.status, e.home_score, e.away_score, c.name, ht.name, aw.name
               FROM events e
               JOIN competitions c ON c.id = e.competition_id
               JOIN teams ht ON ht.id = e.home_team_id
               JOIN teams aw ON aw.id = e.away_team_id
               WHERE e.id = %s""", (event_id,))
        cur.fetchone()
        cur.execute(
            """SELECT m.id, m.type, m.status, s.id, s.name, s.odds
               FROM markets m JOIN selections s ON s.market_id = m.id
               WHERE m.event_id = %s ORDER BY m.id, s.id""", (event_id,))
        cur.fetchall()


def tx_live(conn, book, surge):
    """The live page: every match in play, its score and match-result odds."""
    with conn.cursor() as cur:
        cur.execute(LIVE_SQL)
        cur.fetchall()


def tx_login(conn, book, surge):
    player = book.player(skew=1.5)
    channel = random.choice(CHANNELS)
    ip = f"203.0.113.{random.randint(1, 254)}"
    with transaction(conn) as cur:
        cur.execute("SELECT id, status FROM players WHERE username = %s", (f"player{player}",))
        row = cur.fetchone()
        if row is None or row[1] != "active":
            raise Outcome("blocked")  # self-excluded, suspended or closed
        cur.execute(
            """INSERT INTO player_sessions (token, player_id, channel, ip, expires_at)
               VALUES (%s, %s, %s, %s, NOW(3) + INTERVAL 2 HOUR)""", (uuid.uuid4().hex, player, channel, ip))
        cur.execute("UPDATE players SET last_login_at = NOW(3) WHERE id = %s", (player,))
        cur.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, details) VALUES (%s, 'login', 'player', %s, %s)",
            (f"player:{player}", player, json.dumps({"channel": channel, "ip": ip})))


def tx_place_bet(conn, book, surge, metrics):
    """A bet: the wallet row is locked first (it serializes everything that moves this player's money), then
    the limits, the balance and the odds are checked, and bet, legs, wallet and ledger change in one transaction."""
    player = book.player(skew=2.2)
    legs = 1 if random.random() < 0.7 else random.randint(2, 4)
    picks = book.picks(legs, live_share=0.7 if surge else 0.5)
    if not picks:
        raise Outcome("market_suspended")
    stake = money(random.choice(STAKES))
    channel = random.choice(CHANNELS)
    with transaction(conn) as cur:
        cur.execute(
            """SELECT w.balance, l.max_stake, l.daily_loss_limit, p.status
               FROM wallets w
               JOIN player_limits l ON l.player_id = w.player_id
               JOIN players p ON p.id = w.player_id
               WHERE w.player_id = %s
               FOR UPDATE OF w""", (player,))
        row = cur.fetchone()
        if row is None or row[3] != "active":
            raise Outcome("blocked")
        balance, max_stake, daily_loss_limit, _status = row
        if stake > max_stake:
            raise Outcome("limit_reached")
        if balance < stake:
            raise Outcome("insufficient_funds")
        if daily_loss_limit is not None:
            cur.execute("SELECT COALESCE(SUM(stake), 0) FROM bets WHERE player_id = %s AND placed_at >= CURDATE()", (player,))
            if cur.fetchone()[0] + stake > daily_loss_limit:
                raise Outcome("limit_reached")
        ids = [selection_id for _event, selection_id, _odds in picks]
        cur.execute(
            f"""SELECT s.id, s.odds, m.status, e.status
                FROM selections s
                JOIN markets m ON m.id = s.market_id
                JOIN events e ON e.id = m.event_id
                WHERE s.id IN ({placeholders(ids)})""", ids)
        current = {r[0]: r for r in cur.fetchall()}
        total_odds = Decimal(1)
        for _event, selection_id, seen in picks:
            row = current.get(selection_id)
            if row is None or row[2] != "open" or row[3] not in ("scheduled", "live"):
                raise Outcome("market_suspended")
            if abs(row[1] - seen) > seen * Decimal("0.02"):
                raise Outcome("odds_changed")  # the player has to confirm the new odds first
            total_odds *= row[1]
        total_odds = min(total_odds, Decimal(1000)).quantize(Decimal("0.001"))
        cur.execute(
            """INSERT INTO bets (player_id, type, stake, total_odds, potential_payout, channel)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (player, "single" if len(picks) == 1 else "accumulator", stake, total_odds, money(stake * total_odds), channel))
        bet_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO bet_selections (bet_id, selection_id, event_id, odds) VALUES (%s, %s, %s, %s)",
            [(bet_id, selection_id, event_id, current[selection_id][1]) for event_id, selection_id, _seen in picks])
        cur.execute("UPDATE wallets SET balance = balance - %s WHERE player_id = %s", (stake, player))
        cur.execute(LEDGER_SQL, (player, "stake", "cash", -stake, balance - stake, "bet", bet_id, str(uuid.uuid4())))
    metrics.money.labels("stake").inc(float(stake))


def tx_cashout(conn, book, surge, metrics):
    """A player takes an early payout on a single bet of a live match, at a price from the current odds."""
    event_id = book.live_event()
    if event_id is None:
        raise Outcome("nothing_to_do")
    with transaction(conn) as cur:
        cur.execute(
            """SELECT b.id, b.player_id, b.stake, bs.odds, s.odds, m.status
               FROM bet_selections bs
               JOIN bets b ON b.id = bs.bet_id
               JOIN selections s ON s.id = bs.selection_id
               JOIN markets m ON m.id = s.market_id
               WHERE bs.event_id = %s AND b.status = 'open' AND b.type = 'single'
               LIMIT 1 OFFSET %s""", (event_id, random.randrange(20)))
        row = cur.fetchone()
        if row is None:
            raise Outcome("nothing_to_do")
        bet_id, player, stake, placed_odds, current_odds, market_status = row
        if market_status != "open":
            raise Outcome("market_suspended")
        value = money(stake * placed_odds / current_odds * Decimal("0.95"))
        # The same lock order as a bet: the wallet first, then the bet.
        cur.execute("SELECT balance FROM wallets WHERE player_id = %s FOR UPDATE", (player,))
        balance = cur.fetchone()[0]
        cur.execute(
            "UPDATE bets SET status = 'cashed_out', payout = %s, settled_at = NOW(3) WHERE id = %s AND status = 'open'",
            (value, bet_id))
        if cur.rowcount == 0:
            raise Outcome("nothing_to_do")  # settled or cashed out a moment ago
        cur.execute("UPDATE wallets SET balance = balance + %s WHERE player_id = %s", (value, player))
        cur.execute(LEDGER_SQL, (player, "cashout", "cash", value, balance + value, "bet", bet_id, str(uuid.uuid4())))
    metrics.money.labels("cashout").inc(float(value))


def tx_deposit(conn, book, surge, metrics):
    """Two steps, like a real payment: the request (a pending payment), then the provider's callback moments
    later: approved (the wallet is credited) or declined."""
    player = book.player(skew=2.0)
    amount = money(random.choice(DEPOSITS))
    with transaction(conn) as cur:
        cur.execute("SELECT daily_deposit_limit FROM player_limits WHERE player_id = %s", (player,))
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM payments
                   WHERE player_id = %s AND kind = 'deposit' AND status = 'approved' AND created_at >= CURDATE()""",
                (player,))
            if cur.fetchone()[0] + amount > row[0]:
                raise Outcome("limit_reached")
        cur.execute(
            "INSERT INTO payments (player_id, kind, method, amount, status) VALUES (%s, 'deposit', %s, %s, 'pending')",
            (player, random.choice(METHODS), amount))
        payment_id = cur.lastrowid
    provider_ref = f"PSP-{uuid.uuid4().hex[:16]}"
    with transaction(conn) as cur:
        if random.random() < 0.05:
            cur.execute("UPDATE payments SET status = 'declined', provider_ref = %s WHERE id = %s", (provider_ref, payment_id))
            return "declined"
        cur.execute("SELECT balance FROM wallets WHERE player_id = %s FOR UPDATE", (player,))
        balance = cur.fetchone()[0]
        cur.execute("UPDATE wallets SET balance = balance + %s WHERE player_id = %s", (amount, player))
        cur.execute("UPDATE payments SET status = 'approved', provider_ref = %s WHERE id = %s", (provider_ref, payment_id))
        cur.execute(LEDGER_SQL, (player, "deposit", "cash", amount, balance + amount, "payment", payment_id, str(uuid.uuid4())))
    metrics.money.labels("deposit").inc(float(amount))
    return None


def tx_withdrawal(conn, book, surge, metrics):
    """The money leaves the wallet at once and waits as a pending withdrawal; a batch job pays it out."""
    player = book.player(skew=2.0)
    with transaction(conn) as cur:
        cur.execute(
            """SELECT w.balance, p.kyc_status FROM wallets w JOIN players p ON p.id = w.player_id
               WHERE w.player_id = %s FOR UPDATE OF w""", (player,))
        row = cur.fetchone()
        if row is None:
            raise Outcome("blocked")
        balance, kyc = row
        if kyc != "verified":
            raise Outcome("kyc_required")  # no payout before the identity check
        amount = min(balance, money(random.choice(WITHDRAWALS)))
        if amount < 10:
            raise Outcome("insufficient_funds")
        cur.execute(
            "INSERT INTO payments (player_id, kind, method, amount, status) VALUES (%s, 'withdrawal', 'bank_transfer', %s, 'pending')",
            (player, amount))
        payment_id = cur.lastrowid
        cur.execute("UPDATE wallets SET balance = balance - %s WHERE player_id = %s", (amount, player))
        cur.execute(LEDGER_SQL, (player, "withdrawal", "cash", -amount, balance - amount, "payment", payment_id, str(uuid.uuid4())))
    metrics.money.labels("withdrawal").inc(float(amount))


def tx_bet_history(conn, book, surge):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT b.id, b.type, b.stake, b.total_odds, b.status, b.payout, b.placed_at,
                      bs.odds, s.name, s.result, e.home_score, e.away_score
               FROM (SELECT id, type, stake, total_odds, status, payout, placed_at FROM bets
                     WHERE player_id = %s ORDER BY placed_at DESC LIMIT 20) b
               JOIN bet_selections bs ON bs.bet_id = b.id
               JOIN selections s ON s.id = bs.selection_id
               JOIN events e ON e.id = bs.event_id
               ORDER BY b.placed_at DESC, b.id""", (book.player(skew=2.0),))
        cur.fetchall()


def tx_wallet(conn, book, surge):
    player = book.player(skew=2.0)
    with conn.cursor() as cur:
        cur.execute("SELECT balance, bonus_balance, currency FROM wallets WHERE player_id = %s", (player,))
        cur.fetchone()
        cur.execute(
            """SELECT type, account, amount, balance_after, created_at FROM wallet_transactions
               WHERE player_id = %s ORDER BY created_at DESC LIMIT 20""", (player,))
        cur.fetchall()


def tx_leaderboard(conn, book, surge):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.position, l.points, p.username FROM leaderboard l JOIN players p ON p.id = l.player_id
               WHERE l.period = CURDATE() - INTERVAL WEEKDAY(CURDATE()) DAY ORDER BY l.position LIMIT 100""")
        cur.fetchall()


def tx_signup(conn, book, surge):
    name = f"p{uuid.uuid4().hex[:15]}"
    with transaction(conn) as cur:
        cur.execute(
            "INSERT INTO players (username, email, country_code) VALUES (%s, %s, %s)",
            (name, f"{name}@example.com", random.choice(COUNTRIES)))
        player = cur.lastrowid
        cur.execute("INSERT INTO player_limits (player_id) VALUES (%s)", (player,))
        cur.execute("INSERT INTO wallets (player_id) VALUES (%s)", (player,))
        cur.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id) VALUES (%s, 'signup', 'player', %s)",
            (f"player:{player}", player))


# name -> (function, takes the metrics too)
TRANSACTIONS = {
    "home": (tx_home, False),
    "event_page": (tx_event_page, False),
    "live": (tx_live, False),
    "login": (tx_login, False),
    "place_bet": (tx_place_bet, True),
    "bet_history": (tx_bet_history, False),
    "wallet": (tx_wallet, False),
    "deposit": (tx_deposit, True),
    "cashout": (tx_cashout, True),
    "withdrawal": (tx_withdrawal, True),
    "leaderboard": (tx_leaderboard, False),
    "signup": (tx_signup, False),
}

DEFAULT_MIX = {
    "home": 20, "event_page": 18, "live": 10, "login": 8, "place_bet": 16, "bet_history": 7, "wallet": 6,
    "deposit": 5, "cashout": 3, "withdrawal": 2, "leaderboard": 3, "signup": 2,
}
# Right after a kick-off: players go to the live pages and bet.
DEFAULT_SURGE_MIX = {
    "home": 10, "event_page": 20, "live": 20, "login": 10, "place_bet": 26, "bet_history": 3, "wallet": 3,
    "deposit": 5, "cashout": 1, "withdrawal": 1, "leaderboard": 1,
}


# ---- the odds feed (as sb_feed) ------------------------------------------------------------------------


def implied_odds(event_id):
    """Opening odds of a new fixture: the same formula as the generated data (6% bookmaker margin)."""
    r, q, w = (event_id * 7919) % 1000 / 1000, (event_id * 104729) % 1000 / 1000, (event_id * 1299709) % 1000 / 1000
    p_home = 0.15 + 0.6 * r
    p_away = (1 - p_home) * 0.62
    p_over, p_yes = 0.35 + 0.35 * q, 0.35 + 0.35 * w
    probabilities = [p_home, 1 - p_home - p_away, p_away, p_over, 1 - p_over, p_yes, 1 - p_yes]
    return [Decimal(str(max(1.01, round(1 / (1.06 * p), 2)))) for p in probabilities]


SELECTION_NAMES = [("match_result", "home"), ("match_result", "draw"), ("match_result", "away"),
                   ("over_under_2_5", "over"), ("over_under_2_5", "under"),
                   ("both_teams_score", "yes"), ("both_teams_score", "no")]

RESULTS_SQL = """
UPDATE selections s
JOIN markets m ON m.id = s.market_id
JOIN events e ON e.id = m.event_id
SET s.result = CASE
    WHEN (s.name = 'home' AND e.home_score > e.away_score)
      OR (s.name = 'draw' AND e.home_score = e.away_score)
      OR (s.name = 'away' AND e.home_score < e.away_score)
      OR (s.name = 'over' AND e.home_score + e.away_score > 2)
      OR (s.name = 'under' AND e.home_score + e.away_score < 3)
      OR (s.name = 'yes' AND e.home_score > 0 AND e.away_score > 0)
      OR (s.name = 'no' AND (e.home_score = 0 OR e.away_score = 0)) THEN 'won'
    ELSE 'lost' END
WHERE e.id = %s"""


class Feed:
    """The data supplier's feed, as the trading system applies it: kick-offs, odds moves, goals, full time."""

    def __init__(self, cfg, book, metrics, load, annotations, stop):
        self.cfg = cfg
        self.settings = cfg.get("feed", {})
        self.book = book
        self.metrics = metrics
        self.load = load
        self.annotations = annotations
        self.stop = stop
        self.timeout = float(cfg.get("timeout", 5.0))
        self.match_seconds = float(self.settings.get("match_seconds", 900))
        self.goal_chance = float(self.settings.get("goals_per_match", 2.6)) / self.match_seconds  # per second
        self.suspend_seconds = float(self.settings.get("suspend_seconds", 15))
        self.odds_moves = float(self.settings.get("odds_moves_per_second", 15))
        self.kickoff_now = threading.Event()
        self.kickoff_count = int(self.settings.get("kickoff_matches", 6))
        self.suspended = {}  # event id -> monotonic time to reopen its markets
        for action in ("kickoff", "odds_move", "goal", "resume", "full_time", "fixture"):
            metrics.feed.labels(action)

    def run(self):
        conn = None
        start = time.monotonic()
        next_kickoff = start + float(self.settings.get("kickoff_first", 60))
        next_fixtures = next_counts = start
        while not self.stop.is_set():
            try:
                if conn is None:
                    conn = connect(self.cfg["db"]["feed"], "sportsbook-feed", self.cfg.get("session", {}).get("feed"), self.timeout)
                now = time.monotonic()
                if now >= next_kickoff or self.kickoff_now.is_set():
                    self.kickoff_now.clear()
                    self.kickoff(conn, self.kickoff_count)
                    next_kickoff = now + float(self.settings.get("kickoff_every", 600))
                self.tick(conn)
                if now >= next_counts:
                    self.count_events(conn)
                    next_counts = now + 5
                if now >= next_fixtures:
                    self.fixtures(conn)
                    next_fixtures = now + 60
            except Exception as exc:
                kind = classify(exc)
                self.metrics.feed_errors.labels(kind).inc()
                LOG.warning("odds feed: %s", short_error(exc))
                if kind == "connection" and conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
            self.stop.wait(1)

    def kickoff(self, conn, count):
        """The next scheduled matches start now (lab time: earlier than their fixture time)."""
        with transaction(conn) as cur:
            cur.execute(
                "SELECT id FROM events WHERE status = 'scheduled' ORDER BY starts_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (count,))
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                cur.execute(
                    f"""UPDATE events SET status = 'live', kicked_off_at = NOW(3), starts_at = LEAST(starts_at, NOW(3))
                        WHERE id IN ({placeholders(ids)})""", ids)
        if ids:
            self.metrics.feed.labels("kickoff").inc(len(ids))
            self.load.start_surge(len(ids))
            LOG.info("kick-off: %d matches", len(ids))

    def tick(self, conn):
        """Once a second: full time, goals and reopened markets for the live matches, and odds moves."""
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, home_score, away_score, TIMESTAMPDIFF(SECOND, kicked_off_at, NOW(3))
                   FROM events WHERE status = 'live'""")
            live = cur.fetchall()
        now = time.monotonic()
        for event_id, _home, _away, elapsed in live:
            if elapsed >= self.match_seconds:
                self.full_time(conn, event_id)
            elif event_id not in self.suspended and random.random() < self.goal_chance:
                self.goal(conn, event_id)
        for event_id, reopen_at in list(self.suspended.items()):
            if now >= reopen_at:
                self.resume(conn, event_id)
        self.move_odds(conn)

    def goal(self, conn, event_id):
        """A goal: the score changes and the match's markets are suspended until the odds are recalculated."""
        side = "home_score" if random.random() < 0.55 else "away_score"
        with transaction(conn) as cur:
            cur.execute(f"UPDATE events SET {side} = {side} + 1 WHERE id = %s AND status = 'live'", (event_id,))
            cur.execute("UPDATE markets SET status = 'suspended' WHERE event_id = %s AND status = 'open'", (event_id,))
        self.suspended[event_id] = time.monotonic() + self.suspend_seconds
        self.metrics.feed.labels("goal").inc()

    def resume(self, conn, event_id):
        """New odds after the goal, then the markets open again."""
        rows = self.book.selections.get(event_id, [])
        with transaction(conn) as cur:
            for selection_id, odds, _status in rows:
                new = Decimal(str(max(1.01, round(float(odds) * random.uniform(0.75, 1.3), 2))))
                cur.execute("UPDATE selections SET odds = %s WHERE id = %s AND result = 'pending'", (new, selection_id))
                cur.execute("INSERT INTO odds_history (selection_id, odds) VALUES (%s, %s)", (selection_id, new))
                self.book.set_odds(event_id, selection_id, new)
            cur.execute("UPDATE markets SET status = 'open' WHERE event_id = %s AND status = 'suspended'", (event_id,))
        self.suspended.pop(event_id, None)
        self.metrics.feed.labels("resume").inc()

    def full_time(self, conn, event_id):
        """The final whistle: markets closed, every selection won or lost. Settlement pays out next."""
        with transaction(conn) as cur:
            cur.execute("UPDATE events SET status = 'finished', finished_at = NOW(3) WHERE id = %s AND status = 'live'", (event_id,))
            cur.execute("UPDATE markets SET status = 'closed' WHERE event_id = %s", (event_id,))
            cur.execute(RESULTS_SQL, (event_id,))
        self.suspended.pop(event_id, None)
        self.metrics.feed.labels("full_time").inc()

    def move_odds(self, conn):
        """The traders' odds moves: mostly live matches, some of the next ones."""
        moves = int(self.odds_moves) + (1 if random.random() < self.odds_moves % 1 else 0)
        changes = []
        for _ in range(moves):
            event_id = self.book.event(live_share=0.7)
            if event_id is None or event_id in self.suspended or not self.book.selections.get(event_id):
                continue
            selection_id, odds, status = random.choice(self.book.selections[event_id])
            if status == "open":
                new = Decimal(str(max(1.01, round(float(odds) * random.uniform(0.95, 1.05), 2))))
                changes.append((event_id, selection_id, new))
        if not changes:
            return
        with transaction(conn) as cur:
            for _event, selection_id, new in changes:
                cur.execute("UPDATE selections SET odds = %s WHERE id = %s AND result = 'pending'", (new, selection_id))
            cur.executemany(
                "INSERT INTO odds_history (selection_id, odds) VALUES (%s, %s)",
                [(selection_id, new) for _event, selection_id, new in changes])
        for event_id, selection_id, new in changes:
            self.book.set_odds(event_id, selection_id, new)
        self.metrics.feed.labels("odds_move").inc(len(changes))

    def count_events(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) FROM events WHERE status IN ('scheduled', 'live', 'finished') GROUP BY status")
            counts = dict(cur.fetchall())
        for status in ("scheduled", "live", "finished"):
            self.metrics.events.labels(status).set(counts.get(status, 0))

    def fixtures(self, conn):
        """New fixtures at the end of the schedule, so the lab never runs out of matches to kick off."""
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(starts_at) FROM events WHERE status = 'scheduled'")
            scheduled, last_start = cur.fetchone()
        missing = int(self.settings.get("fixtures_min", 300)) - scheduled
        if missing <= 0:
            return
        with transaction(conn) as cur:
            cur.execute("SELECT id FROM competitions WHERE sport_id = 1")
            competitions = [r[0] for r in cur.fetchall()]
            for i in range(missing):
                competition = random.choice(competitions)
                cur.execute("SELECT id FROM teams WHERE competition_id = %s", (competition,))
                home, away = random.sample([r[0] for r in cur.fetchall()], 2)
                cur.execute(
                    """INSERT INTO events (competition_id, home_team_id, away_team_id, starts_at)
                       VALUES (%s, %s, %s, COALESCE(%s, NOW(3)) + INTERVAL %s MINUTE)""",
                    (competition, home, away, last_start, 10 * (i + 1)))
                event_id = cur.lastrowid
                odds = implied_odds(event_id)
                markets = {}
                for market in ("match_result", "over_under_2_5", "both_teams_score"):
                    cur.execute("INSERT INTO markets (event_id, type) VALUES (%s, %s)", (event_id, market))
                    markets[market] = cur.lastrowid
                cur.executemany(
                    "INSERT INTO selections (market_id, name, odds) VALUES (%s, %s, %s)",
                    [(markets[market], name, odds[k]) for k, (market, name) in enumerate(SELECTION_NAMES)])
        self.metrics.feed.labels("fixture").inc(missing)


# ---- batch jobs and reports ---------------------------------------------------------------------------

REPORT_STEPS = [
    # Gross gaming revenue (stakes - payouts) per day and country: what the regulators tax.
    """SELECT DATE(b.placed_at) AS day, p.country_code, COUNT(*) AS bets, SUM(b.stake) AS turnover,
              SUM(COALESCE(b.payout, 0)) AS payouts, SUM(b.stake) - SUM(COALESCE(b.payout, 0)) AS ggr
       FROM bets b JOIN players p ON p.id = b.player_id
       WHERE b.placed_at >= CURDATE() - INTERVAL 30 DAY
       GROUP BY DATE(b.placed_at), p.country_code""",
    # Turnover per competition.
    """SELECT c.name, COUNT(*) AS legs, SUM(b.stake) AS turnover
       FROM bet_selections bs
       JOIN bets b ON b.id = bs.bet_id
       JOIN events e ON e.id = bs.event_id
       JOIN competitions c ON c.id = e.competition_id
       WHERE e.starts_at >= CURDATE() - INTERVAL 30 DAY
       GROUP BY c.name""",
    # Responsible gambling: bets of self-excluded players in the last day (should be none).
    """SELECT p.id, p.username, COUNT(b.id) AS bets
       FROM players p JOIN bets b ON b.player_id = p.id AND b.placed_at >= NOW() - INTERVAL 1 DAY
       WHERE p.status = 'self_excluded'
       GROUP BY p.id, p.username""",
    # Anti-money laundering: deposits above 2,000 within 24 hours.
    """SELECT player_id, COUNT(*) AS deposits, SUM(amount) AS total
       FROM payments
       WHERE kind = 'deposit' AND status = 'approved' AND created_at >= NOW() - INTERVAL 1 DAY
       GROUP BY player_id HAVING SUM(amount) > 2000""",
    # Liability: what the open bets would pay out, per match.
    """SELECT bs.event_id, COUNT(*) AS open_bets, SUM(b.potential_payout) AS liability
       FROM bets b JOIN bet_selections bs ON bs.bet_id = b.id
       WHERE b.status = 'open'
       GROUP BY bs.event_id ORDER BY liability DESC LIMIT 100""",
]
# Finance: the wallets must hold what the ledger adds up to. In one snapshot, they always agree.
RECONCILIATION_SQL = """
SELECT (SELECT COALESCE(SUM(balance), 0) FROM wallets),
       (SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE account = 'cash')"""


class Jobs:
    def __init__(self, cfg, metrics, annotations, stop):
        self.cfg = cfg
        self.metrics = metrics
        self.annotations = annotations
        self.stop = stop
        self.timeout = float(cfg.get("timeout", 5.0))
        self.lock = threading.Lock()
        self.active = {}  # job -> start time
        self.run_fn = {
            "settlement": ("batch", self.settlement),
            "withdrawals": ("batch", self.withdrawals),
            "bonus_awards": ("batch", self.bonus_awards),
            "leaderboard": ("batch", self.leaderboard),
            "session_cleanup": ("batch", self.session_cleanup),
            "regulatory_report": ("reporting", self.regulatory_report),
        }
        for job in self.run_fn:
            for result in ("ok", "error"):
                metrics.job_runs.labels(job, result)
            metrics.job_active.labels(job).set(0)
        for result in ("won", "lost"):
            metrics.settled.labels(result)

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
        result, detail = "ok", None
        try:
            read_timeout = None if role == "reporting" else 60.0
            session = self.cfg.get("session", {}).get(role)
            with connect(self.cfg["db"][role], f"sportsbook-{job}", session, self.timeout, read_timeout) as conn:
                detail = fn(conn, settings)
        except Exception as exc:
            result, detail = "error", short_error(exc)
        duration = time.time() - started
        if detail or result == "error":
            LOG.info("%s: %s after %.1f s %s", job, result, duration, detail or "")
            if self.annotations:
                self.annotations.finish((job, started), started, time.time(),
                                        f"{job}: {result} after {duration:.0f} s {detail or ''}", ["sportsbook", "workload", job])
        self.metrics.job_runs.labels(job, result).inc()
        self.metrics.job_duration.labels(job).set(duration)
        self.metrics.job_active.labels(job).set(0)
        with self.lock:
            del self.active[job]

    # ---- settlement -----------------------------------------------------------------------------

    def settlement(self, conn, settings):
        """Pays out the bets of matches at full time.

        mode single_transaction (the default, the risky way): one transaction per match settles every bet on
        it and credits every winner's wallet. The winners' wallet rows stay locked until the end: their new
        bets wait (lock_timeout) and the transaction is one big binlog event. mode batched: a commit every
        batch_size bets.
        """
        mode = settings.get("mode", "single_transaction")
        batch = int(settings.get("batch_size", 500))
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM events WHERE status = 'finished' ORDER BY finished_at, id LIMIT 20")
            events = [r[0] for r in cur.fetchall()]
        won = lost = 0
        for event_id in events:
            if self.stop.is_set():
                break
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT bs.bet_id FROM bet_selections bs JOIN bets b ON b.id = bs.bet_id
                       WHERE bs.event_id = %s AND b.status = 'open' ORDER BY bs.bet_id""", (event_id,))
                bet_ids = [r[0] for r in cur.fetchall()]
            if mode == "single_transaction":
                with transaction(conn) as cur:
                    w, l = self._settle_bets(cur, bet_ids)
                    self._close_event(cur, event_id)
                won, lost = won + w, lost + l
            else:
                for chunk in (bet_ids[i:i + batch] for i in range(0, len(bet_ids), batch)):
                    with transaction(conn) as cur:
                        w, l = self._settle_bets(cur, chunk)
                    won, lost = won + w, lost + l
                with transaction(conn) as cur:
                    self._close_event(cur, event_id)
        if not events:
            return None
        return f"({len(events)} matches, {won} bets won, {lost} lost, {mode})"

    def _settle_bets(self, cur, bet_ids):
        """Settles the bets whose legs are all decided (an accumulator waits for its last match)."""
        if not bet_ids:
            return 0, 0
        cur.execute(f"SELECT id FROM bets WHERE id IN ({placeholders(bet_ids)}) AND status = 'open' FOR UPDATE", bet_ids)
        locked = [r[0] for r in cur.fetchall()]
        if not locked:
            return 0, 0
        cur.execute(
            f"""SELECT b.id, b.player_id, b.potential_payout,
                       SUM(s.result = 'pending') AS pending, SUM(s.result = 'lost') AS lost
                FROM bets b
                JOIN bet_selections bs ON bs.bet_id = b.id
                JOIN selections s ON s.id = bs.selection_id
                WHERE b.id IN ({placeholders(locked)})
                GROUP BY b.id, b.player_id, b.potential_payout""", locked)
        rows = cur.fetchall()
        lost_ids = [r[0] for r in rows if r[4] > 0]
        winners = [(r[0], r[1], r[2]) for r in rows if r[4] == 0 and r[3] == 0]
        if lost_ids:
            cur.execute(f"UPDATE bets SET status = 'lost', payout = 0, settled_at = NOW(3) WHERE id IN ({placeholders(lost_ids)})", lost_ids)
        paid = Decimal(0)
        for bet_id, player, payout in winners:  # in bet order, as the bets came in
            cur.execute("UPDATE bets SET status = 'won', payout = %s, settled_at = NOW(3) WHERE id = %s", (payout, bet_id))
            cur.execute("UPDATE wallets SET balance = balance + %s WHERE player_id = %s", (payout, player))
            cur.execute("SELECT balance FROM wallets WHERE player_id = %s", (player,))
            balance = cur.fetchone()[0]
            cur.execute(LEDGER_SQL, (player, "win", "cash", payout, balance, "bet", bet_id, str(uuid.uuid4())))
            paid += payout
        self.metrics.settled.labels("won").inc(len(winners))
        self.metrics.settled.labels("lost").inc(len(lost_ids))
        self.metrics.money.labels("win").inc(float(paid))
        return len(winners), len(lost_ids)

    @staticmethod
    def _close_event(cur, event_id):
        cur.execute("UPDATE markets SET status = 'settled' WHERE event_id = %s", (event_id,))
        cur.execute("UPDATE events SET status = 'settled', settled_at = NOW(3) WHERE id = %s", (event_id,))

    # ---- money --------------------------------------------------------------------------------

    def withdrawals(self, conn, settings):
        """Pays out the pending withdrawals older than a minute; 3% are declined and the money goes back."""
        batch = int(settings.get("batch_size", 200))
        with transaction(conn) as cur:
            cur.execute(
                """SELECT id, player_id, amount FROM payments
                   WHERE kind = 'withdrawal' AND status = 'pending' AND created_at < NOW(3) - INTERVAL 1 MINUTE
                   ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED""", (batch,))
            rows = cur.fetchall()
            declined = [r for r in rows if random.random() < 0.03]
            paid = [r[0] for r in rows if r not in declined]
            if paid:
                cur.execute(
                    f"UPDATE payments SET status = 'paid', provider_ref = CONCAT('PSP-W-', id) WHERE id IN ({placeholders(paid)})", paid)
            for payment_id, player, amount in declined:
                cur.execute("UPDATE payments SET status = 'declined' WHERE id = %s", (payment_id,))
                cur.execute("UPDATE wallets SET balance = balance + %s WHERE player_id = %s", (amount, player))
                cur.execute("SELECT balance FROM wallets WHERE player_id = %s", (player,))
                balance = cur.fetchone()[0]
                cur.execute(LEDGER_SQL, (player, "withdrawal_reversal", "cash", amount, balance, "payment", payment_id, str(uuid.uuid4())))
        if not rows:
            return None
        return f"({len(paid)} paid, {len(declined)} declined)"

    def bonus_awards(self, conn, settings):
        """The hourly mission: whoever staked at least min_turnover this hour gets the bonus, once per hour.

        One transaction for every winner, wallets locked in player order: it can deadlock with settlement,
        which locks wallets in bet order.
        """
        promotion = int(settings.get("promotion_id", 1))
        limit = int(settings.get("max_players", 2000))
        with conn.cursor() as cur:
            cur.execute("SELECT amount, min_turnover FROM promotions WHERE id = %s AND active", (promotion,))
            row = cur.fetchone()
            cur.execute("SELECT DATE_FORMAT(NOW(), '%Y-%m-%d %H:00:00')")
            period = cur.fetchone()[0]
        if row is None:
            return "(the promotion is not active)"
        amount, min_turnover = row
        awarded = 0
        with transaction(conn) as cur:
            cur.execute(
                """SELECT t.player_id
                   FROM (SELECT player_id FROM bets WHERE placed_at >= %s
                         GROUP BY player_id HAVING SUM(stake) >= %s) t
                   LEFT JOIN player_promotions pp
                          ON pp.player_id = t.player_id AND pp.promotion_id = %s AND pp.period = %s
                   WHERE pp.player_id IS NULL
                   ORDER BY t.player_id LIMIT %s""", (period, min_turnover, promotion, period, limit))
            players = [r[0] for r in cur.fetchall()]
            for player in players:
                cur.execute("INSERT INTO player_promotions (player_id, promotion_id, period) VALUES (%s, %s, %s)",
                            (player, promotion, period))
                cur.execute("UPDATE wallets SET bonus_balance = bonus_balance + %s WHERE player_id = %s", (amount, player))
                cur.execute("SELECT bonus_balance FROM wallets WHERE player_id = %s", (player,))
                balance = cur.fetchone()[0]
                cur.execute(LEDGER_SQL, (player, "bonus", "bonus", amount, balance, "promotion", promotion, str(uuid.uuid4())))
                awarded += 1
        if awarded:
            self.metrics.money.labels("bonus").inc(float(amount * awarded))
            return f"({awarded} players got {amount})"
        return None

    def leaderboard(self, conn, settings):
        """This week's top 1,000 by points (stakes, and double for winnings): a scan of the week's bets."""
        with conn.cursor() as cur:
            cur.execute("SELECT CURDATE() - INTERVAL WEEKDAY(CURDATE()) DAY, NOW(3)")
            period, started = cur.fetchone()
        with transaction(conn) as cur:
            cur.execute(
                """REPLACE INTO leaderboard (period, player_id, points, position)
                   SELECT %s, t.player_id, t.points, ROW_NUMBER() OVER (ORDER BY t.points DESC, t.player_id)
                   FROM (SELECT player_id, CAST(SUM(stake) + 2 * SUM(COALESCE(payout, 0)) AS SIGNED) AS points
                         FROM bets WHERE placed_at >= %s
                         GROUP BY player_id ORDER BY points DESC LIMIT 1000) t""", (period, period))
            cur.execute("DELETE FROM leaderboard WHERE period = %s AND updated_at < %s", (period, started))
        return None

    def session_cleanup(self, conn, settings):
        """Deletes expired sessions in batches, each its own transaction."""
        batch = int(settings.get("batch_size", 5000))
        deleted = 0
        with conn.cursor() as cur:
            while not self.stop.is_set():
                cur.execute("DELETE FROM player_sessions WHERE expires_at < NOW(3) LIMIT %s", (batch,))
                deleted += cur.rowcount
                if cur.rowcount < batch:
                    break
        return f"({deleted} expired sessions deleted)" if deleted else None

    # ---- the regulatory report ----------------------------------------------------------------

    def regulatory_report(self, conn, settings):
        """Five reports and a reconciliation in one consistent snapshot (REPEATABLE READ), held for about
        `target` seconds: while it is open, InnoDB can't purge the old row versions it might still read,
        so the history list grows (SHOW ENGINE INNODB STATUS, PMM's InnoDB dashboards)."""
        target = float(settings.get("target", 300))
        steps = len(REPORT_STEPS) + 1
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            try:
                for sql in REPORT_STEPS:
                    if self.stop.is_set():
                        break
                    begin = time.monotonic()
                    cur.execute(sql)
                    cur.fetchall()
                    self.stop.wait(max(0.0, target / steps - (time.monotonic() - begin)))
                cur.execute(RECONCILIATION_SQL)
                wallets, ledger = cur.fetchone()
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        if wallets == ledger:
            return f"(wallets and ledger agree: {wallets})"
        return f"(MISMATCH: wallets {wallets}, ledger {ledger})"


# ---- the load generator ---------------------------------------------------------------------------


class Load:
    def __init__(self, cfg, book, metrics, jobs, annotations, stop):
        self.cfg = cfg
        self.book = book
        self.metrics = metrics
        self.jobs = jobs
        self.annotations = annotations
        self.stop = stop
        self.timeout = float(cfg.get("timeout", 5.0))
        self.base_rate = float(cfg.get("rate", 80))
        self.paused = False
        self.surge_until = 0.0
        self.max_wait = float(cfg.get("max_wait", 30))
        self.requests = queue.Queue(maxsize=int(cfg.get("queue_limit", 5000)))
        mix = cfg.get("mix") or DEFAULT_MIX
        surge_mix = cfg.get("surge_mix") or DEFAULT_SURGE_MIX
        self.mix = (list(mix), list(mix.values()))
        self.surge_mix = (list(surge_mix), list(surge_mix.values()))
        self.wave = cfg.get("wave", {"period": 3600, "amplitude": 0.4})
        self.surge = cfg.get("surge", {"multiplier": 3, "duration": 120})
        self.started = time.monotonic()
        for name in TRANSACTIONS:
            for result in ("ok",) + OUTCOMES + ERRORS + ("expired", "dropped"):
                metrics.tx.labels(name, result)
        for flow in ("stake", "win", "cashout", "deposit", "withdrawal", "bonus"):
            metrics.money.labels(flow)

    def target_rate(self):
        if self.paused:
            return 0.0
        elapsed = time.monotonic() - self.started
        wave = 1 + float(self.wave.get("amplitude", 0)) * math.sin(2 * math.pi * elapsed / float(self.wave.get("period", 3600)))
        surge = float(self.surge.get("multiplier", 3)) if self.surge_active() else 1.0
        return self.base_rate * wave * surge

    def surge_active(self):
        return time.time() < self.surge_until

    def start_surge(self, matches):
        start = time.time()
        seconds = int(self.surge.get("duration", 120))
        self.surge_until = start + seconds
        if self.annotations:
            self.annotations.finish(("kickoff", start), start, start + seconds,
                                    f"Kick-off: {matches} matches, players pile in for {seconds} s",
                                    ["sportsbook", "workload", "kickoff"])

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
            names, weights = self.surge_mix if self.surge_active() else self.mix
            tx = random.choices(names, weights)[0]
            try:
                self.requests.put_nowait((tx, time.monotonic()))
            except queue.Full:
                self.metrics.tx.labels(tx, "dropped").inc()

    def worker(self):
        conn = None
        session = self.cfg.get("session", {}).get("app")
        while not self.stop.is_set():
            try:
                tx, queued_at = self.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if time.monotonic() - queued_at > self.max_wait:  # the player gave up waiting
                self.metrics.tx.labels(tx, "expired").inc()
                continue
            fn, with_metrics = TRANSACTIONS[tx]
            result = "ok"
            for attempt in (1, 2):
                try:
                    if conn is None or not conn.open:
                        conn = connect(self.cfg["db"]["app"], "sportsbook-app", session, self.timeout)
                    surge = self.surge_active()
                    outcome = fn(conn, self.book, surge, self.metrics) if with_metrics else fn(conn, self.book, surge)
                    result = outcome or "ok"
                    break
                except Outcome as exc:
                    result = exc.kind
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
                    if result == "deadlock" and attempt == 1:
                        continue  # one retry, like most applications
                    break
            self.metrics.tx.labels(tx, result).inc()
            self.metrics.latency.labels(tx).observe(time.monotonic() - queued_at)

    def refresher(self):
        """Keeps the application's cache of matches and odds fresh (every 3 s), as sb_app."""
        conn = None
        while not self.stop.is_set():
            try:
                if conn is None:
                    conn = connect(self.cfg["db"]["app"], "sportsbook-cache", self.cfg.get("session", {}).get("app"), self.timeout)
                self.book.refresh(conn)
            except Exception as exc:
                LOG.warning("cache refresh: %s", short_error(exc))
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            self.stop.wait(3)

    def scheduler(self):
        """Starts the batch jobs and reports on their schedules; updates the gauges."""
        start = time.monotonic()
        next_run = {job: start + float(s.get("first", s.get("every", 600))) for job, s in self.cfg["jobs"].items()}
        while not self.stop.is_set():
            now = time.monotonic()
            for job, settings in self.cfg["jobs"].items():
                if settings.get("every") and now >= next_run[job] and job in self.jobs.run_fn:
                    self.jobs.start(job)  # skipped while the previous run is still going
                    next_run[job] = now + float(settings["every"])
            self.metrics.queue_depth.set(self.requests.qsize())
            self.metrics.base_rate.set(self.base_rate)
            self.metrics.surge.set(1 if self.surge_active() else 0)
            self.metrics.paused.set(1 if self.paused else 0)
            self.stop.wait(1)

    def status(self):
        return {
            "base_rate": self.base_rate,
            "target_rate": round(self.target_rate(), 1),
            "paused": self.paused,
            "surge_seconds_left": max(0, round(self.surge_until - time.time())),
            "queued_requests": self.requests.qsize(),
            "live_matches": len(self.book.live),
            "settlement_mode": self.cfg["jobs"].get("settlement", {}).get("mode", "single_transaction"),
            "running_jobs": sorted(self.jobs.active),
            "jobs": sorted(self.jobs.run_fn),
        }


# ---- control endpoint (127.0.0.1 only) ------------------------------------------------------------


def control_server(load, feed, port):
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
                self._reply(200, load.status())
            else:
                self._reply(404, {"error": "unknown path"})

        def do_POST(self):  # noqa: N802
            parts = [p for p in self.path.split("/") if p]
            action, value = (parts + [None, None])[:2]
            if action == "rate" and value:
                load.base_rate = max(0.0, float(value))
                LOG.info("base rate set to %.1f requests/s", load.base_rate)
            elif action == "pause":
                load.paused = True
            elif action == "resume":
                load.paused = False
            elif action == "kickoff":
                feed.kickoff_count = int(value or feed.kickoff_count)
                feed.kickoff_now.set()
            elif action == "settlement-mode" and value in ("single_transaction", "batched"):
                load.cfg["jobs"].setdefault("settlement", {})["mode"] = value
                LOG.info("settlement mode: %s", value)
            elif action == "run" and value in load.jobs.run_fn:
                if not load.jobs.start(value):
                    return self._reply(409, {"error": f"{value} is already running"})
            else:
                return self._reply(400, {
                    "error": "use: rate N | pause | resume | kickoff [MATCHES] | settlement-mode single_transaction|batched | run JOB",
                    "jobs": sorted(load.jobs.run_fn)})
            self._reply(200, load.status())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="control", daemon=True).start()


# ---- entry point ----------------------------------------------------------------------------------


def run(cfg, stop):
    from prometheus_client import start_http_server

    metrics = Metrics()
    start_http_server(int(cfg.get("metrics_port", 9312)), addr=cfg.get("metrics_address", "0.0.0.0"))
    annotations = Annotations(cfg["grafana"]) if cfg.get("grafana") else None
    timeout = float(cfg.get("timeout", 5.0))

    book = Book()
    while not stop.is_set():  # wait for the sportsbook database and its data (e.g. right after a deploy)
        try:
            with connect(cfg["db"]["app"], "sportsbook-app", cfg.get("session", {}).get("app"), timeout) as conn:
                book.load_static(conn)
                book.refresh(conn)
            break
        except Exception as exc:
            LOG.warning("sportsbook database not ready: %s", short_error(exc))
            stop.wait(5)
    if stop.is_set():
        return

    jobs = Jobs(cfg, metrics, annotations, stop)
    load = Load(cfg, book, metrics, jobs, annotations, stop)
    feed = Feed(cfg, book, metrics, load, annotations, stop)
    control_server(load, feed, int(cfg.get("control_port", 9311)))
    workers = int(cfg.get("workers", 32))
    for i in range(workers):
        threading.Thread(target=load.worker, name=f"worker-{i}", daemon=True).start()
    for name, target in (("arrivals", load.arrivals), ("scheduler", load.scheduler),
                         ("cache", load.refresher), ("feed", feed.run)):
        threading.Thread(target=target, name=name, daemon=True).start()
    LOG.info("sportsbook workload: %.0f requests/s base, %d workers, %d players, %d upcoming and %d live matches",
             load.base_rate, workers, book.players_n, len(book.upcoming), len(book.live))
    while not stop.is_set():
        stop.wait(1)
    LOG.info("stopped")
