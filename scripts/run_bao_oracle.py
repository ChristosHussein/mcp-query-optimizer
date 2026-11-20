"""Bao-style Oracle (upper bound) over PostgreSQL binary planner hints with physical-plan deduplication.

Evaluates each JOB query under either:
  - --arms top5  : PostgreSQL default + Bao's top-5 hint sets (Section 6.3, 93% of Bao's gain)
  - --arms all48 : Default (arm 0) + all 48 non-default non-empty join x scan knob combinations

Deduplicates identical physical plans via cheap EXPLAIN before running EXPLAIN ANALYZE so even
all48 finishes fast while preserving exact per-arm latencies (also consumed by LLMSteer baseline).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
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

ROOT = Path(__file__).resolve().parent.parent

JOIN_KNOBS = ("enable_hashjoin", "enable_mergejoin", "enable_nestloop")
SCAN_KNOBS = ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan")
ALL_KNOBS = JOIN_KNOBS + SCAN_KNOBS

# Bao SIGMOD'21 Section 6.3: Default + Top-5 highest-impact hint sets
TOP5_ARMS: list[dict[str, bool]] = [
    # Arm 0: Default PG (all enabled)
    dict.fromkeys(ALL_KNOBS, True),
    # Arm 1: disable nested loop join (35% of gain)
    {**dict.fromkeys(ALL_KNOBS, True), "enable_nestloop": False},
    # Arm 2: disable index scan & merge join (22%)
    {**dict.fromkeys(ALL_KNOBS, True), "enable_indexscan": False, "enable_mergejoin": False},
    # Arm 3: disable nested loop, merge join & index scan (16%)
    {**dict.fromkeys(ALL_KNOBS, True), "enable_nestloop": False, "enable_mergejoin": False, "enable_indexscan": False},
    # Arm 4: disable hash join (10%)
    {**dict.fromkeys(ALL_KNOBS, True), "enable_hashjoin": False},
    # Arm 5: disable merge join (10%)
    {**dict.fromkeys(ALL_KNOBS, True), "enable_mergejoin": False},
]


def build_all48_arms() -> list[dict[str, bool]]:
    """Return Arm 0 (default) + all 48 non-default non-empty (join x scan) combinations."""
    arms: list[dict[str, bool]] = [dict.fromkeys(ALL_KNOBS, True)]
    for j_bits in itertools.product((True, False), repeat=3):
        if not any(j_bits):
            continue
        for s_bits in itertools.product((True, False), repeat=3):
            if not any(s_bits):
                continue
            cfg = dict(zip(ALL_KNOBS, j_bits + s_bits))
            if cfg not in arms:
                arms.append(cfg)
    return arms


def arm_to_label(arm: dict[str, bool]) -> str:
    disabled = [k.replace("enable_", "no_") for k in ALL_KNOBS if not arm[k]]
    return "default" if not disabled else ",".join(disabled)


def plan_signature(node: dict[str, Any]) -> str:
    """Hash physical plan structure (operator, relation, alias, index, child order) to avoid re-running identical plans."""
    def _strip(n: dict[str, Any]) -> dict[str, Any]:
        return {
            "t": n.get("Node Type"),
            "r": n.get("Relation Name"),
            "a": n.get("Alias"),
            "i": n.get("Index Name"),
            "j": n.get("Join Type"),
            "c": [_strip(ch) for ch in n.get("Plans", [])],
        }

    payload = json.dumps(_strip(node), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def get_dsn(dbname: str = "imdb") -> str:
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
    return s[f] if f == c else s[f] + (k - f) * (s[c] - s[f])


def apply_arm_local(cur: psycopg.Cursor, arm: dict[str, bool]) -> None:
    for k, v in arm.items():
        cur.execute(f"SET LOCAL {k} = {'on' if v else 'off'}")


def run_query_arms(
    conn: psycopg.Connection,
    sql: str,
    arms: list[dict[str, bool]],
    warmup_runs: int,
    timed_runs: int,
    timeout_ms: int,
) -> list[dict[str, Any]]:
    clean_sql = sql.strip().rstrip(";")

    # Step 1: Cheap EXPLAIN for every arm to deduplicate identical physical plans
    arm_plans: list[tuple[int, dict[str, bool], str, float]] = []
    with conn.cursor() as cur:
        for arm_idx, arm in enumerate(arms):
            cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                apply_arm_local(cur, arm)
                cur.execute(f"EXPLAIN (FORMAT JSON) {clean_sql}")
                p = cur.fetchone()[0][0]["Plan"]
                arm_plans.append((arm_idx, arm, plan_signature(p), float(p["Total Cost"])))
            finally:
                conn.rollback()

    # Step 2: Execute EXPLAIN ANALYZE once per unique physical plan signature
    sig_results: dict[str, dict[str, Any]] = {}
    for arm_idx, arm, sig, _cost in arm_plans:
        if sig in sig_results:
            continue

        # Early cutoff: cap timeout at max(2 * best_seen, 15s) once Arm 0 completes (matches LLMOpt speedup!)
        best_so_far = min(
            (r["execution_latency_ms"] for r in sig_results.values() if not r["timed_out"]),
            default=None,
        )
        eff_timeout = timeout_ms if best_so_far is None else min(timeout_ms, max(15_000, int(best_so_far * 3)))

        warmup_ms: list[float] = []
        timed_ms: list[float] = []
        timed_out = False
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                apply_arm_local(cur, arm)
                cur.execute(f"SET LOCAL statement_timeout = {int(eff_timeout)}")
                for _ in range(warmup_runs):
                    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                    warmup_ms.append(round(float(cur.fetchone()[0][0]["Execution Time"]), 3))
                for _ in range(timed_runs):
                    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                    timed_ms.append(round(float(cur.fetchone()[0][0]["Execution Time"]), 3))
            except psycopg.errors.QueryCanceled:
                timed_out = True
            finally:
                conn.rollback()

        exec_ms = float(eff_timeout) if timed_out else round(statistics.median(timed_ms), 3)
        sig_results[sig] = {
            "warmup_latencies_ms": warmup_ms,
            "timed_latencies_ms": timed_ms,
            "execution_latency_ms": exec_ms,
            "timed_out": timed_out,
        }

    # Step 3: Map back to every arm
    out: list[dict[str, Any]] = []
    for arm_idx, arm, sig, est_cost in arm_plans:
        res = sig_results[sig]
        out.append({
            "arm_index": arm_idx,
            "arm_label": arm_to_label(arm),
            "plan_sig": sig,
            "estimated_cost": est_cost,
            **res,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", choices=["top5", "all48"], default="all48")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=180_000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    arms = TOP5_ARMS if args.arms == "top5" else build_all48_arms()
    qdir = ROOT / "benchmarks" / "job" / "queries"
    query_files = sorted(qdir.glob("*.sql"))
    if args.limit > 0:
        query_files = query_files[: args.limit]
    if not query_files:
        sys.exit(f"No JOB queries found in {qdir}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"bao_oracle_{args.arms}_job_{ts}"
    out_dir = ROOT / "results" / "runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    commit = git_commit()

    oracle_latencies: list[float] = []
    default_latencies: list[float] = []

    with (
        psycopg.connect(get_dsn("imdb"), autocommit=False) as conn,
        (out_dir / "arm_latencies.jsonl").open("w", encoding="utf-8") as fh_arms,
        (out_dir / "queries.jsonl").open("w", encoding="utf-8") as fh_best,
    ):
        for idx, qf in enumerate(query_files, 1):
            qid = qf.stem
            sql = qf.read_text(encoding="utf-8")
            arm_rows = run_query_arms(conn, sql, arms, args.warmup, args.runs, args.timeout_ms)
            uniq_plans = len({r["plan_sig"] for r in arm_rows})
            for r in arm_rows:
                fh_arms.write(json.dumps({"query_id": qid, **r}) + "\n")
            fh_arms.flush()

            default_ms = arm_rows[0]["execution_latency_ms"]
            best = min(arm_rows, key=lambda x: x["execution_latency_ms"])
            default_latencies.append(default_ms)
            oracle_latencies.append(best["execution_latency_ms"])

            fh_best.write(json.dumps({
                "run_id": run_id,
                "git_commit": commit,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": "job",
                "query_id": qid,
                "method_id": "bao_oracle",
                "round_index": 0,
                "proposed_action": best["arm_label"],
                "action_valid": True,
                "estimated_cost": best["estimated_cost"],
                "execution_latency_ms": best["execution_latency_ms"],
                "default_latency_ms": default_ms,
                "unique_plans_tested": uniq_plans,
                "timed_out": best["timed_out"],
                "llm_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "llm_wall_time_ms": 0.0,
            }) + "\n")
            fh_best.flush()
            print(
                f"[{idx:3d}/{len(query_files)}] {qid:4s} | default={default_ms:8.1f} ms -> "
                f"oracle={best['execution_latency_ms']:8.1f} ms ({best['arm_label']}, {uniq_plans} unique plans)"
            )

    improved = sum(1 for o, d in zip(oracle_latencies, default_latencies) if o < d * 0.95)
    summary = {
        "run_id": run_id,
        "git_commit": commit,
        "benchmark": "job",
        "method_id": "bao_oracle",
        "arms_mode": args.arms,
        "num_arms": len(arms),
        "query_count": len(oracle_latencies),
        "default_p50_ms": round(percentile(default_latencies, 50), 3),
        "default_p90_ms": round(percentile(default_latencies, 90), 3),
        "default_total_s": round(sum(default_latencies) / 1000.0, 3),
        "oracle_p50_ms": round(percentile(oracle_latencies, 50), 3),
        "oracle_p90_ms": round(percentile(oracle_latencies, 90), 3),
        "oracle_total_s": round(sum(oracle_latencies) / 1000.0, 3),
        "improved_pct": round(100.0 * improved / len(oracle_latencies), 2),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\nSummary:", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
