--liquibase formatted sql

--changeset lab:0007-audit-log
--comment: What players and staff did (logins, sign-ups, limit changes), as the regulator requires.
CREATE TABLE audit_log (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    actor       VARCHAR(60) NOT NULL,
    action      VARCHAR(40) NOT NULL,
    entity      VARCHAR(30) NOT NULL,
    entity_id   BIGINT      NULL,
    details     JSON        NULL,
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY ix_audit_log_entity (entity, entity_id),
    KEY ix_audit_log_created (created_at)
);
--rollback DROP TABLE audit_log;
