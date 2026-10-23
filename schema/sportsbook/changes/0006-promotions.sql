--liquibase formatted sql

--changeset lab:0006-promotions
--comment: Bonuses. mission: stake at least min_turnover within an hour and get amount on the bonus balance.
CREATE TABLE promotions (
    id            INT           NOT NULL AUTO_INCREMENT,
    name          VARCHAR(80)   NOT NULL,
    kind          ENUM('free_bet', 'deposit_match', 'mission') NOT NULL,
    amount        DECIMAL(10,2) NOT NULL,
    min_turnover  DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    active        BOOLEAN       NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id)
);
--rollback DROP TABLE promotions;

--changeset lab:0006-player-promotions
--comment: Who got which promotion, once per period (the hour of a mission).
CREATE TABLE player_promotions (
    player_id     BIGINT      NOT NULL,
    promotion_id  INT         NOT NULL,
    period        DATETIME    NOT NULL,
    awarded_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (player_id, promotion_id, period),
    KEY ix_player_promotions_promotion (promotion_id, period),
    CONSTRAINT fk_player_promotions_player FOREIGN KEY (player_id) REFERENCES players (id),
    CONSTRAINT fk_player_promotions_promotion FOREIGN KEY (promotion_id) REFERENCES promotions (id)
);
--rollback DROP TABLE player_promotions;

--changeset lab:0006-leaderboard
--comment: The weekly top 1,000 by points, rebuilt by a batch job. Later a natural fit for a Redis sorted set.
CREATE TABLE leaderboard (
    period      DATE        NOT NULL,
    player_id   BIGINT      NOT NULL,
    points      INT         NOT NULL,
    position    INT         NOT NULL,
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (period, player_id),
    KEY ix_leaderboard_position (period, position)
);
--rollback DROP TABLE leaderboard;
