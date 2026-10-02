# Idea: Persistent Chat Memory with RAG (Question → Answer → Distilled Story)

> **Status: concept, not yet implemented.** This document refines a design
> discussed and partially tested on the live fullstack stack (pgvector
> availability and dynamic installation were verified, see §4). Nothing here
> exists in the code yet.

## Goal

Give the chatbot **cross-session memory**: every completed chat turn
(question, final answer, distilled "how the answer was found" story) is
persisted, and on each new question the semantically closest previous turns
are retrieved and injected into the system prompt as scoping context (a RAG
approach).

Why this is missing today: all conversation state (`history_state`,
`story_state` in `webui/app.py`) lives in Gradio `gr.State` — it dies with
the browser session. A user who resolved an ambiguity yesterday ("*area*"
→ `grossFloorArea` for *this* dataset) has to resolve it again today.

## Design principles

1. **The JSONL file is the source of truth; the pgvector table is a
   derived, rebuildable index.** Backing up or migrating the memory is
   copying one text file. A new stack imports the file into an empty
   vector store.
2. **Memory is a *context* aid, never a data source.** The agent must keep
   querying the live database for facts. The memory blocks carry earlier
   *term resolutions, preferences, and query patterns* — not facts that
   may have gone stale. This is enforced by an explicit instruction in the
   injected prompt block (see §7).
3. **Everything degrades gracefully.** No embedding server reachable, no
   memory file, pgvector missing → memory contributes nothing, the chat
   works exactly as today.
4. **Off by default** (`ENABLE_MEMORY=false`), like other experimental
   features in this project.

## 1. What is stored

One record per completed turn:

| Field | Content | Notes |
|-------|---------|-------|
| `id` | UUID, generated when the turn is created | Stable across export/import; the upsert key |
| `ts` | ISO-8601 timestamp | |
| `session_id` | Gradio session id | For future per-user scoping |
| `question` | The user's question | The text that gets **embedded** |
| `answer` | The final answer shown to the user | Stored unless `MEMORY_SAVE_ANSWER=false` |
| `story` | The **distilled** story (`[AUTO-GENERATED SUMMARY]`) | Only the distilled story, *not* the raw reasoning chain (orders of magnitude smaller, and it is the intended human-readable artifact) |
| `embedding_model` | Model name + tag, e.g. `bge-m3` | Needed to detect model switches |

The **question only** is embedded (retrieval key). Answer and story ride
along with the hit.

### Storage layout

- **JSONL file** at `data/chat_memory.jsonl` (one JSON object per line).
  `production/data` is already bind-mounted into the agent container
  (`./data:/app/data`), so the file survives container recreation, and a
  user can carry it to a fresh stack with a single file copy.
- **Database table** `mcp_memory.turns` in its **own schema** (`mcp_memory`,
  *not* `citydb`), so the table never leaks into the assembled prompt /
  schema snapshots that the query agent sees — the agent must not start
  querying the chat log as if it were CityGML data.

```sql
CREATE SCHEMA IF NOT EXISTS mcp_memory;
CREATE TABLE IF NOT EXISTS mcp_memory.turns (
    id              UUID PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    session_id      TEXT,
    question        TEXT NOT NULL,
    answer          TEXT,            -- NULL when MEMORY_SAVE_ANSWER=false
    story           TEXT,
    embedding_model TEXT NOT NULL,
    embedding       vector(1024)     -- dimension must match the model;
                                     -- drop & rebuild the table on model change
);
CREATE INDEX IF NOT EXISTS turns_embedding_idx
    ON mcp_memory.turns USING ivfflat (embedding vector_cosine_ops);
```

## 2. Write path (per turn)

After a turn completes (question + answer + distilled story are all known —
the story is produced by the existing `_summarize_story_ollama` step):

1. `INSERT INTO mcp_memory.turns … ON CONFLICT (id) DO NOTHING` with the
   freshly computed embedding.
2. Append the same JSON line to `data/chat_memory.jsonl` (in-process
   `threading.Lock`; single process, so no cross-process locking needed).

DB first, file second: if the DB write fails, the turn is not written to
the file either — the file is never *richer* than the index can become,
and the import logic (§3) heals the reverse case anyway.

## 3. Import path (stack start, idempotent)

A background task in the agent container, started at boot, never blocking
the app:

1. `data/chat_memory.jsonl` present? No → done.
2. For each line whose `id` is not yet in `mcp_memory.turns` and whose
   `embedding_model` matches the currently configured model → embed the
   question → upsert.
3. Log progress every 100 records (`[memory] imported 500/2341 …`).

Consequences:

- **New stack + old file** → full initial import.
- **Existing stack** → only turns created since last boot (usually none,
  since the write path already upserts).
- **Partial failure** (embedding server dies mid-import, one record errors)
  → the failing `id` is logged and skipped; the next boot retries it for
  free. No retry machinery needed — the upsert is idempotent by design.
