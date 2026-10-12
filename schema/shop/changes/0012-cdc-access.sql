--liquibase formatted sql

--changeset lab:0012-cdc-access
--comment: debezium (created by Ansible, like every login): the change data capture connector. It reads the order tables once for its first copy, then follows their changes in the WAL through a logical replication slot.
GRANT CONNECT ON DATABASE shop TO debezium;
GRANT USAGE ON SCHEMA shop TO debezium;
GRANT SELECT ON shop.orders, shop.order_items, shop.payments TO debezium;
--rollback REVOKE SELECT ON shop.orders, shop.order_items, shop.payments FROM debezium;
--rollback REVOKE USAGE ON SCHEMA shop FROM debezium;
--rollback REVOKE CONNECT ON DATABASE shop FROM debezium;
