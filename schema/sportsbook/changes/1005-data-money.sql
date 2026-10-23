--liquibase formatted sql

-- The money, consistent like a real ledger:
--   every player's first deposit covers all their stakes, so no running balance ever goes below zero;
--   balance_after is the running balance of the player's account at each row;
--   every wallet holds exactly what its ledger adds up to.

--changeset lab:1005-data-deposits context:data
--comment: Every player's first deposit, a day after sign-up: enough for all their stakes, plus 50 to 500.
INSERT INTO payments (player_id, kind, method, amount, status, provider_ref, created_at, updated_at)
SELECT p.id, 'deposit',
       ELT(1 + p.id % 4, 'card', 'card', 'ewallet', 'bank_transfer'),
       COALESCE(t.stakes, 0) + 50 + (p.id * 7919) % 451,
       'approved',
       CONCAT('PSP-', LPAD(p.id, 12, '0')),
       p.created_at + INTERVAL 1 DAY, p.created_at + INTERVAL 1 DAY
FROM players p
LEFT JOIN (SELECT player_id, SUM(stake) AS stakes FROM bets GROUP BY player_id) t ON t.player_id = p.id
ORDER BY p.id;
--rollback DELETE FROM payments;

--changeset lab:1005-data-ledger context:data
--comment: The ledger rows of the deposits, stakes and wins (balance_after is filled in below).
INSERT INTO wallet_transactions (player_id, type, account, amount, balance_after, reference_type, reference_id, idempotency_key, created_at)
SELECT player_id, 'deposit', 'cash', amount, 0, 'payment', id, UUID(), created_at
FROM payments WHERE kind = 'deposit';
INSERT INTO wallet_transactions (player_id, type, account, amount, balance_after, reference_type, reference_id, idempotency_key, created_at)
SELECT player_id, 'stake', 'cash', -stake, 0, 'bet', id, UUID(), placed_at
FROM bets;
INSERT INTO wallet_transactions (player_id, type, account, amount, balance_after, reference_type, reference_id, idempotency_key, created_at)
SELECT player_id, 'win', 'cash', payout, 0, 'bet', id, UUID(), settled_at
FROM bets WHERE status = 'won';
--rollback DELETE FROM wallet_transactions;

--changeset lab:1005-data-withdrawals context:data
--comment: Players with more than 300 left withdrew half of it an hour ago (paid).
INSERT INTO payments (player_id, kind, method, amount, status, provider_ref, created_at, updated_at)
SELECT w.player_id, 'withdrawal', 'bank_transfer', ROUND(w.balance / 2, 2), 'paid',
       CONCAT('PSP-W-', LPAD(w.player_id, 12, '0')),
       NOW(3) - INTERVAL 1 HOUR, NOW(3) - INTERVAL 1 HOUR
FROM (SELECT player_id, SUM(amount) AS balance FROM wallet_transactions GROUP BY player_id) w
WHERE w.balance > 300;
INSERT INTO wallet_transactions (player_id, type, account, amount, balance_after, reference_type, reference_id, idempotency_key, created_at)
SELECT player_id, 'withdrawal', 'cash', -amount, 0, 'payment', id, UUID(), created_at
FROM payments WHERE kind = 'withdrawal';
--rollback DELETE FROM payments WHERE kind = 'withdrawal';

--changeset lab:1005-data-ledger-balances context:data
--comment: balance_after: the running balance per player and account, in the order the rows were written.
UPDATE wallet_transactions wt
JOIN (SELECT id, SUM(amount) OVER (PARTITION BY player_id, account ORDER BY created_at, id) AS running
      FROM wallet_transactions) r ON r.id = wt.id
SET wt.balance_after = r.running;
--rollback UPDATE wallet_transactions SET balance_after = 0;

--changeset lab:1005-data-wallets context:data
--comment: Every wallet holds what its ledger adds up to.
INSERT INTO wallets (player_id, balance, bonus_balance, currency)
SELECT p.id, COALESCE(SUM(wt.amount), 0), 0, 'EUR'
FROM players p
LEFT JOIN wallet_transactions wt ON wt.player_id = p.id AND wt.account = 'cash'
GROUP BY p.id;
--rollback DELETE FROM wallets;

--changeset lab:1005-data-promotions context:data
INSERT INTO promotions (id, name, kind, amount, min_turnover, active) VALUES
    (1, 'Hourly mission: stake 50 within the hour, get 5', 'mission', 5.00, 50.00, TRUE),
    (2, 'Welcome bonus: 100% of the first deposit, up to 100', 'deposit_match', 100.00, 0.00, TRUE),
    (3, 'Free bet Friday', 'free_bet', 10.00, 0.00, TRUE);
--rollback DELETE FROM promotions;
