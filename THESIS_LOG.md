# Thesis Engineering & Experiment Log

**Thesis Title:** MCP-Driven Query Optimization Advisory for Relational Databases (*MCP ως Σύμβουλος Βελτιστοποίησης Εκτέλεσης Ερωτημάτων σε Σχεσιακές Βάσεις Δεδομένων*)
**Repository:** `https://github.com/ChristosHussein/mcp-query-optimizer` (private)
**Measurement Contract:** [`measurement_contract.yaml`](measurement_contract.yaml) (`v1.0.0`, status `FROZEN_PRE_RUN` as of commit `fe61594`)

> **Purpose of this file:** Chronological, chapter-ready record of every architectural decision, implementation detail, bug/methodological catch, and official experimental run so the final MSc thesis manuscript can be written directly from verified facts.

---

## 1. Hardware & Software Environment

| Component | Exact Specification |
| :--- | :--- |
| **Host Machine** | Windows + WSL 2 (Ubuntu), AMD Ryzen 5 9600X (6 cores / 12 threads), 31.6 GB RAM, SSD (~680 GB free) |
| **Container Runtime** | Docker Desktop `v4.99.0` (Docker Engine `29.9.3`) |
| **Database Image** | Custom `docker/Dockerfile` built on `postgres:16-bookworm` |
| **PostgreSQL Extensions** | `pg_hint_plan 1.6.3`, `hypopg 1.4.5`, `pg_stat_statements 1.10` (`shared_preload_libraries = 'pg_hint_plan,pg_stat_statements'`) |
| **PostgreSQL Tuning (`docker/postgresql.conf`)** | `shared_buffers=4GB`, `effective_cache_size=16GB`, `work_mem=256MB`, `maintenance_work_mem=2GB`, `max_parallel_workers_per_gather=2`, `random_page_cost=1.1`, `effective_io_concurrency=200`, `default_statistics_target=100`, `geqo_threshold=18` |
| **Python Toolchain** | CPython `3.12.13` managed by `uv` (`pyproject.toml` + `uv.lock`) |
| **Key Python Packages** | `mcp==1.30.0`, `psycopg==3.3.9`, `psycopg-pool==3.3.3`, `pglast==8.9`, `google-genai==3.6.1`, `openai==3.3.0`, `duckdb==2.0.1`, `scikit-learn==1.9.1`, `numpy==2.5.5` |

**Verification:** `docker/smoke_test.sql` confirmed both `pg_hint_plan` (`/*+ SeqScan(t_smoke) */` overrode PK `Index Scan`) and `HypoPG` (created virtual index `<13561>btree_t_smoke_val` used in `EXPLAIN` as `Bitmap Index Scan`).

---

## 2. Benchmarks & Physical Database Design

### 2.1 Join Order Benchmark (JOB / IMDb) — Hint Steering
- **Database:** `imdb` (`8,699 MB` on disk)
- **Data Source:** Canonical May 2013 CWI IMDb snapshot (`https://event.cwi.nl/da/job/imdb.tgz`, 1.17 GB compressed, 21 CSV tables).
- **Queries & Schema:** 113 multi-join analytical queries from `gregrahn/join-order-benchmark` (`benchmarks/job/queries/1a.sql` – `33c.sql`).
- **Schema Catch:** `gregrahn/join-order-benchmark/schema.sql` had tighter `varchar(5)` / `NOT NULL` constraints than the actual CWI `imdb.tgz` data; replaced `benchmarks/job/schema.sql` with the exact `schematext.sql` bundled inside `imdb.tgz`.
- **Physical Design:** 21 Primary Keys + 23 Foreign Key indexes (`benchmarks/job/fkindexes.sql`) + `VACUUM ANALYZE`, matching the standard JOB / Bao / LLMSteer / LLMOpt evaluation setup where FK indexes expand the plan space and expose cardinality misestimations.
- **Verified Row Counts (`scripts/load_imdb.py`):** `cast_info` = 36,244,344; `movie_info` = 14,835,720; `movie_keyword` = 4,523,930; `name` = 4,167,491; `char_name` = 3,140,339; `person_info` = 2,963,664; `movie_companies` = 2,609,129; `title` = 2,528,312; `movie_info_idx` = 1,380,035; all 113 queries `EXPLAIN` cleanly.

