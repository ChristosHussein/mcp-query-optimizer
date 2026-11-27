# MCP-Driven Query Optimization Advisory for Relational Databases

MSc Thesis repository implementing an MCP-based agentic query optimization advisor over PostgreSQL 16 (`pg_hint_plan` + `HypoPG`), evaluated across **JOB (IMDb, 113 queries)**, **TPC-H (SF=10, 22 queries)**, **Schema-Anonymized IMDb (`imdb_anon`, Top-12)**, and **STATS-CEB (VLDB 2022, Top-12)**.

## Repository Layout
- `docker/` — PostgreSQL 16 image with `pg_hint_plan 1.6.3`, `HypoPG 1.4.5`, and `pg_stat_statements 1.10`, plus tuning config & smoke test.
- `benchmarks/` — SQL workloads and schemas for `job` (113 queries + 23 FK indexes), `tpch` (22 queries, PKs only), `imdb_anon` (leak-free `t01..t21` / `c01..cN` clone), and `stats_ceb` (146 Stats StackExchange queries).
- `src/postgres_mcp/` — Extended fork of `crystaldba/postgres-mcp` (MIT) adding:
  - `explain_with_hints` (`pg_hint_plan` + binary `Leading(((a b) c))` normalizer + `HintReport` `used`/`not_used`/`warnings` diagnostics)
  - `get_table_stats` (batched `pg_stats`, table sizes, existing indexes)
  - `evaluate_real_indexes` (Phase-2 index materialization strictly guarded to `*_eval` clone DBs, with `pglast` AST validation for plain, expression, and partial `WHERE` indexes)
  - Enhanced `ExplainPlanArtifact` showing table `AS` aliases, `Index Name`, join conditions, and `[EST_ERR: Nx UNDER/OVER]` markers
  - Catalog-filtered `DatabaseTuningAdvisor` (`dta_calc.py`) non-LLM greedy `HypoPG` baseline
- `src/advisor/` — Phase-1 (`v1`) sequential MCP advisory loop (`mcp_single_shot`, `mcp_iter_cheap`, `mcp_iter_hybrid`, `mcp_iter_frontier`) supporting NVIDIA NIM (`nemotron-3.5-super-vl-120b-a12b` Worker + `nemotron-3-ultra-550b-a55b` Frontier) and Google Gemini APIs.
- `scripts/v1/` — Phase-1 runners (`run_pg_default.py`, `run_bao_oracle.py`, `run_llmsteer.py`, `run_advisor.py`, `summarize_runs.py`).
- `scripts/v2/` — Phase-2 async fixed-`K` engine (`run_v2.py` with DB serialization lock, dynamic exploration timeout cap, and Alternating `120B ↔ 550B` Hybrid), anti-contamination builders (`setup_imdb_anon.py`, `load_stats_ceb.py`), and non-LLM heuristic DTA runner (`run_dta_tpch.py`).
- `scripts/v3/` — Phase-3 spec-aligned runner (`run_v3.py` powered by NVIDIA NIM `nemotron-3.5-lightning-30b-a3b`, compact query-filtered Round-1 `get_table_stats`, `--all-queries` full JOB 113 / TPC-H 22 execution, fail-fast quota breaker, and `results/v3/` output).
- `thesis/` — Experiment reports (`v1_experiment_report.md`, `v2_experiment_report.md`, `case_studies.md`, `thesis_progress_and_retrospective_report.md`) and Pareto/anti-contamination/case-study figures.
- `results/` — Immutable raw `queries.jsonl` logs organized by campaign (`results/baselines/`, `results/v1/`, `results/v2/`).
- `contracts/` — Pre-experiment scientific integrity contracts (`measurement_contract_v1.yaml`, `measurement_contract_v2.yaml`).
- `tests/test_mcp_advisory.py` — Unit (`--unit-only`) + live PostgreSQL 16 + MCP stdio integration checks.

## Quick Start
```powershell
# 1. Start PostgreSQL 16 + pg_hint_plan + HypoPG
docker compose up -d --build
Get-Content -Raw docker/smoke_test.sql | docker exec -i thesis-pg16 psql -U postgres -d imdb -v ON_ERROR_STOP=1

# 2. Sync Python 3.12 environment
uv sync --python 3.12

# 3. Load benchmarks (IMDb ~8.7 GB, TPC-H SF=10 ~15 GB + tpch_eval clone, imdb_anon, stats_ceb)
uv run python scripts/load_imdb.py
uv run python scripts/load_tpch.py --sf 10
uv run python scripts/v2/setup_imdb_anon.py
uv run python scripts/v2/load_stats_ceb.py

# 4. Run unit + live MCP advisory integration tests
uv run python tests/test_mcp_advisory.py

# 5. Run baselines and v1 / v2 advisory loops (copy .env.example -> .env first)
uv run python scripts/v1/run_pg_default.py --benchmark job
uv run python scripts/v1/run_bao_oracle.py --arms all48
uv run python scripts/v2/run_dta_tpch.py --run-id dta_tpch_top8_v2
uv run python scripts/v2/run_v2.py --benchmark job --method v2_hybrid --fixed-rounds 6
```
