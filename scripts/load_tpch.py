"""Generate TPC-H (default SF=10) via DuckDB, load into PostgreSQL 16 (PKs only), clone tpch_eval, and verify all 22 queries."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import duckdb
import psycopg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "benchmarks" / "tpch" / "schema.sql"
PKEYS_SQL = ROOT / "benchmarks" / "tpch" / "pkeys.sql"
QUERIES_DIR = ROOT / "benchmarks" / "tpch" / "queries"

TABLES = [
    "region",
    "nation",
    "supplier",
    "part",
    "partsupp",
    "customer",
    "orders",
    "lineitem",
]


def get_dsn(dbname: str) -> str:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"


def export_queries() -> None:
    QUERIES_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch;")
    rows = con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall()
    assert len(rows) == 22, f"Expected 22 TPC-H queries, got {len(rows)}"
    for qnr, sql in rows:
        out = QUERIES_DIR / f"q{qnr:02d}.sql"
        out.write_text(sql.strip() + "\n", encoding="utf-8")
    con.close()
    print(f"[0/6] Exported 22 TPC-H queries to {QUERIES_DIR}")


def generate_csvs(sf: int, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = all((out_dir / f"{t}.csv").exists() for t in TABLES)
    if existing:
        print(f"[1/6] Reusing existing TPC-H SF={sf} CSVs in {out_dir}")
        return

    print(f"[1/6] Generating TPC-H SF={sf} data via DuckDB dbgen...")
    t0 = time.perf_counter()
    db_file = out_dir / f"_gen_sf{sf}.duckdb"
    if db_file.exists():
        db_file.unlink()

    con = duckdb.connect(str(db_file))
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute("SET threads = 6;")
    con.execute("SET memory_limit = '16GB';")
    con.execute(f"CALL dbgen(sf={sf});")
    print(f"  - dbgen(sf={sf}) finished in {time.perf_counter() - t0:.1f}s; exporting CSVs...")

    for table in TABLES:
        t_exp = time.perf_counter()
        csv_path = (out_dir / f"{table}.csv").as_posix()
        con.execute(
            f"COPY {table} TO '{csv_path}' (FORMAT CSV, DELIMITER '|', HEADER FALSE);"
        )
        print(f"  - exported {table}.csv ({time.perf_counter() - t_exp:.1f}s)")

    con.close()
    db_file.unlink(missing_ok=True)
    wal_file = out_dir / f"_gen_sf{sf}.duckdb.wal"
    wal_file.unlink(missing_ok=True)
    print(f"  - Total DuckDB generation + export time: {time.perf_counter() - t0:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sf", type=int, default=10, help="TPC-H scale factor (default: 10)")
    args = parser.parse_args()

    out_dir = ROOT / "data" / f"tpch_sf{args.sf}"
    container_dir = f"/data/tpch_sf{args.sf}"

    t_all = time.perf_counter()
    export_queries()
    generate_csvs(args.sf, out_dir)

    with psycopg.connect(get_dsn("tpch"), autocommit=True) as conn:
        with conn.cursor() as cur:
            print("[2/6] Creating TPC-H schema (8 tables)...")
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))

            print(f"[3/6] Bulk COPYing TPC-H SF={args.sf} CSVs into PostgreSQL...")
            for table in TABLES:
                t_tbl = time.perf_counter()
                cur.execute(
                    f"COPY {table} FROM '{container_dir}/{table}.csv' "
                    f"WITH (FORMAT csv, DELIMITER '|');"
                )
                cur.execute(f"SELECT count(*) FROM {table};")
                count = cur.fetchone()[0]
                print(f"  - {table:10s}: {count:>12,d} rows ({time.perf_counter() - t_tbl:.1f}s)")

            print("[4/6] Adding Primary Keys only (pkeys.sql)...")
            t_pk = time.perf_counter()
            cur.execute(PKEYS_SQL.read_text(encoding="utf-8"))
            print(f"  - Primary Keys built in {time.perf_counter() - t_pk:.1f}s")

            print("[5/6] Running VACUUM ANALYZE & verifying all 22 TPC-H queries...")
            t_vac = time.perf_counter()
            cur.execute("VACUUM ANALYZE;")
            print(f"  - VACUUM ANALYZE completed in {time.perf_counter() - t_vac:.1f}s")

            query_files = sorted(QUERIES_DIR.glob("q*.sql"))
            assert len(query_files) == 22, f"Expected 22 TPC-H queries, found {len(query_files)}"
            for qf in query_files:
                sql = qf.read_text(encoding="utf-8").strip().rstrip(";")
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql};")
                cur.fetchone()

            cur.execute("SELECT pg_size_pretty(pg_database_size('tpch'));")
            db_size = cur.fetchone()[0]
            print(f"  - All 22 TPC-H queries EXPLAIN cleanly! DB size = {db_size}")

    print("[6/6] Cloning pristine 'tpch' -> 'tpch_eval' for isolated Phase-2 index evaluation...")
    t_clone = time.perf_counter()
    with psycopg.connect(get_dsn("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname IN ('tpch', 'tpch_eval') AND pid <> pg_backend_pid();"
            )
            cur.execute("DROP DATABASE IF EXISTS tpch_eval;")
            cur.execute("CREATE DATABASE tpch_eval TEMPLATE tpch;")
    print(f"SUCCESS: tpch + tpch_eval ready! Clone took {time.perf_counter() - t_clone:.1f}s (total {time.perf_counter() - t_all:.1f}s)")


if __name__ == "__main__":
    main()