### 2.2 TPC-H (Scale Factor 10) — Index Recommendation (`HypoPG`)
- **Databases:** `tpch` (pristine baseline, `15 GB`) + `tpch_eval` (isolated clone created via `CREATE DATABASE tpch_eval TEMPLATE tpch;` for Phase-2 real index materialization).
- **Data & Query Source:** Generated reproducibly via DuckDB's built-in official TPC-H extension (`CALL dbgen(sf=10)` and `tpch_queries()` — note Q15 uses a standard `WITH revenue AS (...)` CTE compatible with `EXPLAIN`), loaded via `scripts/load_tpch.py`.
- **Physical Design:** Primary Keys only (`benchmarks/tpch/pkeys.sql`, **no secondary/FK indexes**) so the `HypoPG` advisor has genuine headroom to discover high-impact secondary/composite indexes.
- **Verified Row Counts:** `lineitem` = 59,986,052; `orders` = 15,000,000; `partsupp` = 8,000,000; `part` = 2,000,000; `customer` = 1,500,000; `supplier` = 100,000; `nation` = 25; `region` = 5; all 22 queries (`q01.sql` – `q22.sql`) `EXPLAIN` cleanly.

---

## 3. Extended MCP Server Architecture (`src/postgres_mcp/`)

Forked from `crystaldba/postgres-mcp` (MIT, commit `15c8e33353546148acc2d8bd784551cf3905d1e2`, license preserved in `src/postgres_mcp/LICENSE_UPSTREAM`). Retained upstream tools (`explain_query` with `SqlBindParams` parametric placeholder substitution and `HypoPG`, `list_schemas`, `list_objects`, `get_object_details`, `analyze_workload_indexes`, etc.) and added four thesis-specific extensions:

1. **`explain_with_hints` (`src/postgres_mcp/advisory/advisor_tools.py` & `server.py`):**
   - Normalizes and sanitizes `/*+ ... */` hint blocks (blocks `/*`, `*/`, `;` injection).
   - Runs inside `BEGIN TRANSACTION READ ONLY` with `SET LOCAL statement_timeout`, `pg_hint_plan.enable_hint = on`, `pg_hint_plan.debug_print = on`, `pg_hint_plan.message_level = notice`.
   - Captures PostgreSQL `NOTICE` and `WARNING` diagnostics via `psycopg` notice handler and parses them into a structured `HintReport` (`used`, `not_used`, `duplicated`, `errors`, `warnings`, `all_applied`), giving the agent immediate feedback if an alias or directive was ignored.
   - Reuses upstream `SqlBindParams` when `$1, $2` placeholders are present.
2. **`get_table_stats` (`advisor_tools.py` & `server.py`):**
   - Single batched round-trip returning table row estimates (`pg_class.reltuples`), table/total sizes in MB, existing indexes (`pg_indexes`), and `pg_stats` (`null_frac`, `n_distinct`, `correlation`, top MCVs via `to_json(most_common_vals)` to avoid comma-splitting bugs on quoted strings, and downsampled `histogram_bounds`).
3. **`evaluate_real_indexes` (`advisor_tools.py` & `server.py`):**
   - Implements Phase-2 index evaluation: enforces a hard safety guard refusing any database whose name does not end with `_eval` (`PermissionError`), validates identifiers/index methods, creates temporary `tmp_adv_idx_*` indexes on `tpch_eval`, runs `ANALYZE`, executes warm-up + timed `EXPLAIN (ANALYZE, FORMAT JSON)`, and unconditionally drops the temporary indexes in `finally:`.
