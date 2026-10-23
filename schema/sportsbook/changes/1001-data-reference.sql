--liquibase formatted sql

--changeset lab:1001-data-countries context:data
INSERT INTO countries (code, name, currency, regulated) VALUES
    ('GB', 'United Kingdom', 'GBP', TRUE), ('IE', 'Ireland', 'EUR', TRUE), ('DE', 'Germany', 'EUR', TRUE),
    ('ES', 'Spain', 'EUR', TRUE), ('IT', 'Italy', 'EUR', TRUE), ('FR', 'France', 'EUR', TRUE),
    ('NL', 'Netherlands', 'EUR', TRUE), ('PT', 'Portugal', 'EUR', TRUE), ('BG', 'Bulgaria', 'EUR', TRUE),
    ('RO', 'Romania', 'RON', TRUE), ('GR', 'Greece', 'EUR', TRUE), ('SE', 'Sweden', 'SEK', TRUE),
    ('MT', 'Malta', 'EUR', TRUE), ('CY', 'Cyprus', 'EUR', FALSE);
--rollback DELETE FROM countries;

--changeset lab:1001-data-sports context:data
INSERT INTO sports (id, name) VALUES (1, 'Football'), (2, 'Tennis'), (3, 'Basketball'), (4, 'Ice Hockey');
--rollback DELETE FROM sports;

--changeset lab:1001-data-competitions context:data
--comment: 12 football competitions, ids 1-12: the events below pick one by arithmetic (1 + n % 12).
INSERT INTO competitions (id, sport_id, country_code, name, tier) VALUES
    (1, 1, 'GB', 'Premier League', 1), (2, 1, 'ES', 'La Liga', 1), (3, 1, 'IT', 'Serie A', 1),
    (4, 1, 'DE', 'Bundesliga', 1), (5, 1, 'FR', 'Ligue 1', 1), (6, 1, NULL, 'Champions League', 1),
    (7, 1, 'NL', 'Eredivisie', 2), (8, 1, 'PT', 'Primeira Liga', 2), (9, 1, 'BG', 'First League', 3),
    (10, 1, 'GR', 'Super League', 3), (11, 1, 'RO', 'Liga I', 3), (12, 1, 'SE', 'Allsvenskan', 3);
--rollback DELETE FROM competitions;

--changeset lab:1001-data-teams context:data
--comment: 20 teams per competition, ids (competition - 1) * 20 + 1 .. 20, so a match can pick two by arithmetic.
INSERT INTO teams (id, competition_id, name)
SELECT (c.id - 1) * 20 + s.n, c.id,
       CONCAT(ELT(s.n, 'Ashford', 'Bramley', 'Carlow', 'Dunmore', 'Eastwick', 'Fairhaven', 'Glenrock', 'Harlow',
                  'Ironbridge', 'Kingsport', 'Lakeside', 'Millbrook', 'Northgate', 'Oakridge', 'Portmere', 'Queensbury',
                  'Riverton', 'Stonebridge', 'Westfield', 'Yarrow'),
              ' ', ELT(1 + c.id % 8, 'FC', 'United', 'City', 'Athletic', 'Rovers', 'Sporting', 'Dynamo', 'Real'))
FROM competitions c
JOIN seq s ON s.n <= 20
ORDER BY c.id, s.n;
--rollback DELETE FROM teams;
