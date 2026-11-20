-- Smoke test verifying pg_hint_plan and HypoPG both work on PostgreSQL 16.
-- Run with: docker exec -i thesis-pg16 psql -U postgres -v ON_ERROR_STOP=1 < docker/smoke_test.sql

\echo '=== 1. Check loaded extensions & shared_preload_libraries ==='
SHOW shared_preload_libraries;
SELECT extname, extversion FROM pg_extension WHERE extname IN ('hypopg', 'pg_stat_statements');

\echo '=== 2. Create temporary test table ==='
CREATE TEMP TABLE t_smoke (id int PRIMARY KEY, val int, pad text);
INSERT INTO t_smoke SELECT g, g % 100, repeat('x', 50) FROM generate_series(1, 50000) g;
ANALYZE t_smoke;

\echo '=== 3. Verify pg_hint_plan forces SeqScan over PK IndexScan ==='
EXPLAIN (COSTS OFF) SELECT * FROM t_smoke WHERE id = 42;
/*+ SeqScan(t_smoke) */
EXPLAIN (COSTS OFF) SELECT * FROM t_smoke WHERE id = 42;

\echo '=== 4. Verify HypoPG creates a hypothetical index used by EXPLAIN ==='
SELECT indexrelid, indexname FROM hypopg_create_index('CREATE INDEX ON t_smoke (val)');
EXPLAIN (COSTS OFF) SELECT * FROM t_smoke WHERE val = 7;
SELECT hypopg_reset();

\echo '=== SMOKE TEST PASSED ==='
