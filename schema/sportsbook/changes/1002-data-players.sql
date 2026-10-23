--liquibase formatted sql

--changeset lab:1002-data-players context:data
--comment: 100,000 players per scale unit, ids from 1 (username player<id>), signed up 35 days to 3 years ago (before any bet).
INSERT INTO players (id, username, email, country_code, status, kyc_status, vip_level, marketing_opt_in, created_at)
SELECT s.n,
       CONCAT('player', s.n),
       CONCAT('player', s.n, '@example.com'),
       ELT(1 + FLOOR(POW(RAND(), 1.5) * 13), 'GB', 'DE', 'ES', 'IT', 'IE', 'FR', 'NL', 'PT', 'BG', 'RO', 'GR', 'SE', 'MT'),
       CASE WHEN s.n % 101 = 0 THEN 'self_excluded' WHEN s.n % 97 = 0 THEN 'closed'
            WHEN s.n % 89 = 0 THEN 'suspended' ELSE 'active' END,
       IF(s.n % 10 = 0, 'pending', 'verified'),
       CASE WHEN s.n % 200 = 0 THEN 3 WHEN s.n % 40 = 0 THEN 2 WHEN s.n % 10 = 0 THEN 1 ELSE 0 END,
       s.n % 3 = 0,
       NOW(3) - INTERVAL 35 DAY - INTERVAL FLOOR(RAND() * 94608000) SECOND
FROM seq s
WHERE s.n <= 100000 * ${sportsbook_scale};
UPDATE players SET last_login_at = created_at + INTERVAL FLOOR(RAND() * TIMESTAMPDIFF(SECOND, created_at, NOW(3))) SECOND;
--rollback DELETE FROM players;

--changeset lab:1002-data-player-limits context:data
--comment: Maximum stake by VIP level; some players set a daily deposit or loss limit themselves.
INSERT INTO player_limits (player_id, max_stake, daily_deposit_limit, daily_loss_limit)
SELECT id,
       CASE vip_level WHEN 3 THEN 10000 WHEN 2 THEN 2500 WHEN 1 THEN 1000 ELSE 500 END,
       IF(id % 7 = 0, 1000, NULL),
       IF(id % 11 = 0, 300, NULL)
FROM players;
--rollback DELETE FROM player_limits;
