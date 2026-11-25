"""Generate flagship before/after execution-plan case studies (thesis/case_studies.md + thesis/figures/flagship_case_studies.png).

Reconstructs round-by-round trajectories from frozen results/runs/*/queries.jsonl and, when thesis-pg16 is running,
captures live annotated EXPLAIN (ANALYZE, FORMAT JSON) operator trees (with [EST_ERR] markers) for Before vs. After comparison.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import psycopg
from dotenv import load_dotenv

from postgres_mcp.advisory import evaluate_real_indexes, explain_with_hints
from postgres_mcp.artifacts import ExplainPlanArtifact
from postgres_mcp.explain import ExplainPlanTool
from postgres_mcp.sql import DbConnPool, SqlDriver

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
FIG_OUT = ROOT / "thesis" / "figures" / "flagship_case_studies.png"
MD_OUT = ROOT / "thesis" / "case_studies.md"


def resolve_run_jsonl(run_id: str) -> Path:
    for tier in ("baselines", "v1", "v2", "v3"):
        cand = RESULTS / tier / run_id / "queries.jsonl"
        if cand.exists():
            return cand
    raise FileNotFoundError(f"Run not found across results/{{baselines,v1,v2,v3}}: {run_id}")


def load_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def best_curve(run_id: str, qid: str, max_k: int, timeout_ms: float = 180000.0) -> tuple[list[int], list[float], dict[str, Any]]:
    rows = [r for r in load_jsonl(resolve_run_jsonl(run_id)) if r["query_id"] == qid]
    rows.sort(key=lambda x: int(x.get("round_index", 0)))
    r0 = rows[0]
    b = timeout_ms if r0.get("timed_out") or r0.get("execution_latency_ms") is None else float(r0["execution_latency_ms"])
    ks = [0]
    best_so_far = [b / 1000.0]
    best_rec = r0
    cur = b
    by_r = {int(r["round_index"]): r for r in rows}
    for k in range(1, max_k + 1):
        r = by_r.get(k)
        if r and r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
            ms = float(r["execution_latency_ms"])
            if ms < cur:
                cur = ms
                best_rec = r
        ks.append(k)
        best_so_far.append(cur / 1000.0)
    return ks, best_so_far, best_rec


def build_figure(metrics_out: dict[str, Any]) -> None:
    plt.rcParams.update({"font.size": 9.5, "axes.titlesize": 10.5, "axes.labelsize": 9.5})
    fig, axes = plt.subplots(2, 3, figsize=(15.5, 8.8), dpi=220)
    c120, c550, chyb, cbao = "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"

    # Panel A: JOB 31c (K=0..20)
    ax = axes[0, 0]
    bao_31c = next(float(r["execution_latency_ms"]) for r in load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1")) if r["query_id"] == "31c") / 1000.0
    for rid, lbl, col in [("v2_120b_job_top12", "v2_120b", c120), ("v2_550b_job_top12", "v2_550b", c550), ("v2_hybrid_job_top12", "v2_hybrid", chyb)]:
        ks, ys, _ = best_curve(rid, "31c", 20)
        ax.plot(ks, ys, marker="o", markersize=3, linewidth=1.8, label=lbl, color=col)
    ax.axhline(bao_31c, linestyle="--", color=cbao, linewidth=1.4, label=f"Bao Oracle ({bao_31c:.2f}s)")
    ax.set_title("A. JOB 31c — 23.36× Join-Order Rescue")
    ax.set_xlabel("Advisory Round (K)")
    ax.set_ylabel("Best Verified Latency (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)

    # Panel B: JOB 19d (Alternating Hybrid 120B <-> 550B Synergy)
    ax = axes[0, 1]
    bao_19d = next(float(r["execution_latency_ms"]) for r in load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1")) if r["query_id"] == "19d") / 1000.0
    for rid, lbl, col in [("v2_120b_job_top12", "v2_120b", c120), ("v2_550b_job_top12", "v2_550b", c550), ("v2_hybrid_job_top12", "v2_hybrid (120B↔550B)", chyb)]:
        ks, ys, _ = best_curve(rid, "19d", 20)
        ax.plot(ks, ys, marker="o", markersize=3, linewidth=1.8, label=lbl, color=col)
    ax.axhline(bao_19d, linestyle="--", color=cbao, linewidth=1.4, label=f"Bao Oracle ({bao_19d:.2f}s)")
    ax.set_title("B. JOB 19d — 120B ↔ 550B Shared-History Synergy")
    ax.set_xlabel("Advisory Round (K)")
    ax.set_ylabel("Best Verified Latency (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)

    # Panel C: TPC-H q09 (K=0..6 vs Classical DTA)
    ax = axes[0, 2]
    for rid, lbl, col in [("v2_120b_tpch_top8_k6", "v2_120b", c120), ("v2_550b_tpch_top8_k6", "v2_550b", c550), ("v2_hybrid_tpch_top8_k6", "v2_hybrid", chyb)]:
        ks, ys, _ = best_curve(rid, "q09", 6)
        ax.plot(ks, ys, marker="s", markersize=4, linewidth=1.8, label=lbl, color=col)
    dta_path = resolve_run_jsonl("dta_tpch_top8_v2")
    if dta_path.exists():
        _, dta_ys, _ = best_curve("dta_tpch_top8_v2", "q09", 1)
        ax.axhline(dta_ys[-1], linestyle=":", color="#7f7f7f", linewidth=1.8, label=f"Greedy DTA ({dta_ys[-1]:.2f}s, 0 idx)")
    ax.set_title("C. TPC-H q09 — Multi-Table Composite Indexing")
    ax.set_xlabel("Advisory Round (K)")
    ax.set_ylabel("Best Verified Latency (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)

    # Panel D: Official Frozen HypoPG Cost vs Phase-2 Real Latency Traps (q09 R1 & DTA q03)
    ax = axes[1, 0]
    import numpy as np
    q09_rows = [r for r in load_jsonl(resolve_run_jsonl("v2_120b_tpch_top8_k6")) if r["query_id"] == "q09"]
    q09_c = (float(q09_rows[1]["estimated_cost"]) - float(q09_rows[0]["estimated_cost"])) / float(q09_rows[0]["estimated_cost"]) * 100.0
    q09_m = (float(q09_rows[1]["execution_latency_ms"]) - float(q09_rows[0]["execution_latency_ms"])) / float(q09_rows[0]["execution_latency_ms"]) * 100.0
    q03_rows = [r for r in load_jsonl(resolve_run_jsonl("dta_tpch_top8_v2")) if r["query_id"] == "q03"]
    q03_c = (float(q03_rows[1]["estimated_cost"]) - float(q03_rows[0]["estimated_cost"])) / float(q03_rows[0]["estimated_cost"]) * 100.0
    q03_m = (float(q03_rows[1]["execution_latency_ms"]) - float(q03_rows[0]["execution_latency_ms"])) / float(q03_rows[0]["execution_latency_ms"]) * 100.0
    x = np.arange(2)
    w = 0.34
    b1 = ax.bar(x - w / 2, [q09_c, q03_c], width=w, color="#17becf", label="Phase-1 HypoPG Est. Cost")
    b2 = ax.bar(x + w / 2, [q09_m, q03_m], width=w, color=cbao, label="Phase-2 Actual Latency")
    ax.axhline(0, color="black", linewidth=0.9)
    for b in list(b1) + list(b2):
        v = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2, v + (12 if v >= 0 else -28), f"{v:+.1f}%", ha="center", fontweight="bold", fontsize=8.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["TPC-H q09 (v2_120b R1)\nCaught → Fixed in R3", "TPC-H q03 (Greedy DTA)\nCaught by Phase-2 Gate"])
    ax.set_ylim(-125, 410)
    ax.set_title("D. Deceptive HypoPG Cost Drops vs. Real Regressions")
    ax.set_ylabel("Change vs. PG Default (%)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")

    # Panel E: Leak-Free Anonymized IMDb (imdb_anon 17c & 31a, K=0..6)
    ax = axes[1, 1]
    for rid, qid, lbl, col, ls in [
        ("v2_hybrid_imdb_anon_k6", "17c", "17c (v2_hybrid, 18.1×)", chyb, "-"),
        ("v2_550b_imdb_anon_k6", "31a", "31a (v2_550b, 16.9×)", c550, "--"),
        ("v2_120b_imdb_anon_k6", "17c", "17c (v2_120b, 18.8×)", c120, ":"),
    ]:
        ks, ys, _ = best_curve(rid, qid, 6)
        ax.plot(ks, ys, marker="o", markersize=4, linewidth=1.8, linestyle=ls, label=lbl, color=col)
    ax.set_title("E. imdb_anon — Blind Optimization (0% IMDb Names)")
    ax.set_xlabel("Advisory Round (K)")
    ax.set_ylabel("Best Verified Latency (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)

    # Panel F: Real-World STATS-CEB (s070 5.69x Win vs s058 60s M:N Ceiling)
    ax = axes[1, 2]
    ks_70, ys_70, _ = best_curve("v2_120b_stats_ceb_k6", "s070", 6, 60000.0)
    ks_143, ys_143, _ = best_curve("v2_hybrid_stats_ceb_k6", "s143", 6, 60000.0)
    ks_58, ys_58, _ = best_curve("v2_550b_stats_ceb_k6", "s058", 6, 60000.0)
    ax.plot(ks_58, ys_58, marker="x", markersize=5, linewidth=1.8, color=cbao, linestyle="--", label="s058 (M:N COUNT(*) ceiling)")
    ax.plot(ks_143, ys_143, marker="s", markersize=4, linewidth=1.8, color=chyb, label="s143 (v2_hybrid, 3.19×)")
    ax.plot(ks_70, ys_70, marker="o", markersize=4, linewidth=1.8, color=c120, label="s070 (v2_120b, 5.69×)")
    ax.set_title("F. STATS-CEB — Join-Order Wins vs. M:N Fan-Out Wall")
    ax.set_xlabel("Advisory Round (K)")
    ax.set_ylabel("Best Verified Latency (s)")
    ax.set_ylim(0, 66)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="center right")

    fig.suptitle("MCP-Driven Query Optimization — Flagship Round-by-Round Trajectories & Safety Gates", fontsize=12.5, fontweight="bold", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.965])
    FIG_OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_OUT)
    plt.close(fig)
    print(f"Saved figure -> {FIG_OUT}")


def explain_sync(dsn: str, sql: str, analyze: bool = True, timeout_ms: int = 180000) -> tuple[str, float | None, float | None]:
    clean_sql = sql.strip().rstrip(";")
    opts = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            try:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                cur.execute(f"EXPLAIN ({opts}) {clean_sql}")
                plan_json = cur.fetchone()[0][0]
            finally:
                conn.rollback()
    art = ExplainPlanArtifact.from_json_data(plan_json)
    return art.to_text(), plan_json["Plan"].get("Total Cost"), art.execution_time


async def collect_live_plans() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")

    imdb_dsn = f"postgresql://postgres:{pw}@{host}:{port}/imdb"
    anon_dsn = f"postgresql://postgres:{pw}@{host}:{port}/imdb_anon"
    stats_dsn = f"postgresql://postgres:{pw}@{host}:{port}/stats_ceb"
    tpch_dsn = f"postgresql://postgres:{pw}@{host}:{port}/tpch"
    eval_dsn = f"postgresql://postgres:{pw}@{host}:{port}/tpch_eval"

    imdb_pool = DbConnPool(imdb_dsn)
    anon_pool = DbConnPool(anon_dsn)
    stats_pool = DbConnPool(stats_dsn)
    tpch_pool = DbConnPool(tpch_dsn)
    eval_pool = DbConnPool(eval_dsn)
    for p in (imdb_pool, anon_pool, stats_pool, tpch_pool, eval_pool):
        await p.pool_connect()

    data: dict[str, Any] = {}
    try:
        # 1. JOB 31c (v2_hybrid K=20 best)
        sql_31c = (ROOT / "benchmarks" / "job" / "queries" / "31c.sql").read_text(encoding="utf-8")
        _, _, best_31c = best_curve("v2_hybrid_job_top12", "31c", 20)
        explain_sync(imdb_dsn, sql_31c, analyze=True)  # warmup
        base_txt_31c, _, base_ms_31c = explain_sync(imdb_dsn, sql_31c, analyze=True)
        await explain_with_hints(imdb_pool, sql_31c, best_31c["proposed_action"], analyze=True)
        after_31c = await explain_with_hints(imdb_pool, sql_31c, best_31c["proposed_action"], analyze=True)
        data["job_31c"] = {
            "base_plan": base_txt_31c,
            "after_plan": after_31c["plan_text"],
            "hints": best_31c["proposed_action"],
            "logged_base_ms": round(float(load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1"))[[r["query_id"] for r in load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1"))].index("31c")]["default_latency_ms"]), 1),
            "logged_bao_ms": round(float(load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1"))[[r["query_id"] for r in load_jsonl(resolve_run_jsonl("bao_oracle_all48_job_v1"))].index("31c")]["execution_latency_ms"]), 1),
            "logged_best_ms": round(float(best_31c["execution_latency_ms"]), 1),
            "rationale": best_31c.get("rationale"),
        }

        # 2. JOB 19d (v2_hybrid K=20 synergy)
        sql_19d = (ROOT / "benchmarks" / "job" / "queries" / "19d.sql").read_text(encoding="utf-8")
        _, _, best_19d = best_curve("v2_hybrid_job_top12", "19d", 20)
        explain_sync(imdb_dsn, sql_19d, analyze=True)
        base_txt_19d, _, _ = explain_sync(imdb_dsn, sql_19d, analyze=True)
        await explain_with_hints(imdb_pool, sql_19d, best_19d["proposed_action"], analyze=True)
        after_19d = await explain_with_hints(imdb_pool, sql_19d, best_19d["proposed_action"], analyze=True)
        r0_19d = next(r for r in load_jsonl(resolve_run_jsonl("v2_hybrid_job_top12")) if r["query_id"] == "19d" and r["round_index"] == 0)
        r1_19d = next(r for r in load_jsonl(resolve_run_jsonl("v2_hybrid_job_top12")) if r["query_id"] == "19d" and r["round_index"] == 1)
        data["job_19d"] = {
            "base_plan": base_txt_19d,
            "after_plan": after_19d["plan_text"],
            "r1_120b_hints": r1_19d["proposed_action"],
            "r1_120b_ms": round(float(r1_19d["execution_latency_ms"]), 1),
            "best_round": int(best_19d["round_index"]),
            "best_550b_hints": best_19d["proposed_action"],
            "logged_base_ms": round(float(r0_19d["execution_latency_ms"]), 1),
            "logged_best_ms": round(float(best_19d["execution_latency_ms"]), 1),
            "rationale": best_19d.get("rationale"),
        }

        # 3. TPC-H q17 (180s timeout -> ~16 ms)
        sql_q17 = (ROOT / "benchmarks" / "tpch" / "queries" / "q17.sql").read_text(encoding="utf-8")
        _, _, best_q17 = best_curve("v2_hybrid_tpch_top8_k6", "q17", 6)
        base_est_q17, base_cost_q17, _ = explain_sync(tpch_dsn, sql_q17, analyze=False)
        p2_q17 = await evaluate_real_indexes(eval_pool, sql_q17, best_q17["proposed_action"], warmup_runs=1, timed_runs=1)
        data["tpch_q17"] = {
            "base_est_plan": base_est_q17,
            "after_plan": p2_q17["plan_text"],
            "indexes": best_q17["proposed_action"],
            "logged_base_ms": 180000.0,
            "logged_best_ms": round(float(best_q17["execution_latency_ms"]), 1),
            "build_ms": p2_q17["index_build_times_ms"],
        }

        # 4. TPC-H q03 (dta_tpch_top8_v2) Phase-1 HypoPG Cost Drop vs Phase-2 Real Latency Regression Trap
        sql_q03 = (ROOT / "benchmarks" / "tpch" / "queries" / "q03.sql").read_text(encoding="utf-8")
        q03_rows = [r for r in load_jsonl(resolve_run_jsonl("dta_tpch_top8_v2")) if r["query_id"] == "q03"]
        trap_idx = q03_rows[1]["proposed_action"]
        explain_sync(tpch_dsn, sql_q03, analyze=True)
        q03_base_txt, _, _ = explain_sync(tpch_dsn, sql_q03, analyze=True)
        p2_q03 = await evaluate_real_indexes(eval_pool, sql_q03, trap_idx, warmup_runs=1, timed_runs=1)
        q03_bc, q03_hc = float(q03_rows[0]["estimated_cost"]), float(q03_rows[1]["estimated_cost"])
        q03_bm, q03_am = float(q03_rows[0]["execution_latency_ms"]), float(q03_rows[1]["execution_latency_ms"])
        data["tpch_q06_trap"] = {
            "indexes": trap_idx,
            "base_cost": round(q03_bc, 1),
            "hypo_cost": round(q03_hc, 1),
            "hypopg_cost_change_pct": round((q03_hc - q03_bc) / q03_bc * 100.0, 1),
            "base_ms": round(q03_bm, 1),
            "after_ms": round(q03_am, 1),
            "real_latency_change_pct": round((q03_am - q03_bm) / q03_bm * 100.0, 1),
            "base_plan": q03_base_txt,
            "after_plan": p2_q03["plan_text"],
        }

        # 5. imdb_anon 17c (Blind Optimization)
        sql_anon_17c = (ROOT / "benchmarks" / "imdb_anon" / "queries" / "17c.sql").read_text(encoding="utf-8")
        _, _, best_anon_17c = best_curve("v2_hybrid_imdb_anon_k6", "17c", 6)
        r0_anon_17c = next(r for r in load_jsonl(resolve_run_jsonl("v2_hybrid_imdb_anon_k6")) if r["query_id"] == "17c" and r["round_index"] == 0)
        explain_sync(anon_dsn, sql_anon_17c, analyze=True)
        anon_base_txt, _, _ = explain_sync(anon_dsn, sql_anon_17c, analyze=True)
        await explain_with_hints(anon_pool, sql_anon_17c, best_anon_17c["proposed_action"], analyze=True)
        anon_after = await explain_with_hints(anon_pool, sql_anon_17c, best_anon_17c["proposed_action"], analyze=True)
        data["imdb_anon_17c"] = {
            "sql": sql_anon_17c.strip(),
            "hints": best_anon_17c["proposed_action"],
            "logged_base_ms": round(float(r0_anon_17c["execution_latency_ms"]), 1),
            "logged_best_ms": round(float(best_anon_17c["execution_latency_ms"]), 1),
            "speedup_x": round(float(r0_anon_17c["execution_latency_ms"]) / float(best_anon_17c["execution_latency_ms"]), 2),
            "base_plan": anon_base_txt,
            "after_plan": anon_after["plan_text"],
            "rationale": best_anon_17c.get("rationale"),
        }

        # 6. STATS-CEB s070 (5.69x Real-World Win)
        sql_s070 = (ROOT / "benchmarks" / "stats_ceb" / "queries" / "s070.sql").read_text(encoding="utf-8")
        _, _, best_s070 = best_curve("v2_120b_stats_ceb_k6", "s070", 6, 60000.0)
        r0_s070 = next(r for r in load_jsonl(resolve_run_jsonl("v2_120b_stats_ceb_k6")) if r["query_id"] == "s070" and r["round_index"] == 0)
        explain_sync(stats_dsn, sql_s070, analyze=True, timeout_ms=60000)
        s070_base_txt, _, _ = explain_sync(stats_dsn, sql_s070, analyze=True, timeout_ms=60000)
        await explain_with_hints(stats_pool, sql_s070, best_s070["proposed_action"], analyze=True, timeout_ms=60000)
        s070_after = await explain_with_hints(stats_pool, sql_s070, best_s070["proposed_action"], analyze=True, timeout_ms=60000)
        data["stats_s070"] = {
            "sql": sql_s070.strip(),
            "hints": best_s070["proposed_action"],
            "logged_base_ms": round(float(r0_s070["execution_latency_ms"]), 1),
            "logged_best_ms": round(float(best_s070["execution_latency_ms"]), 1),
            "speedup_x": round(float(r0_s070["execution_latency_ms"]) / float(best_s070["execution_latency_ms"]), 2),
            "base_plan": s070_base_txt,
            "after_plan": s070_after["plan_text"],
            "rationale": best_s070.get("rationale"),
        }
    finally:
        for p in (imdb_pool, anon_pool, stats_pool, tpch_pool, eval_pool):
            await p.close()
    return data


def write_markdown(d: dict[str, Any]) -> None:
    c31 = d["job_31c"]
    c19 = d["job_19d"]
    cq17 = d["tpch_q17"]
    cq06 = d["tpch_q06_trap"]
    canon = d["imdb_anon_17c"]
    cs70 = d["stats_s070"]

    md = f"""# Flagship Query Optimization Case Studies — Before vs. After Execution Plans

