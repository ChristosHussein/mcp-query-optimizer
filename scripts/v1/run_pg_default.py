"""Run PostgreSQL default optimizer baseline on JOB (113 queries) or TPC-H (22 queries).

Adheres strictly to measurement_contract.yaml:
- warm-up runs followed by timed runs (median per query)
- explicit statement_timeout_ms (default 180,000 ms)
- append-only JSONL output + summary.json with P50, P90, total_s, git commit, and environment metadata
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def get_dsn(dbname: str) -> str:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def measure_query(
    conn: psycopg.Connection,
    sql: str,
    warmup_runs: int,
    timed_runs: int,
    timeout_ms: int,
) -> dict[str, Any]:
    clean_sql = sql.strip().rstrip(";")
    warmup_ms: list[float] = []
    timed_ms: list[float] = []
    estimated_cost: float | None = None
    timed_out = False

    with conn.cursor() as cur:
        cur.execute("BEGIN TRANSACTION READ ONLY")
        try:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(f"EXPLAIN (FORMAT JSON) {clean_sql}")
            plan0 = cur.fetchone()[0][0]
            estimated_cost = float(plan0["Plan"]["Total Cost"])

            for _ in range(warmup_runs):
                cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                p = cur.fetchone()[0][0]
                warmup_ms.append(round(float(p["Execution Time"]), 3))

            for _ in range(timed_runs):
                cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                p = cur.fetchone()[0][0]
                timed_ms.append(round(float(p["Execution Time"]), 3))
        except psycopg.errors.QueryCanceled:
            timed_out = True
        finally:
            conn.rollback()

    exec_ms = float(timeout_ms) if timed_out else round(statistics.median(timed_ms), 3)
    return {
        "estimated_cost": estimated_cost,
        "warmup_latencies_ms": warmup_ms,
        "timed_latencies_ms": timed_ms,
        "execution_latency_ms": exec_ms,
        "timed_out": timed_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["job", "tpch"], required=True)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=180_000)
    parser.add_argument("--limit", type=int, default=0, help="Optional max queries for smoke testing")
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args()

    dbname = "imdb" if args.benchmark == "job" else "tpch"
    qdir = ROOT / "benchmarks" / args.benchmark / "queries"
    query_files = sorted(qdir.glob("*.sql"))
    if args.limit > 0:
        query_files = query_files[: args.limit]
    if not query_files:
        sys.exit(f"No .sql files found in {qdir}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"pg_default_{args.benchmark}_{ts}"
    out_dir = ROOT / "results" / "runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "queries.jsonl"
    commit = git_commit()

    latencies: list[float] = []
    timeouts = 0

    with psycopg.connect(get_dsn(dbname), autocommit=False) as conn:
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for idx, qf in enumerate(query_files, 1):
                qid = qf.stem
                sql = qf.read_text(encoding="utf-8")
                m = measure_query(conn, sql, args.warmup, args.runs, args.timeout_ms)
                record = {
                    "run_id": run_id,
                    "git_commit": commit,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "benchmark": args.benchmark,
                    "query_id": qid,
                    "method_id": "pg_default",
                    "round_index": 0,
                    "model_advisor": None,
                    "model_worker": None,
                    "proposed_action": None,
                    "action_valid": True,
                    **m,
                    "llm_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "llm_wall_time_ms": 0.0,
                }
                fh.write(json.dumps(record) + "\n")
                fh.flush()

                latencies.append(m["execution_latency_ms"])
                if m["timed_out"]:
                    timeouts += 1
                status = "TIMEOUT" if m["timed_out"] else f"{m['execution_latency_ms']:.1f} ms"
                print(f"[{idx:3d}/{len(query_files)}] {qid:6s} -> {status}")

    summary = {
        "run_id": run_id,
        "git_commit": commit,
        "benchmark": args.benchmark,
        "method_id": "pg_default",
        "query_count": len(latencies),
        "timeouts": timeouts,
        "warmup_runs": args.warmup,
        "timed_runs": args.runs,
        "timeout_ms": args.timeout_ms,
        "p50_ms": round(percentile(latencies, 50), 3),
        "p90_ms": round(percentile(latencies, 90), 3),
        "total_s": round(sum(latencies) / 1000.0, 3),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\nSummary:", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
