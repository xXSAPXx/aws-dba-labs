--liquibase formatted sql

-- Least privilege, versioned with the schema: each part of the application has a MySQL role, and the
-- privileges are granted to the roles here. Ansible creates the roles before the migrations run, then the
-- users and their memberships (sb_app is a member of sportsbook_app, ...), and makes each user's roles its
-- default roles (SET DEFAULT ROLE ALL): a user has its role's privileges as soon as it logs in.

--changeset lab:0008-access-app
--comment: The players' traffic (sb_app): browse, log in, bet, cash out, deposit, withdraw, sign up.
GRANT SELECT ON sportsbook.countries TO sportsbook_app;
GRANT SELECT ON sportsbook.sports TO sportsbook_app;
GRANT SELECT ON sportsbook.competitions TO sportsbook_app;
GRANT SELECT ON sportsbook.teams TO sportsbook_app;
GRANT SELECT ON sportsbook.events TO sportsbook_app;
GRANT SELECT ON sportsbook.markets TO sportsbook_app;
GRANT SELECT ON sportsbook.selections TO sportsbook_app;
GRANT SELECT ON sportsbook.promotions TO sportsbook_app;
GRANT SELECT ON sportsbook.leaderboard TO sportsbook_app;
GRANT SELECT, INSERT, UPDATE ON sportsbook.players TO sportsbook_app;
GRANT SELECT, INSERT, UPDATE ON sportsbook.player_limits TO sportsbook_app;
GRANT SELECT, INSERT, UPDATE ON sportsbook.wallets TO sportsbook_app;
GRANT SELECT, INSERT, UPDATE ON sportsbook.bets TO sportsbook_app;
GRANT SELECT, INSERT, UPDATE ON sportsbook.payments TO sportsbook_app;
GRANT SELECT, INSERT ON sportsbook.bet_selections TO sportsbook_app;
GRANT SELECT, INSERT ON sportsbook.wallet_transactions TO sportsbook_app;
GRANT SELECT, INSERT, DELETE ON sportsbook.player_sessions TO sportsbook_app;
GRANT INSERT ON sportsbook.audit_log TO sportsbook_app;
--rollback REVOKE ALL PRIVILEGES, GRANT OPTION FROM sportsbook_app;

--changeset lab:0008-access-feed
--comment: The odds feed (sb_feed): match states, scores, odds, market suspensions, new fixtures.
GRANT SELECT ON sportsbook.countries TO sportsbook_feed;
GRANT SELECT ON sportsbook.sports TO sportsbook_feed;
GRANT SELECT ON sportsbook.competitions TO sportsbook_feed;
GRANT SELECT ON sportsbook.teams TO sportsbook_feed;
GRANT SELECT, INSERT, UPDATE ON sportsbook.events TO sportsbook_feed;
GRANT SELECT, INSERT, UPDATE ON sportsbook.markets TO sportsbook_feed;
GRANT SELECT, INSERT, UPDATE ON sportsbook.selections TO sportsbook_feed;
GRANT INSERT ON sportsbook.odds_history TO sportsbook_feed;
--rollback REVOKE ALL PRIVILEGES, GRANT OPTION FROM sportsbook_feed;

--changeset lab:0008-access-batch
--comment: Batch jobs (sb_batch): settlement, withdrawals, bonuses, the leaderboard, clean-ups.
GRANT SELECT ON sportsbook.* TO sportsbook_batch;
GRANT UPDATE ON sportsbook.bets TO sportsbook_batch;
GRANT UPDATE ON sportsbook.wallets TO sportsbook_batch;
GRANT UPDATE ON sportsbook.payments TO sportsbook_batch;
GRANT UPDATE ON sportsbook.events TO sportsbook_batch;
GRANT UPDATE ON sportsbook.markets TO sportsbook_batch;
GRANT INSERT ON sportsbook.wallet_transactions TO sportsbook_batch;
GRANT INSERT ON sportsbook.audit_log TO sportsbook_batch;
GRANT INSERT ON sportsbook.player_promotions TO sportsbook_batch;
GRANT INSERT, UPDATE, DELETE ON sportsbook.leaderboard TO sportsbook_batch;
GRANT DELETE ON sportsbook.player_sessions TO sportsbook_batch;
GRANT DELETE ON sportsbook.odds_history TO sportsbook_batch;
--rollback REVOKE ALL PRIVILEGES, GRANT OPTION FROM sportsbook_batch;

--changeset lab:0008-access-reporting
--comment: Reports for the regulator and the business (sb_reporting): read only.
GRANT SELECT ON sportsbook.* TO sportsbook_reporting;
--rollback REVOKE ALL PRIVILEGES, GRANT OPTION FROM sportsbook_reporting;

--changeset lab:0008-access-support
--comment: Customer support (sb_support): reads everything, may change a player's status and limits.
GRANT SELECT ON sportsbook.* TO sportsbook_support;
GRANT UPDATE ON sportsbook.players TO sportsbook_support;
GRANT UPDATE ON sportsbook.player_limits TO sportsbook_support;
--rollback REVOKE ALL PRIVILEGES, GRANT OPTION FROM sportsbook_support;