- **Embedding server unreachable at boot** → task retries lazily / gives
  up silently; retrieval simply returns 0 hits until it succeeds. The chat
  is unaffected.

### Import parallelism

A thread pool sized by `MEMORY_EMBED_WORKERS` (default 4 — deliberately
small: the embedding server does its own batching, and the pool just
removes round-trip latency). A single import task per container; live
turn writes may run concurrently because `ON CONFLICT DO NOTHING` makes
double-upserts harmless.

## 4. pgvector on the fullstack Postgres (tested)

Verified on the running `production-postgres-1`
(`khaoulakanna1/3dcitydb-v5-docker-sfcgal-patched`, PostgreSQL 18.3,
Debian trixie, pgdg repository):

- pgvector is **not** part of the base image (`vector` absent from
  `pg_available_extensions`, no `.so`/`.control` on disk).
- The matching package **is** installable at runtime from the already
  configured pgdg apt source:
  `apt-get install postgresql-18-pgvector` → **0.8.6**;
  `CREATE EXTENSION vector;` succeeds.

Problem: an install into the container's writable layer is **lost** on
`--force-recreate` / image rebuild, while the database volume survives —
the extension state and the package state would then disagree.

**Decision (option 1):** make it **self-healing at container start** — no
custom postgres image, no fork of the base image:

1. Is the `vector` extension available? No → the fullstack
   `docker-compose` file provides a small **init sidecar / entrypoint hook**
   whose only job is, idempotently at postgres container start:
   `apt-get install -y postgresql-18-pgvector && CREATE EXTENSION IF NOT
   EXISTS vector` (psql as the postgres superuser). The postgres container
   image stays untouched; no fork of the base image, no rebase when the
   upstream image updates.
2. `CREATE SCHEMA mcp_memory` + table/index DDL from §1 (idempotent).
3. The import task from §3.

The exact mechanism (init sidecar vs. custom postgres entrypoint override
in compose) is a detail to settle at implementation time; the invariant is:
**package + extension + schema are re-established idempotently on every
postgres container start.**

## 5. Retrieval pipeline (per new question)

1. Embed the question with the configured embedding model.
2. `SELECT … ORDER BY embedding <=> $q_vec LIMIT k` in `mcp_memory.turns`,
   `k = MEMORY_TOP_K` (default 3).
3. **Admission rule — absolute floor AND relative rule, cumulative (AND):**
   a hit is admitted only if

   ```
   sim_best ≥ MEMORY_MIN_SIMILARITY          (absolute floor)
   AND
   sim_i ≥ α · sim_best   with α ≈ 0.85      (relative rule)
   ```

   - The **floor** protects against the degenerate case where *all* hits
     are poor (0.20 / 0.19 / 0.18 passes any relative rule but is pure
     noise) — then the memory block is simply empty.
   - The **relative rule** trims weak tail hits once the best one is
     genuinely good (0.80 / 0.79 / 0.41 → the 0.41 hit drops out, 2 blocks
     are injected).
   - Rationale for both: cosine similarity is **not comparable across
     embedding models**. On a model switch only the floor needs
     re-calibration; the relative rule stays put. The floor default is
     therefore documented in `.env.example` as model-dependent.
4. **Deduplication against the current session:** turns whose content is
   already part of the live Gradio history are not re-injected (avoids
   duplicated context the model has in front of it anyway).
5. **Token budget:** the whole memory block is capped (e.g. ≤ 1000
   tokens) because the assembled schema prompt is already large; on
   overflow, lowest-similarity hits are dropped first.
6. Injection: a **separate system message** (same pattern as the existing
   `[VIEWER SELECTION]` and distilled-story injections), positioned after
   the main system prompt, with the guard instruction from §7.

## 6. Embedding infrastructure

### Multilingual requirement

Users ask in German and English; a question in one language must retrieve a
turn stored in the other. **Default model: `bge-m3`** (via Ollama):

- 100+ languages, excellent DE/EN, strong **cross-lingual** retrieval —
  exactly the use case.
- Prompt-free (no instruction prefix to get wrong), 1024 dimensions.
- `nomic-embed-text` (English-centric) is **not** suitable here.
- Alternatives documented for `MEMORY_EMBEDDING_MODEL`:
  `snowflake-arctic-embed-l` (40+ langs, 1024d),
  `paraphrase-multilingual-MiniLM-L12-v2` (38 langs, 384d, tiny).

### Separate CPU embedding container

Recommended, but optional:

- Embedding needs little memory (bge-m3 ≈ 2.3 GB weights, runs fine on
  CPU) and must not contend with the chat LLM's generation load — a bulk
  initial import (hundreds of embeddings) would otherwise starve active
  chats on a shared Ollama server.
- A dedicated `ollama-embedder` compose service (CPU-only, small model)
  keeps the GPU LLM server dedicated to generation.

### Separate base URL

