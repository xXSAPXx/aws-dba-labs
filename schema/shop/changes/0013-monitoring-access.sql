--liquibase formatted sql

--changeset lab:0013-monitoring-access
--comment: pgwatch (created by Ansible, like every login): reads statistics only, through pg_monitor. It connects to the shop database for the statistics of its tables and indexes.
GRANT CONNECT ON DATABASE shop TO pgwatch;
--rollback REVOKE CONNECT ON DATABASE shop FROM pgwatch;
