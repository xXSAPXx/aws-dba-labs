--liquibase formatted sql

--changeset lab:2003-place-order-function runOnChange:true splitStatements:false
--comment: The whole checkout in one call. The application sends the order, the database does the rest: one round trip instead of about ten, and the stock rows stay locked for less time.
CREATE OR REPLACE FUNCTION shop.place_order(
    p_customer_id     integer,
    p_warehouse_id    smallint,
    p_items           jsonb,  -- [{"product_id": 12, "quantity": 2}, ...] in the order the customer added them
    p_payment_method  text,
    p_provider_ref    text    -- the payment provider's reference for the pending payment
) RETURNS bigint              -- the new order's id
LANGUAGE plpgsql
AS $$
DECLARE
    v_item      jsonb;
    v_total     numeric(12,2);
    v_order_id  bigint;
BEGIN
    -- Take the stock in the order the customer added the products, not sorted, like the application does:
    -- two checkouts of the same products in a different order can deadlock.
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        UPDATE shop.inventory
           SET quantity = quantity - (v_item->>'quantity')::integer
         WHERE warehouse_id = p_warehouse_id
           AND product_id = (v_item->>'product_id')::bigint
           AND quantity >= (v_item->>'quantity')::integer;
        IF NOT FOUND THEN
            -- Its own error code: the application counts it as "out of stock", a business outcome, not an error.
            -- The exception rolls back everything this call did.
            RAISE EXCEPTION 'product % is out of stock in warehouse %', v_item->>'product_id', p_warehouse_id
                USING ERRCODE = 'SH001';
        END IF;
    END LOOP;

    SELECT coalesce(sum(p.price * i.quantity), 0) INTO v_total
      FROM jsonb_to_recordset(p_items) AS i (product_id bigint, quantity integer)
      JOIN shop.products p ON p.id = i.product_id;

    INSERT INTO shop.orders (customer_id, shipping_address_id, status, total_amount)
    VALUES (p_customer_id,
            (SELECT id FROM shop.customer_addresses
              WHERE customer_id = p_customer_id AND kind = 'shipping' ORDER BY id LIMIT 1),
            'pending', v_total)
    RETURNING id INTO v_order_id;
    -- order_status_history: written by the trigger on orders (2002)

    INSERT INTO shop.order_items (order_id, product_id, quantity, unit_price)
    SELECT v_order_id, i.product_id, i.quantity, p.price
      FROM jsonb_to_recordset(p_items) AS i (product_id bigint, quantity integer)
      JOIN shop.products p ON p.id = i.product_id;

    INSERT INTO shop.stock_movements (warehouse_id, product_id, quantity, reason, order_id)
    SELECT p_warehouse_id, i.product_id, -i.quantity, 'sale', v_order_id
      FROM jsonb_to_recordset(p_items) AS i (product_id bigint, quantity integer);

    INSERT INTO shop.payments (order_id, amount, method, status, provider_ref)
    VALUES (v_order_id, v_total, p_payment_method, 'pending', p_provider_ref);

    UPDATE shop.carts SET status = 'converted'
     WHERE customer_id = p_customer_id AND status = 'open';

    INSERT INTO shop.audit_log (actor, action, entity, entity_id, details)
    VALUES ('customer:' || p_customer_id, 'checkout', 'order', v_order_id,
            jsonb_build_object('items', jsonb_array_length(p_items), 'total', v_total::text));

    RETURN v_order_id;
END
$$;
--rollback DROP FUNCTION shop.place_order(integer, smallint, jsonb, text, text);

--changeset lab:2003-place-order-access
--comment: Functions can be called by everyone (PUBLIC) unless revoked: only the application may place orders. It runs with the caller's own rights (SECURITY INVOKER, the default).
REVOKE EXECUTE ON FUNCTION shop.place_order(integer, smallint, jsonb, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION shop.place_order(integer, smallint, jsonb, text, text) TO shop_app;
--rollback REVOKE EXECUTE ON FUNCTION shop.place_order(integer, smallint, jsonb, text, text) FROM shop_app;
--rollback GRANT EXECUTE ON FUNCTION shop.place_order(integer, smallint, jsonb, text, text) TO PUBLIC;
