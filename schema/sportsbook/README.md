# The sportsbook database (MySQL)

The database of a sports betting platform, for the [mysql-single](../../labs/mysql-single/) lab. The [sportsbook application](../../tools/sportsbook/README.md) runs its traffic against it. Liquibase applies it from this folder (`changelog.yaml`, then every file in `changes/` in name order); the playbook runs it on the first deploy, and `ansible-playbook migrate.yml` applies new migrations.

## The tables

| Area | Tables |
|---|---|
| Reference | `countries`, `sports`, `competitions` (12 football leagues and cups), `teams` (20 per competition) |
| Players | `players`, `player_limits` (maximum stake, daily deposit and loss limits), `player_sessions` |
| Money | `wallets` (cash and bonus balance), `wallet_transactions` (the ledger), `payments` (deposits and withdrawals) |
| Matches | `events` (scheduled → live → finished → settled), `markets` (match result, over/under 2.5, both teams to score), `selections` (the outcomes and their odds), `odds_history` |
| Bets | `bets` (single or accumulator), `bet_selections` (the legs, with the odds the player got) |
| Promotions | `promotions`, `player_promotions`, `leaderboard` (the weekly top 1,000) |
| Audit | `audit_log` |

**The money follows the rules of a real ledger:**
- Every movement is a ledger row (`wallet_transactions`), never updated. `amount` is positive to the player, negative from the player.
- The wallet and its ledger row change in the same transaction.
- For every player and account (cash, bonus), **the wallet balance = the sum of the ledger's amounts**, and `balance_after` is the running balance at each row.
- A `CHECK` keeps balances from going below zero, and a unique `idempotency_key` per request means a retried request can't move money twice.

Check the first rule at any time; the `regulatory_report` job does it in every run:

```sql
SELECT (SELECT SUM(balance) FROM wallets) AS wallets,
       (SELECT SUM(amount) FROM wallet_transactions WHERE account = 'cash') AS ledger;
```

The same check after a restore shows whether the restored copy is consistent.

## Who may do what

Each part of the application has a MySQL role; the privileges are granted to the roles by the migrations (`0008-access.sql`), so they are versioned with the schema. Ansible creates the roles, the users and their memberships, and makes each user's roles its default roles.

| User | Role | Privileges |
|---|---|---|
| `sb_migrator` | (none) | Owns the database: `ALL` on `sportsbook.*` with `GRANT OPTION`. Runs the migrations |
| `sb_app` | `sportsbook_app` | The players: read the catalogue; read and write players, limits, wallets, bets, payments; add legs and ledger rows; manage sessions; write the audit log |
| `sb_feed` | `sportsbook_feed` | The odds feed: matches, markets, selections, `odds_history` |
| `sb_batch` | `sportsbook_batch` | Batch jobs: read everything; settle bets, credit wallets, pay withdrawals, award bonuses, rebuild the leaderboard, delete expired sessions |
| `sb_reporting` | `sportsbook_reporting` | Read only |
| `sb_support` | `sportsbook_support` | Read everything; change a player's status and limits |

All of them may log in only from the PMM server's IP.

## The generated data

The first deploy generates it (Liquibase context `data`); `sportsbook_scale` (default 1) multiplies it:

| | Rows per scale unit |
|---|---|
| Players, wallets, limits | 100,000 (`player1` … `player100000`) |
| Settled matches (last 30 days) | 4,000, plus 1,000 scheduled over the next 7 days |
| Markets / selections | 3 / 7 per match |
| Bets | 1,000,000: 70% singles, the rest accumulators of 2-4 legs; settled as their matches ended |
| Ledger rows | about 1.5 million: a deposit per player, every stake and win, and withdrawals |

About 1 GB: the ledger (360 MB), the bets (310 MB) and their legs (240 MB) are most of it. The ids follow from each other, so every step finds the rows of the previous one by arithmetic: match `e` has markets `(e - 1) * 3 + 1 .. 3` and selections `(e - 1) * 7 + 1 .. 7`. A helper table of the numbers 1 to 1,000,000 (`seq`) generates the rows, since MySQL has no `generate_series`; it is dropped at the end.

## A new migration

Add a file to `changes/`, for example `0009-add-bets-promo-code.sql`:

```sql
--liquibase formatted sql

--changeset lab:0009-add-bets-promo-code
ALTER TABLE bets ADD COLUMN promo_code VARCHAR(20) NULL, ALGORITHM=INSTANT;
--rollback ALTER TABLE bets DROP COLUMN promo_code;
```

Then, from `labs/mysql-single/ansible`: `ansible-playbook migrate.yml`. On the PMM server, `liquibase-sportsbook status`, `update-sql`, `history` and `rollback-count 1` show and control the same.

MySQL notes for migrations:
- **DDL commits by itself:** a changeset with an `ALTER` can't be rolled back by a failed statement after it, unlike PostgreSQL.
- **A `--comment:` line is Liquibase's:** only the first one per changeset. Further comment lines must be MySQL comments, written `-- ` with a space after the dashes.
- **Stored procedures and triggers** need `splitStatements:false` on the changeset, because their bodies contain `;`.
