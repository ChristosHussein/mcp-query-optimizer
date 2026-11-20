# MCP-Driven Query Optimization Advisory for Relational Databases

MSc Thesis repository implementing an MCP-based agentic query optimization advisor over PostgreSQL 16 (`pg_hint_plan` + `HypoPG`), evaluated on **JOB (IMDb)** and **TPC-H (SF=10)**.

## Repository Layout
- `docker/` — PostgreSQL 16 image with `pg_hint_plan`, `HypoPG`, and `pg_stat_statements`, plus config & smoke test.
- `docker-compose.yml` — Local reproducible database service.
- `measurement_contract.yaml` — Pre-experiment metric definitions, protocols, and logging contract.
- `References/` — Core reference papers (JOB, Bao, LLMSteer, LLMOpt).
- `Thesis_Timeline.pdf` — Project schedule (23/11/2026 – 14/03/2027).

## Quick Start (Database Container)
```powershell
cp .env.example .env
docker compose up -d --build
docker exec -i thesis-pg16 psql -U postgres -v ON_ERROR_STOP=1 < docker/smoke_test.sql
```
