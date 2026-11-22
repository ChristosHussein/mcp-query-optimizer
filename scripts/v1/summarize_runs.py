"""Reconstruct thesis evaluation metrics directly from raw per-query JSONL logs (scientific-integrity compliant).

Reads one or more results/runs/<run_id>/queries.jsonl files and computes:
  - P50 / P90 / Total latency (using regression-guarded best valid plan up to round K, plus raw per-round convergence)
  - % Improved (>5% faster than baseline) and % Regressed (>5% slower than baseline)
  - Mean LLM calls, prompt tokens, completion tokens, thought tokens, and LLM wall time per query
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (k - f) * (s[c] - s[f])


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def summarize_run(jsonl_path: Path, max_k: int | None = None) -> dict[str, Any]:
    rows = load_jsonl(jsonl_path)
    if not rows:
        raise ValueError(f"Empty run log: {jsonl_path}")

    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["query_id"]].append(r)

    method_id = rows[0]["method_id"]
    benchmark = rows[0]["benchmark"]
    run_id = rows[0]["run_id"]

    base_lat: list[float] = []
    best_lat: list[float] = []
    llm_calls: list[int] = []
    total_tokens: list[int] = []
    thought_tokens: list[int] = []
    llm_wall_ms: list[float] = []
    improved = 0
    regressed = 0
    timeouts = 0
    escalated_queries = 0

    for qid in sorted(by_q.keys()):
        qrows = sorted(by_q[qid], key=lambda x: int(x.get("round_index", 0)))
        if max_k is not None:
            qrows = [r for r in qrows if int(r.get("round_index", 0)) <= max_k]

        # Baseline latency: round 0 (for advisor) or pg_default_latency_ms / default_latency_ms / self
        r0 = qrows[0]
        b_ms = r0.get("pg_default_latency_ms") or r0.get("default_latency_ms") or r0.get("execution_latency_ms")
        if b_ms is None:
            continue
        b_ms = float(b_ms)
        base_lat.append(b_ms)

        valid_latencies = [
            float(r["execution_latency_ms"])
            for r in qrows
            if r.get("action_valid", True) and r.get("execution_latency_ms") is not None and not r.get("timed_out")
        ]
        if valid_latencies:
            final_ms = min(valid_latencies)
        else:
            final_ms = b_ms
            if any(r.get("timed_out") for r in qrows):
                timeouts += 1

        best_lat.append(final_ms)
        if final_ms < b_ms * 0.95:
            improved += 1
        elif final_ms > b_ms * 1.05:
            regressed += 1

        last = qrows[-1]
        llm_calls.append(int(last.get("llm_calls", 0) or 0))
        in_t = int(last.get("prompt_tokens", 0) or 0)
        out_t = int(last.get("completion_tokens", 0) or 0)
        th_t = int(last.get("thought_tokens", 0) or 0)
        total_tokens.append(in_t + out_t)
        thought_tokens.append(th_t)
        llm_wall_ms.append(float(last.get("llm_wall_time_ms", 0.0) or 0.0))

        advisor_m = last.get("model_advisor")
        if advisor_m and any(r.get("model_used_this_round") == advisor_m for r in qrows):
            escalated_queries += 1

    n = len(best_lat)
    return {
        "run_id": run_id,
        "benchmark": benchmark,
        "method_id": method_id,
        "max_k": max_k,
        "n_queries": n,
        "base_p50_ms": round(percentile(base_lat, 50), 2),
        "base_p90_ms": round(percentile(base_lat, 90), 2),
        "base_total_s": round(sum(base_lat) / 1000.0, 3),
        "p50_ms": round(percentile(best_lat, 50), 2),
        "p90_ms": round(percentile(best_lat, 90), 2),
        "total_s": round(sum(best_lat) / 1000.0, 3),
        "speedup_total_pct": round((sum(base_lat) - sum(best_lat)) / sum(base_lat) * 100.0, 2) if sum(base_lat) > 0 else 0.0,
        "improved_pct": round(100.0 * improved / n, 2) if n else 0.0,
        "regression_pct": round(100.0 * regressed / n, 2) if n else 0.0,
        "timeouts": timeouts,
        "mean_llm_calls": round(sum(llm_calls) / n, 2) if n else 0.0,
        "mean_total_tokens": round(sum(total_tokens) / n, 1) if n else 0.0,
        "mean_thought_tokens": round(sum(thought_tokens) / n, 1) if n else 0.0,
        "mean_llm_wall_s": round((sum(llm_wall_ms) / n) / 1000.0, 2) if n else 0.0,
        "escalated_queries": escalated_queries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl_files", nargs="+", help="Paths to queries.jsonl files")
    parser.add_argument("--by-round", action="store_true", help="Also show K=1..5 convergence breakdown")
    args = parser.parse_args()

    for p_str in args.jsonl_files:
        p = Path(p_str)
        print(json.dumps(summarize_run(p), indent=2))
        if args.by_round:
            print("  --- Convergence by max round K ---")
            for k in range(1, 6):
                s = summarize_run(p, max_k=k)
                print(
                    f"  K={k}: total={s['total_s']:.2f}s (gain={s['speedup_total_pct']:+.1f}%) "
                    f"| P50={s['p50_ms']:.1f}ms | P90={s['p90_ms']:.1f}ms "
                    f"| calls={s['mean_llm_calls']:.1f} | tok={s['mean_total_tokens']:.0f}"
                )


if __name__ == "__main__":
    main()
