--liquibase formatted sql

--changeset lab:1004-data-bets context:data
--comment: 1,000,000 bets per scale unit on the settled matches. Most players bet little, a few bet a lot.
INSERT INTO bets (id, player_id, type, stake, total_odds, potential_payout, status, payout, channel, placed_at)
SELECT (k.n - 1) * 1000000 + s.n,
       1 + FLOOR(POW(RAND(), 2.2) * p.n),
       'single',
       ELT(1 + FLOOR(RAND() * 10), 2, 5, 5, 10, 10, 10, 20, 25, 50, 100),
       1, 1, 'open', NULL,
       ELT(1 + FLOOR(RAND() * 4), 'mobile', 'mobile', 'web', 'app'),
       NOW(3)
FROM seq s
JOIN seq k ON k.n <= ${sportsbook_scale}
CROSS JOIN (SELECT COUNT(*) AS n FROM players) p;
--rollback DELETE FROM bets;

--changeset lab:1004-data-bet-legs context:data
--comment: The match and outcome of every leg: 70% singles, the rest accumulators of 2-4 legs. A helper table, dropped at the end.
-- CREATE TABLE commits by itself: if the INSERT dies, the empty table stays, and the rerun starts by dropping it.
DROP TABLE IF EXISTS gen_legs;
CREATE TABLE gen_legs (bet_id BIGINT NOT NULL, event_id BIGINT NOT NULL, outcome TINYINT NOT NULL);
INSERT INTO gen_legs (bet_id, event_id, outcome)
SELECT b.id, 1 + FLOOR(RAND() * e.n), 1 + FLOOR(RAND() * 7)
FROM bets b
JOIN seq leg ON leg.n <= CASE WHEN b.id % 10 < 7 THEN 1 ELSE 2 + b.id % 3 END
CROSS JOIN (SELECT COUNT(*) AS n FROM events WHERE status = 'settled') e;
--rollback DROP TABLE gen_legs;

--changeset lab:1004-data-bet-selections context:data
--comment: The legs, with the odds of their selection. IGNORE drops the rare leg that repeats a selection of the same bet.
INSERT IGNORE INTO bet_selections (bet_id, selection_id, event_id, odds)
SELECT l.bet_id, s.id, l.event_id, s.odds
FROM gen_legs l
JOIN selections s ON s.id = (l.event_id - 1) * 7 + l.outcome;
--rollback DELETE FROM bet_selections;

--changeset lab:1004-data-bets-settled context:data
--comment: Every bet as settlement left it: odds multiplied, won only if every leg won, placed up to 2 days before its first match.
UPDATE bets b
JOIN (SELECT bs.bet_id,
             COUNT(*) AS legs,
             LEAST(1000, EXP(SUM(LN(bs.odds)))) AS total_odds,
             SUM(s.result = 'lost') AS lost,
             MIN(e.starts_at) AS first_start,
             MAX(e.finished_at) AS last_finish
      FROM bet_selections bs
      JOIN selections s ON s.id = bs.selection_id
      JOIN events e ON e.id = bs.event_id
      GROUP BY bs.bet_id) x ON x.bet_id = b.id
SET b.type = IF(x.legs = 1, 'single', 'accumulator'),
    b.total_odds = ROUND(x.total_odds, 3),
    b.potential_payout = ROUND(b.stake * x.total_odds, 2),
    b.status = IF(x.lost > 0, 'lost', 'won'),
    b.payout = IF(x.lost > 0, 0, ROUND(b.stake * x.total_odds, 2)),
    b.placed_at = x.first_start - INTERVAL (1 + (b.id * 7919) % 2880) MINUTE,
    b.settled_at = x.last_finish + INTERVAL 5 MINUTE;
--rollback UPDATE bets SET status = 'open', payout = NULL, settled_at = NULL;
