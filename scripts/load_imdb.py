"""Load the CWI IMDb (JOB) dataset into PostgreSQL 16 with PK + FK indexes and verify all 113 queries."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "benchmarks" / "job" / "schema.sql"
FKINDEXES_SQL = ROOT / "benchmarks" / "job" / "fkindexes.sql"
QUERIES_DIR = ROOT / "benchmarks" / "job" / "queries"
LOCAL_CSV_DIR = ROOT / "data" / "imdb" / "csv"
CONTAINER_CSV_DIR = "/data/imdb/csv"

TABLES = [
    "aka_name",
    "aka_title",
    "cast_info",
    "char_name",
    "comp_cast_type",
    "company_name",
    "company_type",
    "complete_cast",
    "info_type",
    "keyword",
    "kind_type",
    "link_type",
    "movie_companies",
    "movie_info",
    "movie_info_idx",
    "movie_keyword",
    "movie_link",
    "name",
    "person_info",
    "role_type",
    "title",
]


def get_dsn(dbname: str = "imdb") -> str:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"


def main() -> None:
    for table in TABLES:
        csv_path = LOCAL_CSV_DIR / f"{table}.csv"
        if not csv_path.exists():
            sys.exit(f"Missing CSV file: {csv_path}. Extract imdb.tgz into {LOCAL_CSV_DIR} first.")

    t0 = time.perf_counter()
    with psycopg.connect(get_dsn("imdb"), autocommit=True) as conn:
        with conn.cursor() as cur:
            print("[1/5] Recreating IMDb schema (21 tables with Primary Keys)...")
            cur.execute(f"DROP TABLE IF EXISTS {', '.join(TABLES)} CASCADE;")
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))

            print("[2/5] Bulk COPYing 21 IMDb CSV tables...")
            for table in TABLES:
                t_tbl = time.perf_counter()
                cur.execute(
                    f"COPY {table} FROM '{CONTAINER_CSV_DIR}/{table}.csv' "
                    f"WITH (FORMAT csv, ESCAPE '\\');"
                )
                cur.execute(f"SELECT count(*) FROM {table};")
                count = cur.fetchone()[0]
                print(f"  - {table:18s}: {count:>10,d} rows ({time.perf_counter() - t_tbl:.1f}s)")

            print("[3/5] Creating 23 Foreign Key indexes (fkindexes.sql)...")
            t_idx = time.perf_counter()
            cur.execute(FKINDEXES_SQL.read_text(encoding="utf-8"))
            print(f"  - Indexes built in {time.perf_counter() - t_idx:.1f}s")

            print("[4/5] Running VACUUM ANALYZE...")
            t_vac = time.perf_counter()
            cur.execute("VACUUM ANALYZE;")
            print(f"  - VACUUM ANALYZE completed in {time.perf_counter() - t_vac:.1f}s")

            print("[5/5] Verifying all 113 JOB queries EXPLAIN cleanly...")
            query_files = sorted(QUERIES_DIR.glob("*.sql"))
            assert len(query_files) == 113, f"Expected 113 JOB queries, found {len(query_files)}"
            for qf in query_files:
                sql = qf.read_text(encoding="utf-8").strip().rstrip(";")
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql};")
                cur.fetchone()

            cur.execute("SELECT pg_size_pretty(pg_database_size('imdb'));")
            db_size = cur.fetchone()[0]
            print(f"SUCCESS: All 113 JOB queries EXPLAIN cleanly! DB size = {db_size} (total {time.perf_counter() - t0:.1f}s)")


if __name__ == "__main__":
    main()
