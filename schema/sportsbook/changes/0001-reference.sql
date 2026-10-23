--liquibase formatted sql

--changeset lab:0001-countries
--comment: Countries the sportsbook is licensed in. regulated: the regulator wants a daily report.
CREATE TABLE countries (
    code       CHAR(2)     NOT NULL,
    name       VARCHAR(60) NOT NULL,
    currency   CHAR(3)     NOT NULL,
    regulated  BOOLEAN     NOT NULL DEFAULT TRUE,
    PRIMARY KEY (code)
);
--rollback DROP TABLE countries;

--changeset lab:0001-sports
CREATE TABLE sports (
    id    SMALLINT    NOT NULL AUTO_INCREMENT,
    name  VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_sports_name (name)
);
--rollback DROP TABLE sports;

--changeset lab:0001-competitions
--comment: Leagues and cups. tier 1 = top league: the most bets.
CREATE TABLE competitions (
    id            INT         NOT NULL AUTO_INCREMENT,
    sport_id      SMALLINT    NOT NULL,
    country_code  CHAR(2)     NULL,
    name          VARCHAR(80) NOT NULL,
    tier          TINYINT     NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    CONSTRAINT fk_competitions_sport FOREIGN KEY (sport_id) REFERENCES sports (id),
    CONSTRAINT fk_competitions_country FOREIGN KEY (country_code) REFERENCES countries (code)
);
--rollback DROP TABLE competitions;

--changeset lab:0001-teams
CREATE TABLE teams (
    id              INT         NOT NULL AUTO_INCREMENT,
    competition_id  INT         NOT NULL,
    name            VARCHAR(80) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_teams_competition (competition_id),
    CONSTRAINT fk_teams_competition FOREIGN KEY (competition_id) REFERENCES competitions (id)
);
--rollback DROP TABLE teams;
