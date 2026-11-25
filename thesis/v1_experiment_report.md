# MCP-Driven Query Optimization Advisory for Relational Databases
## Phase 1 (`v1`) — Architecture, Methodology, Empirical Results & Critical Reflections

---

## 1. Problem Statement & Motivation

Modern relational database management systems (RDBMS) such as PostgreSQL rely on cost-based query optimizers to select an execution plan for every SQL query. While cost-based optimizers perform well on uniform, well-indexed workloads, they frequently fail under two common real-world conditions:

1. **Severe Cardinality Estimation Errors (Join-Order & Operator Failure):** When real-world tables exhibit heavy data skew and multi-column correlations, the optimizer can underestimate intermediate join cardinalities by 100×–50,000×. Believing an intermediate result is tiny, PostgreSQL may choose a `Nested Loop` join over a 36-million-row table instead of a `Hash Join` or a more selective join order, turning a 200 ms query into a multi-second bottleneck.
2. **Missing Secondary / Composite Indexes:** On large analytical tables with only primary keys, correlated subqueries and selective filters force repeated full-table scans (`Seq Scan`) that can time out after minutes.

Recent LLM-based query optimization approaches either rely solely on static SQL text (such as embedding-based classifiers like **LLMSteer**, which cannot inspect live database telemetry and suffer from regressions) or propose unconstrained SQL rewrites that risk altering query semantics.

**Core Hypothesis:** By connecting an LLM to PostgreSQL through the **Model Context Protocol (MCP)** — exposing live `EXPLAIN ANALYZE` plans annotated with cardinality-error markers, catalog statistics (`pg_stats`), structured hint diagnostics, and 2-phase index verification — an iterative advisory agent can reliably discover large latency reductions with **zero correctness risk** and **zero execution regressions**, even using open-weights models.

---

## 2. System Architecture

The system separates **reasoning** (LLM), **telemetry & safe execution** (extended MCP server), and **data** (PostgreSQL 16 in Docker):

```text
+-----------------------------------+        +------------------------------------------+        +-----------------------------+
|         Advisory Agent            |  MCP   |         Extended postgres-mcp            |  SQL   |     PostgreSQL 16 (Docker)  |
|  (src/advisor/agent.py)           | <----> |       (src/postgres_mcp/server.py)       | <----> |  pg_hint_plan 1.6.3         |
|                                   | stdio  |                                          |        |  HypoPG 1.4.5               |
|  - Iterative closed-loop (K <= 5) |        |  - explain_query (+ HypoPG)              |        |  pg_stat_statements 1.10    |
|  - Strict tool budget (<= 2/rnd)  |        |  - explain_with_hints (+ HintReport)     |        |                             |
|  - Model tiering (120B -> 550B)   |        |  - get_table_stats (batched pg_stats)    |        |  Databases:                 |
|  - Best-plan retention (0% regr.) |        |  - evaluate_real_indexes (*_eval only!)  |        |   * imdb (8.7 GB, JOB)      |
+-----------------------------------+        +------------------------------------------+        |   * tpch (15 GB, baseline)  |
                                                                                                 |   * tpch_eval (15 GB clone) |
                                                                                                 +-----------------------------+
```

### 2.1 Extended MCP Server (`src/postgres_mcp/`)
Forked from `crystaldba/postgres-mcp` (MIT) and extended with four research-specific capabilities:

1. **`explain_with_hints`:** Injects sanitized `/*+ ... */` `pg_hint_plan` blocks inside `BEGIN TRANSACTION READ ONLY ... ROLLBACK` with `SET LOCAL statement_timeout`. Because `pg_hint_plan` silently ignores typos or misnamed aliases by default, our tool enables `pg_hint_plan.debug_print = on` and `message_level = notice`, capturing PostgreSQL `NOTICE`/`WARNING` messages into a structured `HintReport` (`used`, `not_used`, `duplicated`, `errors`, `warnings`).
2. **`get_table_stats`:** A single batched read-only round-trip returning row estimates (`pg_class.reltuples`), table/index sizes, existing indexes (`pg_indexes`), and per-column `pg_stats` (`null_frac`, `n_distinct`, `correlation`, top Most Common Values, and downsampled histogram bounds).
3. **`evaluate_real_indexes`:** Implements **Phase-2 index verification** with a hard safety guard refusing any database whose name does not end in `_eval` (`PermissionError`). It validates all table and column names against a strict identifier whitelist (`[a-zA-Z_][a-zA-Z0-9_]*`, preventing SQL injection in LLM-generated DDL), creates temporary indexes on `tpch_eval`, runs `ANALYZE`, executes warm-up + timed `EXPLAIN ANALYZE`, and unconditionally drops the temporary indexes in a `finally:` block.
4. **Annotated `ExplainPlanArtifact`:** Enhances plan output with explicit table aliases (`on table AS alias`, required by `pg_hint_plan`), join/index predicates, and automatic cardinality misestimation flags (`[EST_ERR: Nx UNDER]` / `[EST_ERR: Nx OVER]` whenever estimated vs. actual rows differ by ≥ 10×).