4. **Enhanced `ExplainPlanArtifact` (`src/postgres_mcp/artifacts.py`):**
   - Added `Alias` (`on relation AS alias` — critical because `pg_hint_plan` requires query aliases, not base table names), `Index Name`, join/index conditions (`Hash Cond`, `Merge Cond`, `Index Cond`, `Join Filter`), and automatic cardinality misestimation annotations (`[EST_ERR: Nx UNDER]` / `[EST_ERR: Nx OVER]` whenever estimated vs. actual rows differ by ≥10×).

**Upstream Bugs Found & Fixed:**
- `sql_driver.py`: Upstream marked the entire `DbConnPool` invalid (`_is_valid = False`) on *any* SQL exception (including timeouts); narrowed to `(psycopg.OperationalError, psycopg.InterfaceError)`.
- `sql_driver.py` + `explain_plan.py`: `SafeSqlDriver` (`--access-mode restricted`) rejected standard SQL `LIKE '%...%'` patterns present across JOB queries. Fixed `ExplainPlanTool._run_explain_query` to run inside `BEGIN TRANSACTION READ ONLY` + `SET LOCAL statement_timeout` directly so `EXPLAIN` is read-only and timeout-safe without breaking `LIKE` queries (while `execute_sql` is never exposed to the LLM).

**Automated Verification (`tests/test_mcp_advisory.py`):** All 8 checks pass (unit tests + live IMDb `get_table_stats`, valid/invalid `explain_with_hints`, parametric `$1/$2` `EXPLAIN`, `HypoPG` `EXPLAIN`, non-`_eval` guard rejection, live `tpch_eval` Phase-2 create/measure/drop, and live MCP stdio `ListToolsRequest` handshake).

---

## 4. Agentic Advisory Loop & Model Tiering (`src/advisor/agent.py`, `scripts/run_advisor.py`)

### 4.1 Architecture
- Spawns our extended `postgres-mcp` server over real MCP stdio (`mcp.ClientSession`), dynamically discovers tool JSON schemas via `session.list_tools()`, and converts them to Gemini Interactions API or OpenAI-compatible (NVIDIA NIM) function declarations.
- Exposes only read-only diagnostic/exploration tools to the LLM (`explain_query`, `explain_with_hints`, `get_table_stats`, `get_object_details` for JOB; `explain_query` with `hypothetical_indexes`, `get_table_stats`, `get_object_details` for TPC-H). Real index materialization (`evaluate_real_indexes`) is invoked deterministically by the harness **only** after a proposed index passes Phase-1 `HypoPG` cost filtering (≥5% estimated cost drop).
- Supports 4 experimental configurations via the same codebase:
  1. `mcp_single_shot` ($K=1$, Cheap Worker)
  2. `mcp_iter_cheap` ($K \le 5$, Cheap Worker throughout)
  3. `mcp_iter_hybrid` ($K \le 5$, Cheap Worker on Rounds 1–2; escalates to Frontier Advisor on Rounds 3–5 if latency improvement `< 25%` and query `> 50 ms`)
  4. `mcp_iter_frontier` ($K \le 5$, Frontier Advisor throughout)
- Supports `--resume` (skips already-logged `query_id`s in `queries.jsonl`) and per-round API error isolation with exponential backoff retries (`429`, `529`, `503`, `timeout`).

### 4.2 Model Selection & Provider Verification (Option A — \$0 Free Tier)
Connectivity tested on `2026-11-20` via `scripts/check_setup.py`:
- **Frontier Advisor (NVIDIA NIM):** `nvidia:nvidia/nemotron-3-ultra-550b-a55b` (550B total / 55B active MoE, native tool calling) — **verified working**.
- **Cheap Worker (NVIDIA NIM):** `nvidia:nvidia/nemotron-3.5-super-vl-120b-a12b` (120B / 12B active MoE, ~0.5s ping, native tool calling) — **verified working**. *(Rejected `z-ai/glm-5.3-flash` due to congestion and `minimaxai/minimax-m3.1-flash` which returned `HTTP 529 Service temporarily overloaded` during testing; rejected Gemini 4 Frontier models on Free Tier because API returned `HTTP 429: limit: 0 input tokens per minute on Free Tier`.)*
- **LLMSteer Embeddings (Google Gemini API):** `gemini-embedding-001` (`dim=3072`, ~0.3s ping on Free Tier) — **verified working**.

