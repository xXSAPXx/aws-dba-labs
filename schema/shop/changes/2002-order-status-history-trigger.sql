--liquibase formatted sql

--changeset lab:2002-order-status-history-function runOnChange:true splitStatements:false
--comment: The order history is kept by the database: one row per status an order gets, whoever changes it.
CREATE OR REPLACE FUNCTION shop.record_order_status() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO shop.order_status_history (order_id, from_status, to_status)
        VALUES (NEW.id, NULL, NEW.status);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN  -- an UPDATE that sets the same status writes nothing
        INSERT INTO shop.order_status_history (order_id, from_status, to_status)
        VALUES (NEW.id, OLD.status, NEW.status);
    END IF;
    RETURN NULL;  -- an AFTER trigger: the return value is ignored
END
$$;
--rollback DROP FUNCTION shop.record_order_status();

--changeset lab:2002-order-status-history-trigger runOnChange:true
--comment: UPDATE OF status: fires only for UPDATEs that set the status column.
CREATE OR REPLACE TRIGGER record_order_status AFTER INSERT OR UPDATE OF status ON shop.orders
    FOR EACH ROW EXECUTE FUNCTION shop.record_order_status();
--rollback DROP TRIGGER record_order_status ON shop.orders;