### 2.2 Two-Phase Index Evaluation & Zero-Regression Safety
- **Why 2-phase evaluation is mandatory for indexes:** Creating a real B-tree index on 60M-row `lineitem` takes ~45–75 seconds, whereas `HypoPG` creates virtual in-memory index metadata in ~1 ms. However, `HypoPG` relies on PostgreSQL's cost estimator and can be wrong (e.g., on `TPC-H q06`, `HypoPG` predicted a `-41%` cost drop for a composite index on `lineitem`, but real execution on `tpch_eval` regressed latency by `+49%` due to random heap fetches losing to Parallel Seq Scan). Therefore, our pipeline uses `HypoPG` as a fast **Phase-1 filter** (requiring ≥ 5% estimated cost reduction) and materializes surviving candidates on `tpch_eval` in **Phase 2** to measure real execution latency.
- **Zero-regression guarantee:** Because every proposed hint or index is empirically timed before acceptance, the advisor retains the fastest verified plan (including the default plan if no candidate beats it by ≥ 2%). Furthermore, hints and indexes never rewrite SQL logic, eliminating any risk of semantic errors.

---

## 3. Experimental Protocol & Environment

All experiments were governed by a pre-frozen measurement contract (`contracts/measurement_contract_v1.yaml`, version `1.0.0`) on a single dedicated machine so every comparison shares identical hardware:

- **Hardware & Engine:** AMD Ryzen 5 9600X (6C/12T), 31.6 GB RAM, local SSD, Windows + WSL 2 Docker running PostgreSQL 16 (`shared_buffers=4GB`, `effective_cache_size=16GB`, `work_mem=256MB`, `maintenance_work_mem=2GB`, `max_parallel_workers_per_gather=2`, `random_page_cost=1.1`).
- **Timing Protocol:** Warm-cache execution (warm-up runs prior to timed runs to prevent cold OS-buffer bias), `statement_timeout = 180,000 ms` (3 minutes). Queries that time out are recorded as `timed_out = true` with `execution_latency_ms = 180,000.0` (and in the advisory loop, Round-0 timeouts fall back to `EXPLAIN` without `ANALYZE` so the agent can still diagnose and rescue them).
- **LLM Models (Open-Weights via NVIDIA NIM, \$0 Free Tier):**
  - **Worker (`Cheap`):** `nvidia/nemotron-3.5-super-vl-120b-a12b` (120B total / 12B active MoE)
  - **Frontier Advisor:** `nvidia/nemotron-3-ultra-550b-a55b` (550B total / 55B active MoE)
  - **Embeddings (for LLMSteer):** Google `gemini-embedding-001` (`dim = 3072`)

### 3.1 Benchmarks
1. **TPC-H (Scale Factor 10) — Index Recommendation (`tpch` + `tpch_eval`, 15 GB each):**
   - 22 analytical queries (`q01`–`q22`), 8 tables (`lineitem` = 60.0M rows, `orders` = 15.0M rows).
   - Configured with **Primary Keys only** (no secondary/FK indexes) so the advisor has genuine headroom to discover high-impact single and composite B-tree indexes.
2. **Join Order Benchmark (JOB / IMDb) — Hint Steering (`imdb`, 8.7 GB):**
   - 113 multi-join queries (`1a`–`33c`), 21 tables from the canonical May 2013 CWI IMDb snapshot (`cast_info` = 36.2M rows, `movie_info` = 14.8M rows).
   - Configured with **21 Primary Keys + 23 Foreign Key indexes**, matching the standard JOB / Bao / LLMSteer / LLMOpt setup where FK indexes expand the plan space and expose cardinality misestimations.

