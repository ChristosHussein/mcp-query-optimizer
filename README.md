# MCP-Driven Query Optimization Advisory for Relational Databases

MSc Thesis repository implementing an MCP-based agentic query optimization advisor over PostgreSQL 16 (`pg_hint_plan` + `HypoPG`), evaluated on **JOB (IMDb, 113 queries)** and **TPC-H (SF=10, 22 queries)**.

## Repository Layout
- `docker/` — PostgreSQL 16 image with `pg_hint_plan 1.6.3`, `HypoPG 1.4.5`, and `pg_stat_statements`, plus config & smoke test.
- `benchmarks/job/` — CWI IMDb schema, 23 FK indexes (`fkindexes.sql`), and 113 JOB queries.
- `benchmarks/tpch/` — Standard TPC-H schema, Primary Keys only (`pkeys.sql`), and 22 TPC-H queries.
- `src/postgres_mcp/` — Extended fork of `crystaldba/postgres-mcp` (MIT) adding:
  - `explain_with_hints` (`pg_hint_plan` + exact `used`/`not_used`/`warnings` diagnostics)
  - `get_table_stats` (batched `pg_stats`, table sizes, existing indexes)
  - `evaluate_real_indexes` (Phase-2 index materialization strictly guarded to `*_eval` clone DBs)
  - Enhanced `ExplainPlanArtifact` showing table `AS` aliases, `Index Name`, join conditions, and `[EST_ERR: Nx UNDER/OVER]` markers
- `src/advisor/` — MCP-driven agentic advisory loop using the Gemini Interactions API (`google-genai`), supporting single-shot, Cheap+Cheap (`gemini-3.8-flash`), Hybrid escalation (`gemini-3.8-flash` worker + `gemini-4-argon` advisor), and Frontier-only modes.
- `scripts/` — Reproducible dataset loaders (`load_imdb.py`, `load_tpch.py`) and runners (`run_pg_default.py`, `run_bao_oracle.py`, `run_llmsteer.py`, `run_advisor.py`).
- `tests/test_mcp_advisory.py` — Unit + live PostgreSQL + MCP stdio integration checks.
- `measurement_contract.yaml` — Pre-experiment scientific integrity contract.

## Quick Start
```powershell
# 1. Start PostgreSQL 16 + pg_hint_plan + HypoPG
docker compose up -d --build
Get-Content -Raw docker/smoke_test.sql | docker exec -i thesis-pg16 psql -U postgres -d imdb -v ON_ERROR_STOP=1

# 2. Sync Python 3.12 environment
uv sync --python 3.12

# 3. Load benchmarks (IMDb ~8.7 GB, TPC-H SF=10 ~15 GB + tpch_eval clone)
uv run python scripts/load_imdb.py
uv run python scripts/load_tpch.py --sf 10

# 4. Run MCP advisory tests
uv run python tests/test_mcp_advisory.py

# 5. Configure Gemini API key (copy .env.example -> .env) and run advisor/baselines
uv run python scripts/run_pg_default.py --benchmark job
uv run python scripts/run_bao_oracle.py --arms all48
uv run python scripts/run_advisor.py --benchmark job --method mcp_iter_hybrid --queries 17b 18c
```
