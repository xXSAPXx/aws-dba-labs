--liquibase formatted sql

--changeset lab:1099-data-finish context:data
--comment: Remove the helper tables, then fresh statistics for the optimizer.
DROP TABLE gen_legs;
DROP TABLE seq;
ANALYZE TABLE players, player_limits, wallets, wallet_transactions, payments, events, markets, selections, bets, bet_selections;
--rollback SELECT 1;
