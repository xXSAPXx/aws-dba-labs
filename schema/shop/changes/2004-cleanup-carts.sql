--liquibase formatted sql

--changeset lab:2004-cleanup-carts-procedure runOnChange:true splitStatements:false
--comment: The cart clean-up as a procedure that commits after each batch: locks are held for one batch, not for the whole job. CALL it outside a transaction block (autocommit), or it can't commit.
CREATE OR REPLACE PROCEDURE shop.cleanup_carts(
    p_batch_size      integer,
    INOUT p_abandoned bigint DEFAULT 0,  -- carts marked abandoned
    INOUT p_deleted   bigint DEFAULT 0   -- carts deleted (their items go with them: ON DELETE CASCADE)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows bigint;
BEGIN
    p_abandoned := 0;
    p_deleted := 0;

    -- Open carts untouched for 30 minutes are abandoned. SKIP LOCKED: carts a customer is changing right now wait for the next run.
    LOOP
        UPDATE shop.carts SET status = 'abandoned'
         WHERE id IN (SELECT id FROM shop.carts
                       WHERE status = 'open' AND updated_at < now() - interval '30 minutes'
                       LIMIT p_batch_size
                       FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        p_abandoned := p_abandoned + v_rows;
        COMMIT;  -- a new transaction starts here: now() moves on, the batch's locks are released
        EXIT WHEN v_rows < p_batch_size;
    END LOOP;

    -- Abandoned and converted carts are deleted 45 minutes after their last change.
    LOOP
        DELETE FROM shop.carts
         WHERE id IN (SELECT id FROM shop.carts
                       WHERE status IN ('abandoned', 'converted') AND updated_at < now() - interval '45 minutes'
                       LIMIT p_batch_size
                       FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        p_deleted := p_deleted + v_rows;
        COMMIT;
        EXIT WHEN v_rows < p_batch_size;
    END LOOP;
END
$$;
--rollback DROP PROCEDURE shop.cleanup_carts(integer, bigint, bigint);

--changeset lab:2004-cleanup-carts-access
--comment: Only the batch jobs' role may run it (procedures, like functions, are callable by PUBLIC unless revoked).
REVOKE EXECUTE ON PROCEDURE shop.cleanup_carts(integer, bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE shop.cleanup_carts(integer, bigint, bigint) TO shop_batch;
--rollback REVOKE EXECUTE ON PROCEDURE shop.cleanup_carts(integer, bigint, bigint) FROM shop_batch;
--rollback GRANT EXECUTE ON PROCEDURE shop.cleanup_carts(integer, bigint, bigint) TO PUBLIC;
