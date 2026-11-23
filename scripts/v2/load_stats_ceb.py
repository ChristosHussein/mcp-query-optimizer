"""Load real-world STATS-CEB (VLDB 2022, Stats StackExchange) into PostgreSQL 16 and extract queries."""

from __future__ import annotations

import argparse
import os
import statistics
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "stats_ceb" / "datasets" / "stats_simplified"
SQL_DIR = ROOT / "data" / "stats_ceb" / "scripts" / "sql"
WORKLOAD_FILE = ROOT / "data" / "stats_ceb" / "workloads" / "stats_CEB" / "stats_CEB.sql"
OUT_QDIR = ROOT / "benchmarks" / "stats_ceb" / "queries"

TABLE_CSVS = [
    ("users", "users.csv"),
    ("posts", "posts.csv"),
    ("postLinks", "postLinks.csv"),
    ("postHistory", "postHistory.csv"),
    ("comments", "comments.csv"),
    ("votes", "votes.csv"),
    ("badges", "badges.csv"),
    ("tags", "tags.csv"),
]


def get_dsn(dbname: str) -> str:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"


def load_db() -> None:
    with psycopg.connect(get_dsn("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'stats_ceb'")
            if cur.fetchone():
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'stats_ceb' AND pid <> pg_backend_pid()")
                cur.execute("DROP DATABASE stats_ceb")
            cur.execute("CREATE DATABASE stats_ceb")

    schema_sql = (DATA_DIR / "stats.sql").read_text(encoding="utf-8")
    index_sql = (SQL_DIR / "stats_index.sql").read_text(encoding="utf-8")

    with psycopg.connect(get_dsn("stats_ceb"), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            for tbl, csv_name in TABLE_CSVS:
                csv_path = DATA_DIR / csv_name
                with csv_path.open("rb") as fh:
                    with cur.copy(f"COPY {tbl} FROM STDIN WITH (FORMAT csv, HEADER true)") as copy:
                        while chunk := fh.read(1 << 20):
                            copy.write(chunk)
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                cnt = cur.fetchone()[0]
                print(f"  Loaded {tbl:12s}: {cnt:,} rows")
            cur.execute(index_sql)
        conn.commit()

    with psycopg.connect(get_dsn("stats_ceb"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE")
    print("VACUUM ANALYZE complete on stats_ceb.")

    OUT_QDIR.mkdir(parents=True, exist_ok=True)
    lines = [l.strip() for l in WORKLOAD_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, line in enumerate(lines, 1):
        sql = line.split("||", 1)[1].strip() if "||" in line else line
        (OUT_QDIR / f"s{i:03d}.sql").write_text(sql + "\n", encoding="utf-8")
    print(f"Extracted {len(lines)} STATS-CEB queries into {OUT_QDIR.relative_to(ROOT)}")


def find_slowest(top_n: int) -> None:
    files = sorted(OUT_QDIR.glob("*.sql"))
    timings: list[tuple[float, str]] = []
    with psycopg.connect(get_dsn("stats_ceb"), autocommit=False) as conn:
        with conn.cursor() as cur:
            for qf in files:
                sql = qf.read_text(encoding="utf-8").strip().rstrip(";")
                ms_runs: list[float] = []
                for _ in range(2):
                    cur.execute("BEGIN TRANSACTION READ ONLY")
                    try:
                        cur.execute("SET LOCAL statement_timeout = 60000")
                        cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
                        ms_runs.append(float(cur.fetchone()[0][0]["Execution Time"]))
                    except psycopg.errors.QueryCanceled:
                        ms_runs.append(60000.0)
                    finally:
                        conn.rollback()
                med = statistics.median(ms_runs)
                timings.append((med, qf.stem))
    timings.sort(reverse=True)
    print(f"\nTop {top_n} slowest STATS-CEB queries in PostgreSQL 16:")
    for ms, qid in timings[:top_n]:
        print(f"  {qid}: {ms:8.2f} ms")
    print("Query IDs:", [q for _, q in timings[:top_n]])


def main() -> None:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("--find-slowest", type=int, default=0, help="Benchmark all 146 queries and print Top N slowest")
    args = p.parse_args()
    if args.find_slowest > 0:
        find_slowest(args.find_slowest)
    else:
        load_db()


if __name__ == "__main__":
    main()