### 4.3 Early Engineering & Methodological Catches (from Exploratory Smoke Tests)
> *Note: Numbers below are from disposable exploratory smoke runs (deleted after diagnosis) and are NOT official benchmark results.*
1. **Tool-budget loop blow-up (fixed in `e1280b9`):** In the initial 2-query JOB smoke test (`17b`, `18c`), the worker ignored a text-only budget warning and kept emitting tool calls (16.5 LLM calls, ~206k tokens/query). Stripping tool declarations from the API request once `max_tool_calls_per_round=2` is reached (and forcing `analyze=False` on exploration calls) reduced Round-1 usage to **3 LLM calls / ~13.7k tokens** (~7× reduction).
2. **Cold-cache vs. warm-cache bias (fixed in `e1280b9`):** In the first smoke test on `17b`, Round 0 ran on cold OS buffers (`53.6s`), whereas warm-cache baseline is `~2.74s` (and hinted plan `~0.36s`). Added warm-up runs before timing both Round 0 and every evaluated hint/index proposal so cache warming cannot inflate measured speedups.
3. **Validation of 2-phase `HypoPG` on TPC-H SF=10 `q06`:** In the exploratory `q06` smoke run, `HypoPG` estimated a **-41% cost drop** for `(l_shipdate, l_quantity, l_discount)` on 60M-row `lineitem`, but Phase-2 real materialization on `tpch_eval` revealed an actual **+49% regression** (`1,333 ms` → `1,990 ms`) due to random heap fetches vs. Parallel Seq Scan — empirical proof that Phase-2 measurement is mandatory.

---

## 5. Baselines & Analysis Tooling

- **`scripts/run_pg_default.py`:** Measures default PostgreSQL 16 (`warmup=2`, `runs=3`, median, `timeout_ms=180000`) on JOB (113) and TPC-H SF=10 (22).
- **`scripts/run_bao_oracle.py`:** Upper-bound Bao Oracle over PostgreSQL's 6 binary join/scan planner knobs (`--arms top5` or `--arms all48` = Arm 0 default + 48 non-empty join×scan combinations), deduplicating identical physical plans via `EXPLAIN` structure hashing before executing `EXPLAIN ANALYZE`. Outputs both `queries.jsonl` (oracle best) and `arm_latencies.jsonl` (per-arm ground truth for LLMSteer).
- **`scripts/run_llmsteer.py`:** Reproduces LLMSteer (Akioyamen et al., 2024): selects the highest-impact alternative Bao arm, embeds all 113 JOB queries with `gemini-embedding-001` (cached in `results/cache/`), and runs 10-fold stratified CV with `StandardScaler + PCA + RBF-SVM (class_weight="balanced")`.
- **`scripts/summarize_runs.py`:** Independent reconstruction script reading raw `queries.jsonl` logs to compute P50, P90, total latency, % improved, % regressed, timeouts, mean LLM calls/tokens, escalation count, and $K=1 \dots 5$ convergence curves.

---

## 6. Official Experiment Ledger

