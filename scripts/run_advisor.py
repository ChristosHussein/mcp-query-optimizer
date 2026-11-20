"""CLI entry point to run the MCP-driven Query Optimization Advisor on JOB or TPC-H."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

from dotenv import load_dotenv

from advisor import AdvisorConfig
from advisor import QueryAdvisorRunner

ROOT = Path(__file__).resolve().parent.parent


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Missing GEMINI_API_KEY. Copy .env.example to .env and set GEMINI_API_KEY.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", choices=["job", "tpch"], required=True)
    parser.add_argument(
        "--method",
        choices=["mcp_single_shot", "mcp_iter_cheap", "mcp_iter_hybrid", "mcp_iter_frontier"],
        default="mcp_iter_hybrid",
    )
    parser.add_argument("--worker-model", default="gemini-3.8-flash")
    parser.add_argument("--advisor-model", default="gemini-4-argon")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--queries", nargs="*", help="Optional specific query IDs (e.g. 17b 18c or q01 q09)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    qdir = ROOT / "benchmarks" / args.benchmark / "queries"
    files = sorted(qdir.glob("*.sql"))
    if args.queries:
        wanted = set(args.queries)
        files = [f for f in files if f.stem in wanted]
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        sys.exit("No matching benchmark queries found.")

    queries = [(f.stem, f.read_text(encoding="utf-8")) for f in files]
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    dbname = "imdb" if args.benchmark == "job" else "tpch"
    db_uri = f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"
    eval_uri = f"postgresql://postgres:{pw}@{host}:{port}/tpch_eval" if args.benchmark == "tpch" else None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"{args.method}_{args.benchmark}_{ts}"
    out_jsonl = ROOT / "results" / "runs" / run_id / "queries.jsonl"

    cfg = AdvisorConfig(
        benchmark=args.benchmark,
        method_id=args.method,
        worker_model=args.worker_model,
        advisor_model=args.advisor_model,
        max_rounds=args.max_rounds,
    )
    runner = QueryAdvisorRunner(cfg, database_uri=db_uri, eval_database_uri=eval_uri)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(runner.open_mcp_and_run(queries, run_id=run_id, git_commit=git_commit(), out_jsonl=out_jsonl))
    print(f"\nSaved run log to: {out_jsonl}")


if __name__ == "__main__":
    main()
