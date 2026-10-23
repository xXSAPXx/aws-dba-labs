--liquibase formatted sql

--changeset lab:0005-bets
--comment: A single (one selection) or an accumulator (2-4 selections, all must win; the odds multiply).
-- open -> won | lost (settlement) | cashed_out (the player took an early payout).
CREATE TABLE bets (
    id                BIGINT        NOT NULL AUTO_INCREMENT,
    player_id         BIGINT        NOT NULL,
    type              ENUM('single', 'accumulator') NOT NULL,
    stake             DECIMAL(12,2) NOT NULL,
    total_odds        DECIMAL(10,3) NOT NULL,
    potential_payout  DECIMAL(14,2) NOT NULL,
    status            ENUM('open', 'won', 'lost', 'void', 'cashed_out') NOT NULL DEFAULT 'open',
    payout            DECIMAL(14,2) NULL,
    channel           ENUM('web', 'mobile', 'app') NOT NULL,
    placed_at         DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    settled_at        DATETIME(3)   NULL,
    PRIMARY KEY (id),
    KEY ix_bets_player_placed (player_id, placed_at),
    KEY ix_bets_status_placed (status, placed_at),
    KEY ix_bets_placed (placed_at),
    CONSTRAINT fk_bets_player FOREIGN KEY (player_id) REFERENCES players (id),
    CONSTRAINT ck_bets_stake CHECK (stake > 0)
);
--rollback DROP TABLE bets;

--changeset lab:0005-bet-selections
--comment: The selections of a bet, with the odds the player got. event_id is a copy of the selection's event,
-- so settlement finds an event's bets with one index (ix_bet_selections_event).
CREATE TABLE bet_selections (
    bet_id        BIGINT       NOT NULL,
    selection_id  BIGINT       NOT NULL,
    event_id      BIGINT       NOT NULL,
    odds          DECIMAL(7,3) NOT NULL,
    PRIMARY KEY (bet_id, selection_id),
    KEY ix_bet_selections_event (event_id, bet_id),
    KEY ix_bet_selections_selection (selection_id),
    CONSTRAINT fk_bet_selections_bet FOREIGN KEY (bet_id) REFERENCES bets (id),
    CONSTRAINT fk_bet_selections_selection FOREIGN KEY (selection_id) REFERENCES selections (id)
);
--rollback DROP TABLE bet_selections;
