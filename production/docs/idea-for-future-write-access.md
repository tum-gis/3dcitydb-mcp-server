# Idea: Write Access (DDL / DML) for the MCP Server and ChatBot

> ## ⚠️ RISK WARNING — read this first
> Enabling write access gives the LLM (and therefore *any user of the ChatBot*)
> the ability to run **arbitrary** data-modifying and schema-modifying SQL
> against the configured 3DCityDB instance. This opens the following risks:
>
> - **Data corruption from bad SQL** — a model can emit a syntactically valid
>   but semantically wrong statement (e.g. an `UPDATE`/`DELETE` with a wrong
>   or missing `WHERE`, an `ALTER TABLE` that drops a needed column, a
>   `DROP TABLE`). There is no automatic rollback of a *committed* mistake.
> - **Intentional sabotage** — a human user (or a prompt-injection payload in
>   imported content) can steer the model toward destructive statements.
>   Because this is explicitly an *experimental* tool, that threat is assumed
>   to be tolerated; the mitigations below limit the blast radius but do not
>   remove it.
>
> **Write access is disabled by default and must be explicitly turned on in
> `.env`.** This feature is intended for developer/experimental use against
> disposable or well-backed-up datasets — **not** for production databases.
> Keep current backups (or a disposable copy of the DB) before enabling it.

---

## Status: already specified, not yet implemented

`production/docs/mcp-tools-reference.md` already documents a read-write mode
gated behind `--mode=readwrite --i-understand-the-risks`, with two
*structured* tools (`update_property`, `replace_geometry`), transaction
wrapping, and a JSONL audit log (`citydb-mcp-audit.log`, override via
`CITYDB_MCP_AUDIT_LOG`) — referencing a `MODES.md` risk model.

**None of it exists yet.** There is no `--mode` flag, no write tools, and no
audit code in `src/`. The only real enforcement today is:

- `src/citydb_mcp/db.py` → `_configure_connection()` sets
  `SET default_transaction_read_only = on` on **every** pooled connection.
  This is the security boundary (the code comments say so explicitly).
- `src/citydb_mcp/tools/runtime_tools.py` → `run_query()` does a fast-fail
  `SELECT`/`WITH` prefix check (a convenience, *not* the boundary).

This document re-scopes that specced feature: instead of (only) structured
writes, the primary design is **arbitrary DML/DDL freedom**, with the
structured functions kept as an optional quality-controlled complement (see
the recommendation near the end).

## Goal

Allow the MCP server and the ChatBot to modify the database when — and only
when — explicitly permitted in configuration:

1. **`run_dml_statement(sql)`** — data manipulation: `INSERT`, `UPDATE`,
   `DELETE`, and (by decision, see below) stored-procedure `CALL`.
2. **`run_ddl_statement(sql)`** — schema manipulation: `CREATE`, `ALTER`,
   `DROP` for tables, indexes, and other objects.
3. Both **off by default**, each **independently toggleable**, with a
   **database-side** boundary (role grants) so the app-level flag is not the
   only line of defense.
4. A **JSONL audit log** of every write.
5. A **structured-function layer** as an optional, safer alternative for
   routine data maintenance.

This is deliberately an **experimental/developer** capability for exploring
3DCityDB + the chatbot, not an end-user feature.

## Design decision: arbitrary statements, not a fixed tool per operation