| Run ID | Benchmark | Method | Git Commit | Status | P50 (ms) | P90 (ms) | Total (s) | Timeouts | Evidence Path |
| :--- | :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `pg_default_job_v1` | JOB (113) | `pg_default` | `fe61594` | **DONE** | 154.49 | 2,425.95 | 82.828 | 0 | `results/runs/pg_default_job_v1/queries.jsonl` |
| `pg_default_tpch_v1` | TPC-H SF=10 (22) | `pg_default` | `5ec3d35` | **DONE** | 2,578.07 | 24,726.97 | 456.898 | 2 (`q17`, `q20`) | `results/runs/pg_default_tpch_v1/queries.jsonl` |
| `bao_oracle_all48_job_v1` | JOB (113) | `bao_oracle` (`all48`, 49 arms) | `5de8b3c` | **DONE** | 143.14 | 1,708.84 | 52.824 | 0 | `results/runs/bao_oracle_all48_job_v1/queries.jsonl` |
| `llmsteer_job_v1` | JOB (113) | `llmsteer_baseline` (10-fold CV) | `7fb875e` | **DONE** | 199.76 | 1,986.56 | 81.515 | 0 | `results/runs/llmsteer_job_v1/queries.jsonl` |
| `mcp_single_shot_tpch_v1` | TPC-H SF=10 (22) | `mcp_single_shot` (K=1, Cheap) | `7fb875e` | **DONE** | 1,675.14 | 11,183.48 | 86.263 | 0 | `results/runs/mcp_single_shot_tpch_v1/queries.jsonl` |
| `mcp_iter_cheap_tpch_v1` | TPC-H SF=10 (22) | `mcp_iter_cheap` (K≤5, Cheap) | `3f971ba` | **DONE** | 1,036.56 | 10,259.20 | 73.134 | 0 | `results/runs/mcp_iter_cheap_tpch_v1/queries.jsonl` |
| `mcp_iter_hybrid_tpch_v1` | TPC-H SF=10 (22) | `mcp_iter_hybrid` (K≤5, Hybrid) | `f8f4745` | **DONE** | 1,055.06 | 10,950.73 | 72.923 | 0 | `results/runs/mcp_iter_hybrid_tpch_v1/queries.jsonl` |
| `mcp_single_shot_job_v1` | JOB (113) | `mcp_single_shot` (K=1, Cheap) | `dd76bb4` | **DONE** | 145.13 | 1,523.65 | 68.624 | 0 | `results/runs/mcp_single_shot_job_v1/queries.jsonl` |
| `mcp_iter_cheap_job_v1` | JOB (113) | `mcp_iter_cheap` (K≤5, Cheap) | `cde1fdc` | **DONE** | 131.38 | 1,310.64 | 53.014 | 0 | `results/runs/mcp_iter_cheap_job_v1/queries.jsonl` |
| `mcp_iter_hybrid_job_v1` | JOB (113) | `mcp_iter_hybrid` (K≤5, Hybrid) | `6f98ee9` | **DONE** | 183.38 | 1,796.20 | 69.539 | 0 | `results/runs/mcp_iter_hybrid_job_v1/queries.jsonl` |
| `v2_120b_job_top12` | JOB Top-12 (`v2`) | `v2_120b` (K=20 fixed, 120B) | `1eb9ba2` | **DONE** | 1,263.15 | 2,072.88 | 15.010 | 0 | `results/runs/v2_120b_job_top12/queries.jsonl` |
| `v2_550b_job_top12` | JOB Top-12 (`v2`) | `v2_550b` (K=20 fixed, 550B) | `097c4d9` | **DONE** | 1,152.30 | 2,905.70 | 18.550 | 0 | `results/runs/v2_550b_job_top12/queries.jsonl` |
| `v2_hybrid_job_top12` | JOB Top-12 (`v2`) | `v2_hybrid` (K=20 alternating 120B↔550B) | `c13b67f` | **DONE** | 1,093.10 | 2,017.03 | 14.040 | 0 | `results/runs/v2_hybrid_job_top12/queries.jsonl` |
| `v2_120b_tpch_top8_k6` | TPC-H Top-8 (`v2`, K*=6) | `v2_120b` (120B) | `d72560d` | **DONE** | 5,199.40 | 12,311.56 | 49.840 | 0 | `results/runs/v2_120b_tpch_top8_k6/queries.jsonl` |
| `v2_550b_tpch_top8_k6` | TPC-H Top-8 (`v2`, K*=6) | `v2_550b` (550B) | `ccc6468` | **DONE** | 4,686.90 | 12,248.11 | 46.940 | 0 | `results/runs/v2_550b_tpch_top8_k6/queries.jsonl` |
| `v2_hybrid_tpch_top8_k6` | TPC-H Top-8 (`v2`, K*=6) | `v2_hybrid` (alternating 120B↔550B) | `db9ccf2` | **DONE** | 4,332.70 | 11,966.40 | 43.110 | 0 | `results/runs/v2_hybrid_tpch_top8_k6/queries.jsonl` |

