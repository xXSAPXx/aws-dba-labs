--liquibase formatted sql

-- Database code (2xxx): functions, procedures and triggers. Numbered after the data (1xxx), so a new lab
-- loads its generated data first: a trigger that existed during the load would rewrite the generated
-- timestamps and history. On a lab that already runs, these are simply the next migrations.
-- The code changesets below run on change: edit the code in a file, and the next migration run deploys
-- the new version. A function body contains semicolons, so those changesets are not split into
-- statements, and each of them holds exactly one statement.

--changeset lab:2001-set-updated-at-function runOnChange:true splitStatements:false
--comment: updated_at is kept by the database: every UPDATE of a row sets it, whoever runs the UPDATE.
CREATE OR REPLACE FUNCTION shop.set_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();  -- the transaction's start time, like now() in the UPDATE itself
    RETURN NEW;
END
$$;
--rollback DROP FUNCTION shop.set_updated_at();

--changeset lab:2001-set-updated-at-triggers runOnChange:true
--comment: The four tables the application updates most. inventory is the hottest: every checkout pays for this trigger.
CREATE OR REPLACE TRIGGER set_updated_at BEFORE UPDATE ON shop.orders FOR EACH ROW EXECUTE FUNCTION shop.set_updated_at();
CREATE OR REPLACE TRIGGER set_updated_at BEFORE UPDATE ON shop.payments FOR EACH ROW EXECUTE FUNCTION shop.set_updated_at();
CREATE OR REPLACE TRIGGER set_updated_at BEFORE UPDATE ON shop.carts FOR EACH ROW EXECUTE FUNCTION shop.set_updated_at();
CREATE OR REPLACE TRIGGER set_updated_at BEFORE UPDATE ON shop.inventory FOR EACH ROW EXECUTE FUNCTION shop.set_updated_at();
--rollback DROP TRIGGER set_updated_at ON shop.inventory;
--rollback DROP TRIGGER set_updated_at ON shop.carts;
--rollback DROP TRIGGER set_updated_at ON shop.payments;
--rollback DROP TRIGGER set_updated_at ON shop.orders;