`MEMORY_EMBEDDING_BASE_URL` **defaults to falling back to
`OLLAMA_BASE_URL`**; set it explicitly when the embedding server runs on a
different machine than the chat LLM.

### Ollama-native API, not LiteLLM

Ollama does not provide a reliable OpenAI-compatible `/v1/embeddings`
endpoint. Embedding is a small, direct HTTP call to
`{MEMORY_EMBEDDING_BASE_URL}/api/embed` — deliberately outside the
LiteLLM plumbing (no provider inference, no `safe_completion`, no
reasoning concerns). The model must be pulled once on the embedding
server (`ollama pull bge-m3`); auto-pull at startup is intentionally not
done.

## 7. Prompt injection format & safety

```
[PAST CONVERSATIONS — context only]
The following similar turns from earlier conversations may help you
interpret terms and phrasing:

1. (sim 0.81) Q: "Wie groß ist die Geschossfläche von diesen Gebäuden?"
   A: … (truncated)
   HOW SOLVED: …distilled story…
2. …

Rules for using this block:
- Treat it ONLY as context for terminology, earlier user preferences, and
  query patterns (e.g. the user already established that "area" means
  grossFloorArea in this project).
- Never cite it as a data source and never repeat its data values as facts:
  the database may have changed since. Always query the live database.
- If nothing here is relevant, ignore the block entirely.
```

The last rule matters: the system prompt already forbids imitating
`[AUTO-GENERATED SUMMARY]` blocks; without an explicit "ignore when
irrelevant" instruction the model would tend to reference stale answers
("wie bereits erwähnt …").

## 8. Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_MEMORY` | `false` | Master switch for the whole feature |
| `MEMORY_STORE_FILE` | `data/chat_memory.jsonl` | JSONL path (relative to `/app`) |
| `MEMORY_TOP_K` | `3` | Maximum number of injected hits |
| `MEMORY_MIN_SIMILARITY` | `0.35` (re-calibrate per model) | Absolute floor, see §5 |
| `MEMORY_SAVE_ANSWER` | `true` | Store the answer in JSONL *and* DB; `false` stores question + story only |
| `MEMORY_EMBEDDING_MODEL` | `bge-m3` | Ollama embedding model |
| `MEMORY_EMBEDDING_BASE_URL` | fallback: `OLLAMA_BASE_URL` | Separate URL when the embedder runs on another machine |
| `MEMORY_EMBED_WORKERS` | `4` | Import/thread-pool parallelism |

`MEMORY_SAVE_ANSWER=false` applies consistently to **both** stores so the
source of truth and the index never diverge.

## 9. Placement in the codebase

- All memory logic lives in the **webui** (`production/webui/` — new
  `memory.py`, plus hooks in `app.py`): the answer and the distilled story
  are produced there.
- `src/citydb_mcp/` stays untouched (it is process-agnostic; the MCP
  server's DB access remains read-only — the only write path is the
  webui's `mcp_memory` schema, using the same connection).
- Postgres-side init (pgvector install + `mcp_memory` DDL) belongs to the
  fullstack compose setup, not to the app image.

## 10. Estimated effort

| Piece | Effort |
|-------|--------|
| `memory.py`: JSONL store + `mcp_memory.turns` upsert (write path, lock, `MEMORY_SAVE_ANSWER`) | ~1 day |
| Embedding client (Ollama `/api/embed`, base-URL fallback, error handling) | ~half a day |
| Boot init: pgvector self-healing (compose hook) + idempotent DDL + parallel import task | ~1 day |
| Retrieval pipeline (top-k, floor + relative rule, session dedup, token cap) | ~half a day |
| Prompt injection block + system instruction, wired into the existing message assembly | ~half a day |
| `.env` plumbing (`ENABLE_MEMORY`, `MEMORY_*`) + doctor/status reporting | ~half a day |
| Optional `ollama-embedder` compose service + documentation | ~half a day |
| **Base total (feature, off by default)** | **~4 days** |
| Testing: unit tests for store/import/retrieval + live end-to-end pass on the fullstack stack | +1–2 days |

## 11. Future extensions (out of scope for the initial implementation)

- **Cap / rotation:** the JSONL grows unbounded; a rotation policy (keep
  the most recent N turns, e.g. 5000) keeps imports and retrieval fast.
  Explicitly *not* in the first version.
- **Per-user memory:** `session_id` is already recorded; adding
  identities and scoping retrieval to the current user is a later step
  (v1 is single-tenant: all data in one file / one table).
- **Embedding model migration tooling:** on model change, rebuild the
  table from the JSONL (drop table, re-embed all questions) — currently a
  manual operation; could become a CLI command.
- **Selective erasure / privacy:** per-record or per-session deletion
  (GDPR-style right to be forgotten) from a small UI or CLI.
- **Memory inspection UI:** a webui tab to browse/edit/prune stored turns.
- **Recency weighting:** combine the similarity score with a recency
  decay factor so recent turns rank above equally similar old ones.