**Baseline Insights:**
- **JOB (`pg_default_job_v1`):** Classic heavy-tailed distribution — median is fast (`154.5 ms`), while P90 is `2,426.0 ms` and the top tail queries (`16b`: `7.66s`, `19d`: `6.06s`, `17e`/`17a`: `4.76s`, `17f`: `4.14s`, `31c`: `3.74s`, `25c`: `3.15s`, `10c`: `2.96s`) dominate total runtime (`82.83s`).
- **TPC-H SF=10 (`pg_default_tpch_v1`):** With PKs only, 20 queries complete in `96.90s` (notably `q18`: `26.20s`, `q13`: `11.47s`, `q01`: `11.21s`, `q09`: `9.33s`, `q10`: `6.63s`), while **`q17` and `q20` time out at `180s`** due to unindexed correlated `lineitem(l_partkey)` lookups (counted at `180,000 ms` each per `measurement_contract.yaml`, yielding `456.90s` total).
- **Important Pre-Run Catch for Advisor:** `src/advisor/agent.py` currently returns early if Round 0 `EXPLAIN ANALYZE` times out (`base_ms is None`). Before running the TPC-H advisor, we should fall back to `EXPLAIN` (without `ANALYZE`) and set `base_ms = 180000.0` on Round-0 timeouts so the advisor can optimize `q17` and `q20`!

---

## 7. Phase 2 (`v2`) Methodology — Fixed-`K` Pareto & Anti-Contamination (`measurement_contract_v2.yaml`)

- **Fast Async Engine (`scripts/v2/run_v2.py`):** 4-way `AsyncOpenAI` concurrency across queries with a strict `asyncio.Lock` around every PostgreSQL `EXPLAIN ANALYZE` / Phase-2 real index build (verified zero DB contention in `tests/speeduptest/bench_e2e.py`, 3.2× faster wall-clock time), `get_table_stats` pre-fetched once at Round 0 (-20% tokens), unbounded thinking (apples-to-apples with `v1`), and per-query exact-action deduplication cache to force genuine exploration across `K = 20` fixed rounds.
- **3-Way Model Comparison on Slowest `v1` Queries (`K = 20` fixed, no early stopping):**
  1. `v2_120b` (`nemotron-3.5-super-vl-120b-a12b`)
  2. `v2_550b` (`nemotron-3-ultra-550b-a55b`)
  3. `v2_hybrid` — **Alternating round-robin multi-model ensemble** (`odd rounds = 120B`, `even rounds = 550B`), sharing full `Previous Rounds History` and `Last Evaluated Plan` (updated pre-run in `c13b67f` after `v2_120b` `-68.4%` outperformed `v2_550b` `-62.7%` overall while exhibiting complementary per-query wins).
- **Anti-Contamination Validation (at solved Pareto-optimal `K*`):**
  1. **`imdb_anon`:** Schema-anonymized opaque-view mirror (`t01..t21`, `c01..cN`) of `job_top12` — zero IMDb identifiers, identical data skew and cardinality errors.
  2. **`stats_ceb`:** Real-world **STATS-CEB (VLDB 2022)** benchmark (Stats StackExchange, 8 tables) — non-IMDb/non-TPC-H real-world generalization test.

---

## 8. Git Commit Log

- `e0014c5` — Initial project scaffold, PostgreSQL 16 Docker setup, and draft measurement contract.
- `75ee861` — JOB & TPC-H SF=10 loaders, extended `postgres-mcp` server, Gemini advisory loop, and baselines.
- `986db31` — NVIDIA NIM (OpenAI-compatible) provider routing with retry/backoff.
- `e1280b9` — Tool-budget enforcement, warm-cache fairness, read-only `EXPLAIN` fix, API timeout resilience, `check_setup.py`, `summarize_runs.py`.
- `fe61594` — Frozen pre-experiment measurement contract `v1.0.0` before official baseline runs.
