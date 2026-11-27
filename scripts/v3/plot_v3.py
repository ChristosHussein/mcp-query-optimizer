"""Generate Phase 3 (v3) convergence and baseline comparison figure from frozen raw logs."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parents[2]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return s[int(k)] if f == c else s[f] + (k - f) * (s[c] - s[f])


def curve(path: Path, max_k: int = 6) -> list[dict]:
    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in load_jsonl(path):
        by_q[r["query_id"]].append(r)
    out = []
    for k in range(max_k + 1):
        bests, bases, toks = [], [], []
        for rs in by_q.values():
            rs = sorted(rs, key=lambda x: x["round_index"])
            b = float(rs[0]["execution_latency_ms"])
            m = b
            for r in rs[1:]:
                if r["round_index"] <= k and r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
                    m = min(m, float(r["execution_latency_ms"]))
            bases.append(b)
            bests.append(m)
            last = rs[k] if k < len(rs) else rs[-1]
            toks.append((last.get("prompt_tokens") or 0) + (last.get("completion_tokens") or 0))
        out.append({
            "k": k,
            "total_s": sum(bests) / 1000.0,
            "p50": pct(bests, 50),
            "p90": pct(bests, 90),
            "mean_tok_k": (sum(toks) / len(toks)) / 1000.0,
        })
    return out


def main() -> None:
    job_c = curve(REPO / "results" / "v3" / "v3_lightning_job_full_k6" / "queries.jsonl")
    tpch_c = curve(REPO / "results" / "v3" / "v3_lightning_tpch_full_k6" / "queries.jsonl")

    # Frozen baselines for JOB (113q)
    bao_rows = load_jsonl(REPO / "results" / "baselines" / "bao_oracle_all48_job_v1" / "queries.jsonl")
    llm_rows = load_jsonl(REPO / "results" / "baselines" / "llmsteer_job_v1" / "queries.jsonl")
    bao_p90 = pct([float(r["execution_latency_ms"]) for r in bao_rows], 90)
    llm_p90 = pct([float(r["execution_latency_ms"]) for r in llm_rows], 90)

    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.25})
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.2), dpi=180)

    # Panel A: JOB 113q P90 & Total Latency Convergence vs Baselines
    ax = axes[0]
    ks = [c["k"] for c in job_c]
    p90s = [c["p90"] for c in job_c]
    ax.plot(ks, p90s, marker="o", linewidth=2.4, color="#1f77b4", label="v3_lightning P90 (ms)")
    ax.axhline(bao_p90, color="#2ca02c", linestyle="--", linewidth=1.8, label=f"Bao Oracle (49 arms) P90 ({bao_p90:.0f} ms)")
    ax.axhline(llm_p90, color="#d62728", linestyle=":", linewidth=1.8, label=f"LLMSteer (10-fold CV) P90 ({llm_p90:.0f} ms)")
    for c in job_c:
        if c["k"] in (0, 1, 2, 6):
            xy = (6, 8) if c["k"] == 0 else ((0, 9) if c["k"] == 1 else ((0, -26) if c["k"] == 2 else (-6, -26)))
            ha = "left" if c["k"] == 0 else ("right" if c["k"] == 6 else "center")
            ax.annotate(f"{c['p90']:.0f} ms\n({c['total_s']:.1f}s)", (c["k"], c["p90"]),
                        textcoords="offset points", xytext=xy,
                        ha=ha, fontsize=8.5, fontweight="bold" if c["k"] == 6 else "normal")
    ax.set_title("A. Full JOB (113q): P90 Latency Convergence vs. Baselines", fontsize=10.5, fontweight="bold")
    ax.set_xlabel("Advisory Round (K)  [K=1 Single-Shot → K*=6 Iterative]")
    ax.set_ylabel("P90 Execution Latency (ms)")
    ax.set_xticks(ks)
    ax.set_ylim(1050, 2750)
    ax.legend(loc="upper right", fontsize=8.5)

    # Panel B: TPC-H SF=10 Full (22q) Total Latency & P50 Convergence
    ax2 = axes[1]
    tots = [c["total_s"] for c in tpch_c]
    ax2.plot(ks, tots, marker="s", linewidth=2.4, color="#ff7f0e", label="v3_lightning Total Latency (s)")
    for c in tpch_c:
        if c["k"] in (0, 1, 2, 6):
            xy = (8, 6) if c["k"] == 0 else ((10, 8) if c["k"] == 1 else ((4, 12) if c["k"] == 2 else (-6, 12)))
            ha = "right" if c["k"] == 6 else "left"
            ax2.annotate(f"{c['total_s']:.1f} s\n(P50 {c['p50']:.0f} ms)", (c["k"], c["total_s"]),
                         textcoords="offset points", xytext=xy,
                         ha=ha, fontsize=8.5,
                         fontweight="bold" if c["k"] == 6 else "normal")
    ax2.set_title("B. Full TPC-H SF=10 (22q): 2-Phase Index Convergence (2→0 Timeouts)", fontsize=10.5, fontweight="bold")
    ax2.set_xlabel("Advisory Round (K)  [K=1 Single-Shot → K*=6 Iterative]")
    ax2.set_ylabel("Total Workload Latency (s)")
    ax2.set_xticks(ks)
    ax2.set_ylim(0, 640)
    ax2.legend(loc="upper right", fontsize=8.5)

    fig.suptitle("Phase 3 (v3): Single-Shot (K=1) vs. Iterative (K*=6) with Working pg_stats & Hardened MCP", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out_path = REPO / "thesis" / "figures" / "v3_convergence_and_comparison.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