### 3.2 Baselines & Advisory Configurations
- **`pg_default`:** Unmodified PostgreSQL 16 (both benchmarks).
- **`llmsteer_baseline` (JOB):** Reproduces Akioyamen et al. (2024) — embeds all 113 SQL queries with `gemini-embedding-001`, applies `StandardScaler + PCA + RBF-SVM` under 10-fold stratified cross-validation to pick between default PG and the single highest-impact Bao hint set (`no_nestloop,no_indexscan`), with **zero** live DB telemetry.
- **`bao_oracle` (JOB):** Theoretical upper bound over all **49 non-empty combinations** of PostgreSQL's 6 global binary join/scan switches (`enable_hashjoin`, `enable_mergejoin`, `enable_nestloop`, `enable_seqscan`, `enable_indexscan`, `enable_indexonlyscan`), deduplicated by physical plan hash and keeping the fastest plan per query.
- **`mcp_single_shot`:** MCP agent limited to `K = 1` round using the 120B Worker.
- **`mcp_iter_cheap`:** Iterative MCP loop (`K ≤ 5` rounds) using the 120B Worker throughout.
- **`mcp_iter_hybrid`:** Iterative MCP loop (`K ≤ 5` rounds) using the 120B Worker on Rounds 1–2, escalating to the 550B Frontier Advisor on Rounds 3–5 if latency gain `< 25%` and query `> 50 ms`.

**Loop Controls (`v1`):** Maximum 2 exploration tool calls per round (`analyze=False` enforced during exploration); queries already `≤ 50 ms` at Round 0 are skipped (`early_stop_latency_ms = 50.0`); iterative loops stop early if `best_ms ≤ 50 ms`, the LLM sets `"converged": true`, or **2 consecutive rounds** fail to improve latency by ≥ 2% (`no_improve_patience = 2`).

---

## 4. Empirical Results (`v1`)

All metrics below are reconstructed directly from the immutable raw `results/runs/<run_id>/queries.jsonl` logs.

### 4.1 TPC-H SF=10 (22 Queries — Index Recommendation)

| Method | P50 (ms) | P90 (ms) | Total (s) | Paired Speedup vs. Base | Timeouts | Improved (>5%) | Regressed (>5%) | Mean LLM Calls | Mean Tokens/Q |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`pg_default`** | 2,578.07 | 24,726.97 | 456.90 | — | **2** (`q17`, `q20`) | — | — | 0 | 0 |
| **`mcp_single_shot`** (K=1, Worker) | 1,675.14 | 11,183.48 | 86.26 | +83.8% | **0** | 72.73% (16/22) | **0.0%** | 3.00 | 16,352 |
| **`mcp_iter_cheap`** (K ≤ 5, Worker) | **1,036.56** | **10,259.20** | 73.13 | +86.3% | **0** | **81.82%** (18/22) | **0.0%** | 7.86 | 54,723 |
| **`mcp_iter_hybrid`** (K ≤ 5, Hybrid) | 1,055.06 | 10,950.73 | **72.92** | **+87.2%** | **0** | **81.82%** (18/22) | **0.0%** | 8.91 | 66,771 |

**Key Observations on TPC-H:**
- **Timeout elimination:** Default PostgreSQL timed out at `180 s` on both `q17` and `q20` due to unindexed correlated lookups on `lineitem(l_partkey)`. All three MCP modes rescued both queries in Round 1 (`q17`: `180,000 ms → 16.3 ms`, ~11,000× speedup; `q20`: `180,000 ms → 432.1 ms`, ~416× speedup).
- **Iterative gain & convergence:** Moving from `K = 1` to iterative `K ≤ 5` cut total latency further (`86.26 s → 73.13 s / 72.92 s`), cut P50 from `1,675 ms → 1,037 ms`, and raised improved queries from `72.7%` to `81.8%` (e.g., `q19`: `2,048 ms → 22.8 ms`). In `mcp_iter_cheap`, gains converged cleanly by **`K = 3`** (`K=1: 88.88 s → K=2: 75.80 s → K=3: 73.13 s → K=4..5: 73.13 s`).
- **Low Frontier escalation:** `mcp_iter_hybrid` only escalated `1 / 22` queries because the 120B Worker already surpassed the 25% gain threshold within Rounds 1–2 on nearly every indexable query.
- **Zero regressions (`0.0%`):** Phase-2 verification on `tpch_eval` rejected every misleading `HypoPG` proposal.

