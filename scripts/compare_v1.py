"""Reconstruct per-query master comparison tables (CSV + Markdown) from frozen v1 JSONL logs."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "results" / "runs"
OUT = ROOT / "results" / "tables"


def load_best_ms(run_id: str) -> dict[str, tuple[float, float]]:
    """Return {query_id: (base_ms, best_valid_ms)} reconstructed from queries.jsonl."""
    by_q: dict[str, list[dict]] = defaultdict(list)
    with (RUNS / run_id / "queries.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                by_q[r["query_id"]].append(r)

    out: dict[str, tuple[float, float]] = {}
    for qid, rows in by_q.items():
        r0 = next((r for r in rows if r["round_index"] == 0), rows[0])
        base = float(r0["execution_latency_ms"])
        best = base
        for r in rows:
            if r["round_index"] > 0 and r.get("action_valid") and r.get("execution_latency_ms") is not None:
                best = min(best, float(r["execution_latency_ms"]))
        out[qid] = (base, best)
    return out


def build_table(
    name: str,
    columns: list[tuple[str, str]],
    sort_key=None,
) -> tuple[list[str], list[list[str]]]:
    data = {label: load_best_ms(run_id) for label, run_id in columns}
    qids = sorted(data[columns[0][0]].keys(), key=sort_key)

    headers = ["Query"] + [f"{lbl} (ms)" for lbl, _ in columns] + ["Best MCP (ms)", "Best Speedup vs pg_default"]
    rows_out: list[list[str]] = []
    mcp_labels = [lbl for lbl, _ in columns if lbl.startswith("mcp_")]

    for qid in qids:
        pg_ms = data[columns[0][0]][qid][1]
        vals = [data[lbl][qid][1] for lbl, _ in columns]
        best_mcp = min(data[lbl][qid][1] for lbl in mcp_labels)
        speedup = pg_ms / best_mcp if best_mcp > 0 else 1.0
        rows_out.append(
            [qid]
            + [f"{v:.2f}" for v in vals]
            + [f"{best_mcp:.2f}", f"{speedup:.2f}x"]
        )

    # Totals row
    totals = [sum(data[lbl][q][1] for q in qids) / 1000.0 for lbl, _ in columns]
    best_mcp_tot = sum(min(data[lbl][q][1] for lbl in mcp_labels) for q in qids) / 1000.0
    rows_out.append(
        ["**TOTAL (s)**"]
        + [f"{t:.2f}s" for t in totals]
        + [f"{best_mcp_tot:.2f}s", f"{totals[0] / best_mcp_tot:.2f}x"]
    )

    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / f"{name}_v1_comparison.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows_out:
            w.writerow([c.replace("**", "") for c in r])

    md_path = OUT / f"{name}_v1_comparison.md"
    lines = [
        f"# {name.upper()} v1 Per-Query Master Comparison\n",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] + ["---:"] * (len(headers) - 1)) + " |",
    ]
    for r in rows_out:
        lines.append("| " + " | ".join(r) + " |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {csv_path.relative_to(ROOT)} and {md_path.relative_to(ROOT)} ({len(qids)} queries)")
    return headers, rows_out


def main() -> None:
    job_cols = [
        ("pg_default", "pg_default_job_v1"),
        ("llmsteer", "llmsteer_job_v1"),
        ("bao_oracle", "bao_oracle_all48_job_v1"),
        ("mcp_single_shot", "mcp_single_shot_job_v1"),
        ("mcp_iter_cheap", "mcp_iter_cheap_job_v1"),
        ("mcp_iter_hybrid", "mcp_iter_hybrid_job_v1"),
    ]
    tpch_cols = [
        ("pg_default", "pg_default_tpch_v1"),
        ("mcp_single_shot", "mcp_single_shot_tpch_v1"),
        ("mcp_iter_cheap", "mcp_iter_cheap_tpch_v1"),
        ("mcp_iter_hybrid", "mcp_iter_hybrid_tpch_v1"),
    ]
    build_table("job", job_cols, sort_key=lambda q: (int("".join(c for c in q if c.isdigit())), q))
    build_table("tpch", tpch_cols)


if __name__ == "__main__":
    main()
