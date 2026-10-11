--liquibase formatted sql

--changeset lab:0011-support-access
--comment: shop_support (created by Ansible, like every login): support staff who look up anything and fix customer records by hand.
GRANT CONNECT ON DATABASE shop TO shop_support;
GRANT USAGE ON SCHEMA shop TO shop_support;
GRANT SELECT ON ALL TABLES IN SCHEMA shop TO shop_support;
ALTER DEFAULT PRIVILEGES IN SCHEMA shop GRANT SELECT ON TABLES TO shop_support;
GRANT UPDATE ON shop.customers, shop.customer_addresses TO shop_support;
--rollback REVOKE UPDATE ON shop.customers, shop.customer_addresses FROM shop_support;
--rollback ALTER DEFAULT PRIVILEGES IN SCHEMA shop REVOKE SELECT ON TABLES FROM shop_support;
--rollback REVOKE SELECT ON ALL TABLES IN SCHEMA shop FROM shop_support;
--rollback REVOKE USAGE ON SCHEMA shop FROM shop_support;
--rollback REVOKE CONNECT ON DATABASE shop FROM shop_support;