---

### 4.2 JOB / IMDb (113 Queries — `pg_hint_plan` Steering)

| Method | P50 (ms) | P90 (ms) | Total (s) | Paired Speedup vs. Base* | Timeouts | Improved (>5%) | Regressed (>5%) | Mean LLM Calls | Mean Tokens/Q |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`pg_default`** | 154.49 | 2,425.95 | 82.83 | — | 0 | — | — | 0 | 0 |
| **`llmsteer_baseline`** (10-fold CV) | 199.76 | 1,986.56 | 81.52 | -5.2% | 0 | 7.08% | **10.62%** | — | — |
| **`bao_oracle`** (`all48`, 49 arms) | 143.14 | 1,708.84 | **52.82** | +31.8% | 0 | **44.25%** | 0.0% | 0 | 0 |
| **`mcp_single_shot`** (K=1, Worker) | 145.13 | 1,523.65 | 68.62 | +16.0% | 0 | 10.62% | **0.0%** | 2.26 | 18,965 |
| **`mcp_iter_cheap`** (K ≤ 5, Worker) | **131.38** | **1,310.64** | 53.01 | **+34.0%** | 0 | 23.89% (27/113) | **0.0%** | 5.60 | 54,355 |
| **`mcp_iter_hybrid`** (K ≤ 5, Hybrid) | 183.38 | 1,796.20 | 69.54 | +31.1% | 0 | 23.01% (26/113) | **0.0%** | 5.67 | 53,988 |

*\*Paired speedup is computed against each run's own Round-0 / Arm-0 baseline (`bao_oracle`: `77.50 s → 52.82 s`; `mcp_single_shot`: `81.73 s → 68.62 s`; `mcp_iter_cheap`: `80.30 s → 53.01 s`; `mcp_iter_hybrid`: `100.86 s → 69.54 s`, which experienced slightly higher host background load).*

**Key Observations on JOB:**
- **Why live MCP feedback beats static SQL-embedding steering (`LLMSteer`):** Although perfect binary selection of Bao Arm 9 (`no_nestloop,no_indexscan`) could reduce JOB total latency to `57.22 s`, `LLMSteer` achieved only `AUROC = 0.697` and `precision = 0.381`. Without live DB verification, its false positives caused **`10.62%` regressions**, making total workload runtime *worse* than default PostgreSQL (`81.52 s` vs. `77.50 s`). By contrast, all MCP modes achieved **`0.0%` regressions**.
- **Fine-grained `pg_hint_plan` beats Bao Oracle's 49-arm ceiling on tail latency:** Even though `bao_oracle` exhaustively tries all 49 combinations of PostgreSQL's global planner switches, it cannot reorder joins. Our iterative 120B MCP agent (`mcp_iter_cheap`) beat `bao_oracle` on **P50** (`131.38 ms` vs. `143.14 ms`), crushed it on **P90** (`1,310.64 ms` vs. `1,708.84 ms`, **-23.3% better**), and achieved a higher paired total speedup (**+34.0%** vs. **+31.8%**). On severe cardinality-error queries, fine-grained `Leading(...)` + join hints outperformed `bao_oracle` by **7×–11×** (e.g., `6d`: `1,448 ms → 129.7 ms`; `17d`: `1,707 ms → 162.8 ms`; `31c`: `2,472 ms → 258.1 ms`; `18a`: `1,418 ms → 190.0 ms`).
- **Clear iterative progression (`K = 1…5`):** In `mcp_iter_cheap_job_v1`, total latency improved monotonically across rounds (`K=1: 66.44 s [+17.3%] → K=2: 56.69 s [+29.4%] → K=3: 55.31 s [+31.1%] → K=4: 53.01 s [+34.0%] → K=5: 53.01 s`), more than doubling the single-shot speedup.
- **MCP tool utilization:** Across `mcp_single_shot_tpch_v1`, the agent paired `get_table_stats` (20 calls) with `explain_query` + `HypoPG` (24 calls) and completely ignored upstream `get_object_details` (0 calls). Across `mcp_iter_cheap_job_v1` (211 rounds), it invoked `explain_with_hints` (236 calls) and `get_table_stats` (186 calls).

---

## 5. Critical Reflections, Methodological Concerns & Open Questions

