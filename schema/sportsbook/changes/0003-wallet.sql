--liquibase formatted sql

--changeset lab:0003-wallets
--comment: One wallet per player: the cash balance and the bonus balance. A bet, a win, a deposit or a bonus
-- changes it in the same transaction as its ledger row. The CHECK is the last line of defence.
CREATE TABLE wallets (
    player_id      BIGINT        NOT NULL,
    balance        DECIMAL(14,2) NOT NULL DEFAULT 0.00,
    bonus_balance  DECIMAL(14,2) NOT NULL DEFAULT 0.00,
    currency       CHAR(3)       NOT NULL DEFAULT 'EUR',
    updated_at     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (player_id),
    CONSTRAINT fk_wallets_player FOREIGN KEY (player_id) REFERENCES players (id),
    CONSTRAINT ck_wallets_balance CHECK (balance >= 0 AND bonus_balance >= 0)
);
--rollback DROP TABLE wallets;

--changeset lab:0003-wallet-transactions
--comment: The ledger: one row per money movement, never updated. amount is + to the player, - from the player.
-- For every player and account: the wallet balance = the sum of the ledger's amounts.
-- idempotency_key: one per request, so a retried request can't move money twice.
CREATE TABLE wallet_transactions (
    id               BIGINT        NOT NULL AUTO_INCREMENT,
    player_id        BIGINT        NOT NULL,
    type             ENUM('deposit', 'withdrawal', 'withdrawal_reversal', 'stake', 'win', 'cashout', 'bonus') NOT NULL,
    account          ENUM('cash', 'bonus') NOT NULL DEFAULT 'cash',
    amount           DECIMAL(14,2) NOT NULL,
    balance_after    DECIMAL(14,2) NOT NULL,
    reference_type   ENUM('bet', 'payment', 'promotion') NOT NULL,
    reference_id     BIGINT        NOT NULL,
    idempotency_key  CHAR(36)      NOT NULL,
    created_at       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_wallet_transactions_idempotency (idempotency_key),
    KEY ix_wallet_transactions_player (player_id, created_at),
    KEY ix_wallet_transactions_reference (reference_type, reference_id),
    CONSTRAINT fk_wallet_transactions_player FOREIGN KEY (player_id) REFERENCES players (id)
);
--rollback DROP TABLE wallet_transactions;

--changeset lab:0003-payments
--comment: Deposits and withdrawals through payment providers. Deposit: pending -> approved | declined.
-- Withdrawal: pending (money already taken from the wallet) -> paid | declined (money back).
CREATE TABLE payments (
    id            BIGINT        NOT NULL AUTO_INCREMENT,
    player_id     BIGINT        NOT NULL,
    kind          ENUM('deposit', 'withdrawal') NOT NULL,
    method        ENUM('card', 'bank_transfer', 'ewallet', 'crypto') NOT NULL,
    amount        DECIMAL(12,2) NOT NULL,
    status        ENUM('pending', 'approved', 'declined', 'paid') NOT NULL DEFAULT 'pending',
    provider_ref  VARCHAR(40)   NULL,
    created_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY ix_payments_player (player_id, created_at),
    KEY ix_payments_queue (kind, status, created_at),
    CONSTRAINT fk_payments_player FOREIGN KEY (player_id) REFERENCES players (id)
);
--rollback DROP TABLE payments;
