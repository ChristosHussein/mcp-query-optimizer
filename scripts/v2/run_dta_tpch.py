"""Non-LLM Classical Heuristic DTA Baseline on TPC-H (DatabaseTuningAdvisor + Phase-2 tpch_eval timing).

Evaluates postgres-mcp's greedy Pareto HypoPG index tuner (DatabaseTuningAdvisor, 0 LLM tokens)
under the exact same Round-0 warm-cache baseline and Phase-2 isolated tpch_eval timing protocol as v2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

from postgres_mcp.advisory import evaluate_real_indexes
from postgres_mcp.index.dta_calc import DatabaseTuningAdvisor
from postgres_mcp.sql import DbConnPool, SqlDriver

ROOT = Path(__file__).resolve().parents[2]
TPCH_TOP8 = ["q17", "q20", "q18", "q13", "q01", "q09", "q10", "q03"]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def measure_baseline_sync(dsn: str, sql: str, timeout_ms: int) -> tuple[float, float | None, bool]:
    clean_sql = sql.strip().rstrip(";")
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Estimated cost first (always fast)
            cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                cur.execute(f"EXPLAIN (FORMAT JSON) {clean_sql}")
                est_cost = float(cur.fetchone()[0][0]["Plan"]["Total Cost"])
            finally:
                conn.rollback()

            # 1 warm-up + 1 timed run (matching v2 run_v2.py protocol)
            for run_i in range(2):
                cur.execute("BEGIN TRANSACTION READ ONLY")
                try:
                    cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                    exec_ms = float(cur.fetchone()[0][0]["Execution Time"])
                except psycopg.errors.QueryCanceled:
                    conn.rollback()
                    return float(timeout_ms), est_cost, True
                finally:
                    conn.rollback()
            return round(exec_ms, 3), est_cost, False


async def run_dta_benchmark(args: argparse.Namespace) -> None:
    load_dotenv(ROOT / ".env")
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    tpch_dsn = f"postgresql://postgres:{pw}@{host}:{port}/tpch"
    eval_dsn = f"postgresql://postgres:{pw}@{host}:{port}/tpch_eval"

    qdir = ROOT / "benchmarks" / "tpch" / "queries"
    qids = args.queries if args.queries else TPCH_TOP8
    queries = [(qid, (qdir / f"{qid}.sql").read_text(encoding="utf-8")) for qid in qids]

    run_id = args.run_id
    out_dir = ROOT / "results" / "baselines" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_jsonl = out_dir / "queries.jsonl"
    commit = git_commit()

    tpch_pool = DbConnPool(tpch_dsn)
    eval_pool = DbConnPool(eval_dsn)
    await tpch_pool.pool_connect()
    await eval_pool.pool_connect()

    try:
        driver = SqlDriver(conn=tpch_pool)
        with out_jsonl.open("w", encoding="utf-8") as fh:
            for idx, (qid, sql) in enumerate(queries, 1):
                base_ms, base_cost, base_to = measure_baseline_sync(tpch_dsn, sql, args.timeout_s * 1000)

                r0: dict[str, Any] = {
                    "run_id": run_id,
                    "git_commit": commit,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "benchmark": "tpch",
                    "query_id": qid,
                    "method_id": "dta_baseline",
                    "round_index": 0,
                    "model_advisor": None,
                    "model_worker": None,
                    "model_used_this_round": None,
                    "proposed_action": None,
                    "action_valid": True,
                    "estimated_cost": base_cost,
                    "execution_latency_ms": base_ms,
                    "timed_out": base_to,
                    "llm_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "thought_tokens": 0,
                    "llm_wall_time_ms": 0.0,
                }
                fh.write(json.dumps(r0) + "\n")

                dta = DatabaseTuningAdvisor(
                    driver,
                    max_runtime_seconds=args.dta_max_s,
                    max_index_width=3,
                )
                res = await dta.analyze_workload(query_list=[sql])
                recs = [{"table": r.table, "columns": list(r.columns), "using": r.using} for r in res.recommendations]

                if not recs:
                    r1: dict[str, Any] = {
                        "run_id": run_id,
                        "git_commit": commit,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "benchmark": "tpch",
                        "query_id": qid,
                        "method_id": "dta_baseline",
                        "round_index": 1,
                        "model_advisor": None,
                        "model_worker": None,
                        "model_used_this_round": "dta_greedy_hypopg",
                        "proposed_action": [],
                        "action_valid": False,
                        "estimated_cost": base_cost,
                        "execution_latency_ms": None,
                        "timed_out": False,
                        "feedback": res.error or "DTA greedy search found no index meeting Pareto space/cost threshold.",
                        "llm_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "thought_tokens": 0,
                        "llm_wall_time_ms": 0.0,
                    }
                    fh.write(json.dumps(r1) + "\n")
                    fh.flush()
                    print(f"[{idx}/{len(queries)}] {qid} | base={base_ms:.1f} ms -> DTA proposed 0 indexes (best={base_ms:.1f} ms)")
                    continue

                try:
                    p2 = await evaluate_real_indexes(
                        eval_pool,
                        sql,
                        recs,
                        warmup_runs=1,
                        timed_runs=1,
                        timeout_ms=args.timeout_s * 1000,
                    )
                    exec_ms = float(p2["median_execution_ms"])
                    valid = True
                    timed_out = False
                    est_cost = p2.get("total_cost")
                    fb = f"DTA Phase-2 materialized in {p2['index_build_times_ms']} ms -> actual {exec_ms} ms"
                except Exception as exc:
                    is_to = "canceling statement" in str(exc).lower() or "timeout" in str(exc).lower()
                    exec_ms = float(args.timeout_s * 1000) if is_to else None
                    valid = False
                    timed_out = is_to
                    est_cost = None
                    fb = f"DTA Phase-2 error: {exc}"

                r1 = {
                    "run_id": run_id,
                    "git_commit": commit,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "benchmark": "tpch",
                    "query_id": qid,
                    "method_id": "dta_baseline",
                    "round_index": 1,
                    "model_advisor": None,
                    "model_worker": None,
                    "model_used_this_round": "dta_greedy_hypopg",
                    "proposed_action": recs,
                    "action_valid": valid,
                    "estimated_cost": est_cost,
                    "execution_latency_ms": exec_ms,
                    "timed_out": timed_out,
                    "feedback": fb,
                    "llm_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "thought_tokens": 0,
                    "llm_wall_time_ms": 0.0,
                }
                fh.write(json.dumps(r1) + "\n")
                fh.flush()
                best_ms = min(base_ms, exec_ms) if (valid and exec_ms is not None) else base_ms
                print(f"[{idx}/{len(queries)}] {qid} | base={base_ms:.1f} ms -> dta={exec_ms} ms (best={best_ms:.1f} ms) | recs={recs}")
    finally:
        await tpch_pool.close()
        await eval_pool.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default="dta_tpch_top8_v2")
    p.add_argument("--dta-max-s", type=int, default=60)
    p.add_argument("--timeout-s", type=int, default=180)
    p.add_argument("--queries", nargs="*")
    args = p.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_dta_benchmark(args))


if __name__ == "__main__":
    main()