All latencies below come from the frozen `results/runs/*/queries.jsonl` logs, paired with live annotated PostgreSQL 16 `EXPLAIN (ANALYZE)` operator trees (`ExplainPlanArtifact` showing `AS` aliases and `[EST_ERR: Nx UNDER/OVER]` markers).

![Flagship Case Studies & Safety Gates](figures/flagship_case_studies.png)

---

## Case Study 1: `JOB 31c` — Breaking the 49-Arm `Bao Oracle` Ceiling (`23.36×` Speedup)
- **Logged Latencies:** PostgreSQL Default `{c31['logged_base_ms']} ms` | 49-Arm `Bao Oracle` `{c31['logged_bao_ms']} ms` | **`v2_hybrid` `{c31['logged_best_ms']} ms` (`23.36×` vs. PG, `-93.9%` vs. Bao)**
- **Agent Diagnosis:** {c31['rationale']}
- **Winning `pg_hint_plan` Block:** `{c31['hints']}`

### Default PostgreSQL 16 Execution Plan (Severe `[EST_ERR]` Nested-Loop Explosion)
```text
{c31['base_plan'].strip()}
```

### MCP-Steered Execution Plan (`{c31['logged_best_ms']} ms`)
```text
{c31['after_plan'].strip()}
```

---

## Case Study 2: `JOB 19d` — Collaborative `120B ↔ 550B` Alternating Synergy
- **Logged Latencies:** Round-0 Baseline `{c19['logged_base_ms']} ms` | Standalone `v2_550b` `6,437.8 ms` (`1.00×`) | Standalone `v2_120b` `3,424.8 ms` | **`v2_hybrid` Round 1 (`120B`) `{c19['r1_120b_ms']} ms` → Round `{c19['best_round']}` (`550B`) `{c19['logged_best_ms']} ms`** (beating `Bao Oracle`'s `2,421.0 ms`)
- **Round 1 (`120B` Worker) Foundation:** `{c19['r1_120b_hints']}`
- **Round `{c19['best_round']}` (`550B` Frontier) Refinement:** `{c19['best_550b_hints']}`
- **Frontier Diagnosis:** {c19['rationale']}

### Default PostgreSQL 16 Execution Plan
```text
{c19['base_plan'].strip()}
```

### Alternating Hybrid Refined Execution Plan (`{c19['logged_best_ms']} ms`)
```text
{c19['after_plan'].strip()}
```

---

## Case Study 3: `TPC-H SF=10 q17` — Timeout Rescue via 2-Phase Indexing (`180,000.0 ms → {cq17['logged_best_ms']} ms`)
- **Logged Latencies:** PostgreSQL Default `180,000.0 ms` (Timed Out) | **`v2_hybrid` `{cq17['logged_best_ms']} ms`** (Phase-2 index build `{cq17['build_ms']} ms` on `tpch_eval`)
- **Winning Composite Index Spec:** `{json.dumps(cq17['indexes'])}`

### Default PostgreSQL 16 Estimated Plan (Correlated `Seq Scan` on 60M-Row `lineitem`)
```text
{cq17['base_est_plan'].strip()}
```

### Phase-2 Verified Execution Plan on `tpch_eval` (`{cq17['logged_best_ms']} ms`)
```text
{cq17['after_plan'].strip()}
```

---

## Case Study 4: `TPC-H SF=10 q03` & `q09` — Why Phase-2 Real Materialization Is Mandatory
- **Frozen Official Evidence (`dta_tpch_top8_v2` `q03`):** Classical `DTA` proposed `{json.dumps(cq06['indexes'])}` because `HypoPG` estimated a cost reduction from `{cq06['base_cost']}` → `{cq06['hypo_cost']}` (**`{cq06['hypopg_cost_change_pct']:+.1f}%`**). Yet Phase-2 timing on `tpch_eval` revealed a **`{cq06['real_latency_change_pct']:+.1f}%` real latency regression** (`{cq06['base_ms']} ms` → `{cq06['after_ms']} ms`, caught and blocked by our Phase-2 gate). Similarly, on `q09` Round 1 (`v2_120b_tpch_top8_k6`), `HypoPG` predicted `-80.5%` cost while real latency regressed `+332.4%` (`15,631.6 ms → 67,588.6 ms`), prompting the agent to pivot in Round 3 to a verified `6,863.7 ms` (`2.28×`) composite index set.
- **Root Cause:** Replacing a fast Parallel `Seq Scan` + `Hash Join` with scattered B-tree heap fetches across millions of `orders` rows loses to sequential scan bandwidth despite lower planner cost units.

### Default Parallel Plan on `TPC-H q03` (`{cq06['base_ms']} ms`)
```text
{cq06['base_plan'].strip()}
```

### Regressing Indexed Plan Caught in Phase 2 (`{cq06['after_ms']} ms`, Rejected by Regression Gate)
```text
{cq06['after_plan'].strip()}
```

---

## Case Study 5: `imdb_anon 17c` — Blind Optimization With Zero IMDb Identifiers (`{canon['speedup_x']}×`)
- **Logged Latencies:** Baseline `{canon['logged_base_ms']} ms` → **`v2_hybrid` `{canon['logged_best_ms']} ms` (`{canon['speedup_x']}×`)**
- **Winning Blind `pg_hint_plan` Block:** `{canon['hints']}`
- **Agent Diagnosis (Purely From `t01..t21` `pg_stats` & `[EST_ERR]`):** {canon['rationale']}

### Anonymized SQL Query (`benchmarks/imdb_anon/queries/17c.sql`)
```sql
{canon['sql']}
```

### Default vs. MCP-Steered Plan on `imdb_anon`
```text
--- BEFORE ({canon['logged_base_ms']} ms) ---
{canon['base_plan'].strip()}

--- AFTER ({canon['logged_best_ms']} ms) ---
{canon['after_plan'].strip()}
```

---

## Case Study 6: `STATS-CEB s070` — Unseen Real-World Generalization (`{cs70['speedup_x']}×`)
- **Logged Latencies:** Baseline `{cs70['logged_base_ms']} ms` → **`v2_120b` `{cs70['logged_best_ms']} ms` (`{cs70['speedup_x']}×`)**
- **Winning `pg_hint_plan` Block:** `{cs70['hints']}`
- **Agent Diagnosis:** {cs70['rationale']}

```sql
{cs70['sql']}
```

```text
--- BEFORE ({cs70['logged_base_ms']} ms) ---
{cs70['base_plan'].strip()}

--- AFTER ({cs70['logged_best_ms']} ms) ---
{cs70['after_plan'].strip()}
```
"""
    MD_OUT.write_text(md, encoding="utf-8")
    print(f"Saved case studies markdown -> {MD_OUT}")


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    live_data = asyncio.run(collect_live_plans())
    build_figure(live_data)
    write_markdown(live_data)


if __name__ == "__main__":
    main()
