--liquibase formatted sql

-- Matches, markets and selections. The ids follow from each other, so every step can find the rows of the
-- previous one by arithmetic instead of a lookup:
--   event e: markets (e - 1) * 3 + 1 .. 3, selections (e - 1) * 7 + 1 .. 7 (home, draw, away, over, under, yes, no).
-- Values that several columns share (a match's kick-off, its odds) come from a hash of the id, not RAND(), so they agree.

--changeset lab:1003-data-events-past context:data
--comment: 4,000 settled matches per scale unit over the last 30 days (ids from 1), on 15-minute kick-off slots.
INSERT INTO events (id, competition_id, home_team_id, away_team_id, starts_at, status, home_score, away_score,
                    kicked_off_at, finished_at, settled_at)
SELECT g.id, g.competition_id,
       (g.competition_id - 1) * 20 + 1 + g.home_ix,
       (g.competition_id - 1) * 20 + 1 + (g.home_ix + 1 + g.id % 19) % 20,
       g.starts_at, 'settled',
       FLOOR(POW(RAND(), 1.7) * 6), FLOOR(POW(RAND(), 2) * 5),
       g.starts_at, g.starts_at + INTERVAL 110 MINUTE, g.starts_at + INTERVAL 115 MINUTE
FROM (SELECT s.n AS id,
             1 + s.n % 12 AS competition_id,
             (s.n * 7) % 20 AS home_ix,
             DATE_FORMAT(NOW(), '%Y-%m-%d %H:00:00') - INTERVAL 1 DAY - INTERVAL ((s.n * 7919) % 2784) * 15 MINUTE AS starts_at
      FROM seq s
      WHERE s.n <= 4000 * ${sportsbook_scale}) g;
--rollback DELETE FROM events;

--changeset lab:1003-data-events-upcoming context:data
--comment: 1,000 scheduled matches over the next 7 days, ids after the settled ones. The odds feed kicks them off early (lab time).
INSERT INTO events (id, competition_id, home_team_id, away_team_id, starts_at, status)
SELECT p.n + s.n,
       1 + s.n % 12,
       (s.n % 12) * 20 + 1 + (s.n * 7) % 20,
       (s.n % 12) * 20 + 1 + ((s.n * 7) % 20 + 1 + s.n % 19) % 20,
       DATE_FORMAT(NOW(), '%Y-%m-%d %H:00:00') + INTERVAL s.n * 10 MINUTE,
       'scheduled'
FROM seq s
CROSS JOIN (SELECT MAX(id) AS n FROM events) p
WHERE s.n <= 1000;
--rollback DELETE FROM events WHERE status = 'scheduled';

--changeset lab:1003-data-markets context:data
--comment: Three markets per match.
INSERT INTO markets (id, event_id, type, status)
SELECT (e.id - 1) * 3 + k.n, e.id,
       ELT(k.n, 'match_result', 'over_under_2_5', 'both_teams_score'),
       IF(e.status = 'settled', 'settled', 'open')
FROM events e
JOIN seq k ON k.n <= 3
ORDER BY e.id, k.n;
--rollback DELETE FROM markets;

--changeset lab:1003-data-selections context:data
--comment: Seven outcomes per match with a 6% bookmaker margin in the odds; for settled matches, the result from the score.
INSERT INTO selections (id, market_id, name, odds, result)
SELECT (e.id - 1) * 7 + k.n,
       (e.id - 1) * 3 + CASE WHEN k.n <= 3 THEN 1 WHEN k.n <= 5 THEN 2 ELSE 3 END,
       ELT(k.n, 'home', 'draw', 'away', 'over', 'under', 'yes', 'no'),
       GREATEST(1.01, ROUND(1 / (1.06 * CASE k.n
           WHEN 1 THEN e.p_home
           WHEN 2 THEN 1 - e.p_home - e.p_away
           WHEN 3 THEN e.p_away
           WHEN 4 THEN e.p_over
           WHEN 5 THEN 1 - e.p_over
           WHEN 6 THEN e.p_yes
           ELSE 1 - e.p_yes END), 2)),
       CASE WHEN e.status <> 'settled' THEN 'pending'
            WHEN (k.n = 1 AND e.home_score > e.away_score)
              OR (k.n = 2 AND e.home_score = e.away_score)
              OR (k.n = 3 AND e.home_score < e.away_score)
              OR (k.n = 4 AND e.home_score + e.away_score > 2)
              OR (k.n = 5 AND e.home_score + e.away_score < 3)
              OR (k.n = 6 AND e.home_score > 0 AND e.away_score > 0)
              OR (k.n = 7 AND (e.home_score = 0 OR e.away_score = 0)) THEN 'won'
            ELSE 'lost' END
FROM (SELECT id, status, home_score, away_score,
             0.15 + 0.6 * ((id * 7919) % 1000) / 1000 AS p_home,
             (0.85 - 0.6 * ((id * 7919) % 1000) / 1000) * 0.62 AS p_away,
             0.35 + 0.35 * ((id * 104729) % 1000) / 1000 AS p_over,
             0.35 + 0.35 * ((id * 1299709) % 1000) / 1000 AS p_yes
      FROM events) e
JOIN seq k ON k.n <= 7
ORDER BY e.id, k.n;
--rollback DELETE FROM selections;
