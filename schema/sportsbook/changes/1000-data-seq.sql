--liquibase formatted sql

--changeset lab:1000-data-seq context:data
--comment: A helper table of the numbers 1 to 1,000,000 to generate rows from (MySQL has no generate_series). Dropped at the end (1099).
CREATE TABLE seq_digits (d TINYINT NOT NULL, PRIMARY KEY (d));
INSERT INTO seq_digits (d) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9);
CREATE TABLE seq (n INT NOT NULL, PRIMARY KEY (n));
INSERT INTO seq (n)
SELECT 1 + a.d + 10 * b.d + 100 * c.d + 1000 * d.d + 10000 * e.d + 100000 * f.d
FROM seq_digits a, seq_digits b, seq_digits c, seq_digits d, seq_digits e, seq_digits f;
DROP TABLE seq_digits;
--rollback DROP TABLE seq;