The primary mechanism is a small number of **general-purpose** tools that run
arbitrary SQL of a given class, rather than one tool per CRUD verb. Rationale
(for this project's intent):

- Maximum flexibility for experimentation — the LLM is free to craft any
  DML/DDL the role is permitted to run.
- Minimal surface area: two tools instead of a long CRUD vocabulary to
  maintain and document in the prompt.

The trade-off (less safety) is accepted deliberately and is mitigated by the
database-side role grants, the audit log, and the ChatBot confirmation step
(see below). The structured-function option at the end of this document is the
escape hatch for when a specific recurring operation is worth hardening.

### Tool definition

| Tool | Runs | Gated by | Notes |
|------|------|----------|-------|
| `run_dml_statement(sql)` | `INSERT`, `UPDATE`, `DELETE`, `CALL` | `CITYDB_ALLOW_DML` | Explicit transaction, rollback on error, reports rows affected |
| `run_ddl_statement(sql)` | `CREATE`, `ALTER`, `DROP` (table/index/view/…) | `CITYDB_ALLOW_DDL` | Postgres DDL is transactional, so it can be wrapped + rolled back too |

### Stored procedures: covered, but `CALL` needs to be explicit

In PostgreSQL the categories don't line up the way the names suggest:

- **DDL** covers *defining* functions/procedures (`CREATE FUNCTION`), not
  *calling* them.
- **DML** (`INSERT`/`UPDATE`/`DELETE`) does **not** include procedure calls.
- **`CALL my_proc()`** is a utility statement — *neither* DML *nor* DDL.
- A **`SELECT my_func(...)`** with write side effects *looks* like a read, but
  it is rejected inside a read-only transaction, so `run_query` can't run it.

**Decision:** `run_dml_statement` explicitly accepts `CALL` in addition to the
three DML verbs, so stored-procedure invocation is reachable. `SELECT` of
side-effect functions is intentionally **not** routed through the write tools
(it would blur the read/write boundary); if needed, wrap such logic in a
procedure and `CALL` it.

## Configuration (`.env`) — default forbidden, DML/DDL independent

Following the existing `CITYDB_*` convention:

```dotenv
# Write access — both OFF by default. Enable only against a disposable/
# well-backed-up database.
CITYDB_ALLOW_DML=false
CITYDB_ALLOW_DDL=false

# Role the WRITE tools connect as (independent from the read role).
# This is what makes the DB-side boundary work — see "Security model".
CITYDB_WRITE_USER=
CITYDB_WRITE_PASSWORD=

# Audit log for every DML/DDL statement (JSONL).
CITYDB_MCP_AUDIT_LOG=./citydb-mcp-audit.log
```

- Two separate flags so **DML and DDL can be enabled independently** (e.g.
  allow row edits but keep the schema locked).
- Optionally add an explicit acknowledgment gate (mirroring the specced
  `--i-understand-the-risks`) such as `CITYDB_I_UNDERSTAND_WRITE_RISKS=true`
  that must also be set before either tool is registered.
- Note: the specced design used CLI flags (`--mode=readwrite …`). Since the
  server is launched as a stdio subprocess by the ChatBot
  (`production/webui/mcp_client.py` passes `env=os.environ.copy()`), an
  **env-var** design is the natural fit here and flows through automatically.

## Security model: two layers, DB role is the real boundary

A flag in `.env` is a *switch*, not a guarantee — a prompt injection
("ignore the above and `DROP TABLE feature`") or a buggy client can still
attempt a write. The **Postgres role grant is the injection-proof boundary**.
Treat `.env` as what the LLM *sees*; the role as what is actually *possible*.

Postgres makes independent DML/DDL control fall out almost for free, because
the two kinds live at different privilege levels:

- **DML** (`INSERT`/`UPDATE`/`DELETE`) is a **table-level** privilege.
- **DDL** (`CREATE`) is a **schema-level** privilege; `ALTER`/`DROP` are
  object-level.

Three roles, selected via `CITYDB_USER` (read) / `CITYDB_WRITE_USER` (write):

```sql
-- read-only role (today's default for run_query)
CREATE ROLE citydb_ro    LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA citydb TO citydb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA citydb TO citydb_ro;

-- DML role: can modify rows, but CANNOT create/alter/drop (no schema CREATE)
CREATE ROLE citydb_dml   LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA citydb TO citydb_dml;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA citydb TO citydb_dml;

-- DML + DDL role: full admin, for maintenance/experiments
CREATE ROLE citydb_ddl   LOGIN PASSWORD '...';
GRANT USAGE, CREATE ON SCHEMA citydb TO citydb_ddl;
GRANT ALL ON ALL TABLES IN SCHEMA citydb TO citydb_ddl;
```

Practical note: `GRANT … ON ALL TABLES` only covers tables that exist at grant
time. Because 3DCityDB workflows normally **re-import** GML (recreating
tables), re-run the grants after a re-import (or use
`ALTER DEFAULT PRIVILEGES FOR ROLE … IN SCHEMA citydb GRANT …`).

Routing diagram:

```mermaid
flowchart LR
    LLM["LLM (chatbot)"] -->|"run_query"| RT["read pool<br/>autocommit + read-only txn"]
    LLM -->|"run_dml_statement (if CITYDB_ALLOW_DML)"| WT["write pool<br/>non read-only"]
    LLM -->|"run_ddl_statement (if CITYDB_ALLOW_DDL)"| WT
    RT -->|"role citydb_ro<br/>SELECT only"| DB[("3DCityDB")]
    WT -->|"role citydb_dml / citydb_ddl<br/>per GRANTs"| DB
```

## Connection-layer changes (`src/citydb_mcp/db.py`)

The current `DatabaseConnection` uses a single `ThreadedConnectionPool`
(`minconn=1`, `maxconn=CITYDB_POOL_MAX`, default 8) and forces
`default_transaction_read_only = on` on every connection. To support writes
**without weakening `run_query`**, use **two pools**:

- **Read pool** — unchanged: autocommit + read-only transaction, connects as
  the read role (`CITYDB_USER`). `run_query` keeps its hard read-only
  boundary regardless of whether writes are enabled.
- **Write pool** — non read-only, connects as `CITYDB_WRITE_USER`. Used *only*
  by `run_dml_statement`/`run_ddl_statement`. Its actual power is bounded by
  the role's `GRANT`s, not by any app-level SQL string check.

Transaction handling for the write pool:

- **DML** — explicit `BEGIN … COMMIT`, `ROLLBACK` on exception; return the
  affected row count.
- **DDL** — Postgres DDL is transactional, so it can likewise be wrapped in
  `BEGIN … COMMIT`/`ROLLBACK`; commit only after the statement succeeds.

## Guardrails (defense in depth — not the boundary)

These reduce accidental damage; they do **not** replace the role grants.

1. **Tool-class check via `sqlglot`** (already a dependency, used by
   `run_query`): classify the parsed statement and reject the *wrong class* —
   e.g. `run_dml_statement` refuses a `DROP`, `run_ddl_statement` refuses a
   bare `SELECT`. Prevents a model from smuggling a `DROP` through the "DML"
   tool.
2. **Single-statement enforcement** — psycopg2's simple-query protocol can
   execute `;`-separated batches in one call (so `UPDATE …; DROP TABLE …`
   would both run). Reject statements with embedded `;`, or split and
   validate each. The role grant is the backstop (a DML role can't `DROP`
   regardless).
3. **Affected-rows cap (optional, configurable)** — refuse or cap DML that
   touches more than `CITYDB_MAX_AFFECTED_ROWS` (default e.g. 1000), and
   require a `WHERE` clause on `UPDATE`/`DELETE` unless an explicit
   `allow_full_table=true` is set. This is the single most valuable guard
   against a hallucinated empty `WHERE`.
4. **Echo + confirm in the UI** — the ChatBot must show the exact SQL with a
   prominent ⚠️ and require an explicit confirmation (ideally typed) before
   dispatching a write. See ChatBot section.

## Audit log

Reuse the already-specified mechanism: append one JSONL line per write to
`CITYDB_MCP_AUDIT_LOG` (default `./citydb-mcp-audit.log`):

- timestamp, audit ID, role used, tool name, the full SQL, affected rows
  (DML), and commit/rollback status.
- For the structured-function layer (below), also record old/new values.

This is non-negotiable for LLM-driven writes — it's what makes "the model
changed X" recoverable/forensic after the fact.

## ChatBot (WebUI) side

- **Env flows through for free** — `production/webui/mcp_client.py` spawns
  `3dcitydb-mcp` with `env=os.environ.copy()`, so the `CITYDB_ALLOW_*` flags
  set in `production/.env` reach the server automatically.
- **Surface the tools** — the WebUI does not auto-discover tools today: the
  cloud backend advertises a hard-coded tool list and the local backend's
  ReAct parser is `run_query`-specific. So when a flag is on:
  - add the matching tool schema(s) to the cloud backend's advertised list;
  - route their execution through the already-generic
    `mcp_client.run_tool_sync(tool_name, args)`;
  - the Agent Activity panel must render arbitrary SQL (not just the existing
    ```sql fence) with a warning banner, and gate it behind the confirmation
    step above.
  - Status bar: show **read-only** vs **read-write (DML)** /
    **read-write (DDL)** so the mode is always visible.
- **Local (Ollama) models** — extending the ReAct parser for a second/third
  tool is extra work; the cloud backend is the easy first path. (Overlaps with
  [idea-for-future-multiple-MCP-servers.md](idea-for-future-multiple-MCP-servers.md) if you want clean general tool
  discovery rather than a targeted addition.)

## Recommendation: structured functions as a quality-controlled complement

For **routine** data maintenance, raw DML is the least safe option. It is
worth defining specific, parameterized database functions and exposing them as
narrow tools, *alongside* the arbitrary ones:

| Tool (backed by a DB function) | Purpose |
|-------------------------------|---------|
| `create_property(feature_id, name, namespace_id, value, datatype)` | Add one property row in the correct `val_*` column |
| `update_property(feature_id, name, namespace_id, value)` | Change one property (the already-specified tool) |
| `create_object(class, objectid, …)` / `delete_object(objectid)` | Create/remove a feature with its relations kept consistent |
| `replace_geometry(feature_id, wkt, srid, geometry_type)` | Validate WKT, update `geometry_data` + `geometry_properties` + `feature.envelope` (already-specified) |

Why this is worth having even with arbitrary tools available:

- **Integrity in one place** — 3DCityDB is a normalized, referentially
  integrated model. A raw `UPDATE property SET val_double = …` can write to
  the wrong column, leave `feature.envelope` stale, or orphan a relation. A
  function enforces the invariants and can be unit-tested.
- **No SQL injection** — parameters are bound, not string-built; the model
  cannot smuggle in a second statement.
- **Per-operation enable/disable at the DB layer** —
  `GRANT EXECUTE ON FUNCTION …` lets you allow "update a property" while
  leaving "drop a table" locked, purely in Postgres.
- **Cleaner audit** — record old/new values per feature, not just raw SQL.

Suggested stance: ship the **arbitrary** tools for the experimental freedom
you want, and progressively add **specific functions** for whatever recurring
edits you find yourself doing by hand — each new function is a small,
self-contained win and mirrors what the docs already spec
(`update_property`, `replace_geometry`).

## Effort

| Piece | Effort |
|-------|--------|
| `db.py` two-pool / mode-aware connection (read-only + write) | ~half a day |
| `run_dml_statement` + `run_ddl_statement` (txn, sqlglot class-check, single-statement, affected-rows cap) | ~1 day |
| Conditional tool registration in `server.py` + `.env` plumbing | ~half a day |
| JSONL audit log | ~half a day |
| DB role grants + post-re-import re-grant script | ~half a day |
| WebUI: tool surfacing (cloud) + confirmation UI + status indicator | ~1 day |
| **Base total (arbitrary DML/DDL, config, audit, WebUI)** | **~3–4 days** |
| Optional structured functions (`create_property`, `update_property`, `create_object`, `delete_object`, `replace_geometry`) | +2–4 days (each ~half day) |

## Suggested build order

1. Two-pool connection layer in `db.py` (write pool non read-only, read pool untouched).
2. `run_dml_statement` / `run_ddl_statement` with sqlglot class-check, single-statement guard, and affected-rows cap.
3. Conditional registration + `.env` flags (default off) + doctor/status reporting.
4. Audit log.
5. DB roles + re-grant script; verify the read role still cannot write.
6. WebUI: surface tools, SQL rendering with ⚠️, typed confirmation, mode status.
7. (Iteratively) structured functions for the specific edits you actually do.
