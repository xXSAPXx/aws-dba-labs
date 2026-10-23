--liquibase formatted sql

--changeset lab:0002-players
--comment: The sportsbook's customers. self_excluded: the player asked to be blocked (responsible gambling).
CREATE TABLE players (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    username          VARCHAR(40)  NOT NULL,
    email             VARCHAR(120) NOT NULL,
    country_code      CHAR(2)      NOT NULL,
    currency          CHAR(3)      NOT NULL DEFAULT 'EUR',
    status            ENUM('active', 'suspended', 'self_excluded', 'closed') NOT NULL DEFAULT 'active',
    kyc_status        ENUM('pending', 'verified', 'rejected') NOT NULL DEFAULT 'pending',
    vip_level         TINYINT      NOT NULL DEFAULT 0,
    marketing_opt_in  BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    last_login_at     DATETIME(3)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_players_username (username),
    UNIQUE KEY uq_players_email (email),
    KEY ix_players_country (country_code),
    CONSTRAINT fk_players_country FOREIGN KEY (country_code) REFERENCES countries (code)
);
--rollback DROP TABLE players;

--changeset lab:0002-player-limits
--comment: Responsible gambling limits, set by the player or the operator. NULL = no limit.
CREATE TABLE player_limits (
    player_id            BIGINT        NOT NULL,
    max_stake            DECIMAL(12,2) NOT NULL DEFAULT 500.00,
    daily_deposit_limit  DECIMAL(12,2) NULL,
    daily_loss_limit     DECIMAL(12,2) NULL,
    updated_at           DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (player_id),
    CONSTRAINT fk_player_limits_player FOREIGN KEY (player_id) REFERENCES players (id)
);
--rollback DROP TABLE player_limits;

--changeset lab:0002-player-sessions
--comment: Login sessions. High churn (every login inserts, a batch job deletes expired ones), so no foreign key.
CREATE TABLE player_sessions (
    token       CHAR(32)    NOT NULL,
    player_id   BIGINT      NOT NULL,
    channel     ENUM('web', 'mobile', 'app') NOT NULL,
    ip          VARCHAR(45) NOT NULL,
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    expires_at  DATETIME(3) NOT NULL,
    PRIMARY KEY (token),
    KEY ix_player_sessions_player (player_id),
    KEY ix_player_sessions_expires (expires_at)
);
--rollback DROP TABLE player_sessions;
