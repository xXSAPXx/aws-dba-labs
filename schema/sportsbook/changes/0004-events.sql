--liquibase formatted sql

--changeset lab:0004-events
--comment: Matches. scheduled -> live (kick-off) -> finished (full time, results known) -> settled (bets paid).
CREATE TABLE events (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    competition_id  INT         NOT NULL,
    home_team_id    INT         NOT NULL,
    away_team_id    INT         NOT NULL,
    starts_at       DATETIME(3) NOT NULL,
    status          ENUM('scheduled', 'live', 'finished', 'settled', 'cancelled') NOT NULL DEFAULT 'scheduled',
    home_score      TINYINT     NOT NULL DEFAULT 0,
    away_score      TINYINT     NOT NULL DEFAULT 0,
    kicked_off_at   DATETIME(3) NULL,
    finished_at     DATETIME(3) NULL,
    settled_at      DATETIME(3) NULL,
    updated_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY ix_events_status_starts (status, starts_at),
    KEY ix_events_competition_starts (competition_id, starts_at),
    CONSTRAINT fk_events_competition FOREIGN KEY (competition_id) REFERENCES competitions (id),
    CONSTRAINT fk_events_home_team FOREIGN KEY (home_team_id) REFERENCES teams (id),
    CONSTRAINT fk_events_away_team FOREIGN KEY (away_team_id) REFERENCES teams (id)
);
--rollback DROP TABLE events;

--changeset lab:0004-markets
--comment: What can be bet on per match. suspended: no bets for a moment (a goal, a red card).
CREATE TABLE markets (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    event_id    BIGINT      NOT NULL,
    type        ENUM('match_result', 'over_under_2_5', 'both_teams_score') NOT NULL,
    status      ENUM('open', 'suspended', 'closed', 'settled') NOT NULL DEFAULT 'open',
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_markets_event_type (event_id, type),
    CONSTRAINT fk_markets_event FOREIGN KEY (event_id) REFERENCES events (id)
);
--rollback DROP TABLE markets;

--changeset lab:0004-selections
--comment: The outcomes of a market and their current odds (decimal: a 10 stake at 2.50 returns 25).
-- The odds feed updates them constantly: hot rows while a match is live.
CREATE TABLE selections (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    market_id   BIGINT       NOT NULL,
    name        ENUM('home', 'draw', 'away', 'over', 'under', 'yes', 'no') NOT NULL,
    odds        DECIMAL(7,3) NOT NULL,
    result      ENUM('pending', 'won', 'lost', 'void') NOT NULL DEFAULT 'pending',
    updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_selections_market_name (market_id, name),
    CONSTRAINT fk_selections_market FOREIGN KEY (market_id) REFERENCES markets (id),
    CONSTRAINT ck_selections_odds CHECK (odds >= 1.01)
);
--rollback DROP TABLE selections;

--changeset lab:0004-odds-history
--comment: Every odds change of the feed, for disputes ("what were the odds when I bet?"). Insert-only, high rate,
-- so no foreign key.
CREATE TABLE odds_history (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    selection_id  BIGINT       NOT NULL,
    odds          DECIMAL(7,3) NOT NULL,
    changed_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY ix_odds_history_selection (selection_id, changed_at)
);
--rollback DROP TABLE odds_history;