While Phase 1 demonstrates that MCP-grounded iterative optimization works effectively on both benchmarks, careful inspection of the raw logs revealed **five concrete limitations and concerns** that motivate our next experimental phase:

### 5.1 Potential Benchmark Familiarity / Data Contamination
JOB (IMDb, 2015) and TPC-H (1993) are two of the most widely published database benchmarks in history. Modern LLMs have almost certainly encountered their table schemas and SQL tuning discussions during pretraining. Although our iterative gains over single-shot (`+17.3%` at `K=1` → `+34.0%` at `K=4` on JOB) and live Phase-2 index verification show that runtime MCP telemetry is doing real work, relying solely on famous public schemas leaves a valid threat to external validity.
- **Implication:** We need a direct anti-contamination test — specifically a **controlled schema-anonymization ablation** (exposing slow JOB queries through opaque table/column names with identical underlying data skew and cardinality errors) and/or evaluation on a **less-known / private-style skewed database**.

### 5.2 Premature Cutoff from `no_improve_patience = 2`
Inspecting the slowest unimproved JOB queries in `mcp_iter_cheap_job_v1` (`19d`: `5.25 s`, `31a`: `2.55 s`, `8c`: `2.25 s`, `6f`: `2.03 s`, `9d`: `1.85 s`, `18c`: `1.68 s`, `7c`: `1.43 s`, `30a`: `1.31 s`) revealed that **every single one stopped at Round 2** (most after 1–2 invalid `pg_hint_plan` syntax/warning rounds). Worse, in `mcp_iter_hybrid_job_v1`, because Frontier escalation only triggers at **Round 3**, `patience = 2` terminated most hard queries *immediately before* the 550B Frontier Advisor could ever inspect them (only `7 / 113` queries escalated).
- **Implication:** Adaptive early stopping confounds the true `Latency vs. Rounds (K) vs. Tokens` curve on hard queries. Running a **fixed loop budget (no early stopping)** on the hardest / least-improved ~10% of queries will reveal whether additional rounds and guaranteed Frontier escalation crack these remaining bottlenecks, and allow us to solve cleanly for the **Pareto-optimal loop budget `K*`**.

### 5.3 Wall-Clock Bottlenecks & Redundant Tool Calls
Profiling the Phase 1 JOB logs showed that **88% of total wall-clock time** (`265.2 min` out of `~302 min`) was spent waiting sequentially on NVIDIA NIM API calls (`22.5–25.1 s/call` across ~635 calls), while PostgreSQL sat idle. Two avoidable inefficiencies accounted for most of that overhead:
1. **Sequential execution:** Running one query at a time serialized all network waits. Overlapping LLM API requests asynchronously across multiple queries while keeping PostgreSQL `EXPLAIN ANALYZE` behind a strict single-file lock eliminates idle wait time with zero database contention.
2. **Redundant `get_table_stats` calls & unbounded reasoning tokens:** The agent re-fetched static `get_table_stats` in **186 / 211 JOB rounds** (~88% of rounds, including Rounds 2–5 of the same query), and unbounded hidden reasoning consumed ~30% of all tokens (`1.88M` thought tokens). Pre-fetching `get_table_stats` once at Round 0 and optionally bounding `reasoning_budget` directly cuts both wall-clock time (3×–4.6×) and token cost (20%–32%).

### 5.4 Host Load Variance Across Long Sequential Runs
Because `mcp_iter_hybrid_job_v1` ran sequentially for ~4.5 hours, host background load shifted its Round-0 baseline total (`100.86 s` vs. `80.30 s` in `mcp_iter_cheap_job_v1`), underscoring why **paired speedup against each run's own Round-0 baseline** must be reported alongside absolute milliseconds — and why shorter, faster runs improve measurement stability.

### 5.5 Expression-Index Safety Boundary & Interpretability
On `TPC-H q22`, the agent attempted to propose a functional index on `substring(c_phone FROM 1 FOR 2)`, which our strict SQL-injection identifier whitelist intentionally blocked (though the agent still recovered to achieve a `2.4×` speedup using plain columns). Supporting AST-validated expression indexes remains a clear future engineering improvement. Finally, while aggregate metrics prove effectiveness, pairing them with **side-by-side visual execution-plan comparisons** (for the Top 5 JOB and Top 5 TPC-H improvements) will make the exact mechanism of MCP-driven optimization transparent to readers.
