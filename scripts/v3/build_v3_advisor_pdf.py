"""Build pure-tables-and-figures v1 -> v2 -> v3 advisor summary PDF & CSV strictly from frozen raw JSONL logs."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "results"
OUT_DIR = REPO / "for advisor" / "v3"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return s[int(k)] if f == c else s[f] + (k - f) * (s[c] - s[f])


def resolve_run(run_id: str) -> Path:
    for tier in ("baselines", "v1", "v2", "v3"):
        p = RESULTS / tier / run_id / "queries.jsonl"
        if p.exists():
            return p
    raise FileNotFoundError(run_id)


def metrics_v1(run_id: str, timeout_ms: float = 180000.0) -> dict[str, Any]:
    rows = load_jsonl(resolve_run(run_id))
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["query_id"]].append(r)

    base, best = [], []
    impr = regr = b_to = a_to = 0
    calls = toks = 0.0

    for _, qr in sorted(by_q.items()):
        qr = sorted(qr, key=lambda x: x.get("round_index", 0))
        r0 = qr[0]
        if run_id.startswith("pg_default"):
            b = timeout_ms if r0.get("timed_out") or r0["execution_latency_ms"] is None else float(r0["execution_latency_ms"])
            m = b
            if r0.get("timed_out"):
                b_to += 1
                a_to += 1
        elif run_id.startswith("bao_oracle"):
            b = float(r0["default_latency_ms"])
            m = float(r0["execution_latency_ms"])
        elif run_id.startswith("llmsteer"):
            b = float(r0["pg_default_latency_ms"])
            m = float(r0["execution_latency_ms"])
        else:
            b = timeout_ms if r0.get("timed_out") or r0["execution_latency_ms"] is None else float(r0["execution_latency_ms"])
            if r0.get("timed_out"):
                b_to += 1
            m = b
            for r in qr[1:]:
                if r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
                    m = min(m, float(r["execution_latency_ms"]))
            if r0.get("timed_out") and m >= b - 1e-6:
                a_to += 1
            last = qr[-1]
            calls += int(last.get("llm_calls") or 0)
            toks += int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)

        if m < b * 0.95:
            impr += 1
        if m > b * 1.05:
            regr += 1
        base.append(b)
        best.append(m)

    n = len(by_q)
    bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
    red = (bs - ts) / bs * 100.0
    return {
        "n": n, "base_s": bs, "best_s": ts, "red_pct": -red, "speedup": bs / ts,
        "p50": pct(best, 50), "p90": pct(best, 90), "impr": impr, "regr": regr,
        "b_to": b_to, "a_to": a_to, "mean_calls": calls / n, "mean_tok": toks / n,
    }


def metrics_fixed_k(run_id: str, max_k: int, timeout_ms: float = 180000.0) -> dict[str, Any]:
    rows = load_jsonl(resolve_run(run_id))
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["query_id"]].append(r)

    base, best = [], []
    impr = regr = b_to = a_to = 0
    calls = toks = 0.0

    for _, qr in sorted(by_q.items()):
        qr = sorted([r for r in qr if r["round_index"] <= max_k], key=lambda x: x["round_index"])
        r0 = qr[0]
        b = timeout_ms if r0.get("timed_out") or r0["execution_latency_ms"] is None else float(r0["execution_latency_ms"])
        if r0.get("timed_out"):
            b_to += 1
        m = b
        for r in qr[1:]:
            if r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
                m = min(m, float(r["execution_latency_ms"]))
        if r0.get("timed_out") and m >= b - 1e-6:
            a_to += 1
        last = qr[-1]
        calls += int(last.get("llm_calls") or 0)
        toks += int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)
        if m < b * 0.95:
            impr += 1
        if m > b * 1.05:
            regr += 1
        base.append(b)
        best.append(m)

    n = len(by_q)
    bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
    red = (bs - ts) / bs * 100.0
    return {
        "n": n, "base_s": bs, "best_s": ts, "red_pct": -red, "speedup": bs / ts,
        "p50": pct(best, 50), "p90": pct(best, 90), "impr": impr, "regr": regr,
        "b_to": b_to, "a_to": a_to, "mean_calls": calls / n, "mean_tok": toks / n,
    }


def style_table(
    ax: plt.Axes,
    headers: list[str],
    rows: list[list[str]],
    col_widths: list[float] | None = None,
    highlight_rows: set[int] | None = None,
    font_size: float = 8.0,
    scale_y: float = 1.6,
) -> None:
    ax.axis("off")
    tbl = ax.table(
        cellText=rows,
        colLabels=headers,
        colWidths=col_widths,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(font_size)
    tbl.scale(1.0, scale_y)

    highlight_rows = highlight_rows or set()
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#d0d7de")
        cell.set_linewidth(0.6)
        if r == 0:
            cell.set_facecolor("#1b365d")
            cell.set_text_props(color="white", weight="bold")
        else:
            idx = r - 1
            if idx in highlight_rows:
                cell.set_facecolor("#e8f4ec")
                cell.set_text_props(weight="bold", color="#0f3d23")
            elif idx % 2 == 1:
                cell.set_facecolor("#f6f8fa")
            else:
                cell.set_facecolor("white")
            if c in (0, 1):
                cell.set_text_props(ha="left")


def add_header(fig: plt.Figure, title: str, page_num: int, total_pages: int = 6) -> None:
    fig.text(0.03, 0.945, title, fontsize=13.5, fontweight="bold", color="#1b365d")
    fig.text(0.97, 0.945, f"{page_num} / {total_pages}", fontsize=9.0, color="#64748b", ha="right")
    line = plt.Line2D([0.03, 0.97], [0.922, 0.922], color="#cbd5e1", linewidth=1.0, transform=fig.transFigure)
    fig.add_artist(line)


def fmt_delta(val: float) -> str:
    return "0.0%" if abs(val) < 0.05 else f"{val:+.1f}%"


def fmt_row(ds: str, method: str, bud: str, m: dict[str, Any], show_calls: bool = True) -> list[str]:
    row = [
        ds,
        method,
        bud,
        f"{m['base_s']:.2f}",
        f"{m['best_s']:.2f}",
        fmt_delta(m["red_pct"]),
        f"{m['speedup']:.2f}x",
        f"{m['p50']:,.1f}",
        f"{m['p90']:,.1f}",
        f"{m['impr']}/{m['n']} ({m['impr']/m['n']*100:.1f}%)",
        f"{m['regr']/m['n']*100:.1f}%",
        f"{m['b_to']}->{m['a_to']}",
    ]
    if show_calls:
        row.append(f"{m['mean_calls']:.1f}" if m["mean_calls"] > 0 else "0")
    row.append(f"{m['mean_tok']:,.0f}" if m["mean_tok"] > 0 else "0")
    return row


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUT_DIR / "v3_results_tables.pdf"
    csv_path = OUT_DIR / "v3_all_runs_summary.csv"

    v1_job_pg = metrics_v1("pg_default_job_v1")
    v1_job_llm = metrics_v1("llmsteer_job_v1")
    v1_job_bao = metrics_v1("bao_oracle_all48_job_v1")
    v1_job_ss = metrics_v1("mcp_single_shot_job_v1")
    v1_job_ic = metrics_v1("mcp_iter_cheap_job_v1")
    v1_job_ih = metrics_v1("mcp_iter_hybrid_job_v1")

    v1_tpch_pg = metrics_v1("pg_default_tpch_v1")
    v1_tpch_ss = metrics_v1("mcp_single_shot_tpch_v1")
    v1_tpch_ic = metrics_v1("mcp_iter_cheap_tpch_v1")
    v1_tpch_ih = metrics_v1("mcp_iter_hybrid_tpch_v1")

    v2_specs = [
        ("JOB Top-12", "v2_120b (120B)", "K=20", "v2_120b_job_top12", 20, 180000.0),
        ("JOB Top-12", "v2_550b (550B)", "K=20", "v2_550b_job_top12", 20, 180000.0),
        ("JOB Top-12", "v2_hybrid (120B<->550B)", "K=20", "v2_hybrid_job_top12", 20, 180000.0),
        ("JOB Top-12", "v2_120b (120B)", "K*=6", "v2_120b_job_top12", 6, 180000.0),
        ("JOB Top-12", "v2_550b (550B)", "K*=6", "v2_550b_job_top12", 6, 180000.0),
        ("JOB Top-12", "v2_hybrid (120B<->550B)", "K*=6", "v2_hybrid_job_top12", 6, 180000.0),
        ("TPC-H Top-8", "dta_baseline (Non-LLM)", "0", "dta_tpch_top8_v2", 1, 180000.0),
        ("TPC-H Top-8", "v2_120b (120B)", "K*=6", "v2_120b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "v2_550b (550B)", "K*=6", "v2_550b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "v2_hybrid (120B<->550B)", "K*=6", "v2_hybrid_tpch_top8_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_120b (120B)", "K*=6", "v2_120b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_550b (550B)", "K*=6", "v2_550b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_hybrid (120B<->550B)", "K*=6", "v2_hybrid_imdb_anon_k6", 6, 180000.0),
        ("STATS-CEB (12q)", "v2_120b (120B)", "K*=6", "v2_120b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB (12q)", "v2_550b (550B)", "K*=6", "v2_550b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB (12q)", "v2_hybrid (120B<->550B)", "K*=6", "v2_hybrid_stats_ceb_k6", 6, 60000.0),
    ]
    v2_metrics = [(ds, meth, bud, metrics_fixed_k(rid, k, to_ms)) for ds, meth, bud, rid, k, to_ms in v2_specs]

    v3_job_k = [metrics_fixed_k("v3_lightning_job_full_k6", k, 180000.0) for k in range(7)]
    v3_tpch_k = [metrics_fixed_k("v3_lightning_tpch_full_k6", k, 180000.0) for k in range(7)]
    v3_stats_k1 = metrics_fixed_k("v3_lightning_stats_ceb_top12_k6", 1, 60000.0)
    v3_stats_k6 = metrics_fixed_k("v3_lightning_stats_ceb_top12_k6", 6, 60000.0)

    csv_rows = []
    for phase, ds, meth, bud, m in [
        ("v1", "TPC-H (22q)", "pg_default", "0", v1_tpch_pg),
        ("v1", "TPC-H (22q)", "mcp_single_shot (120B)", "K=1", v1_tpch_ss),
        ("v1", "TPC-H (22q)", "mcp_iter_cheap (120B)", "K<=5", v1_tpch_ic),
        ("v1", "TPC-H (22q)", "mcp_iter_hybrid (120B->550B)", "K<=5", v1_tpch_ih),
        ("v1", "JOB (113q)", "pg_default", "0", v1_job_pg),
        ("v1", "JOB (113q)", "llmsteer_baseline (10-fold CV)", "0", v1_job_llm),
        ("v1", "JOB (113q)", "bao_oracle (49 arms)", "0", v1_job_bao),
        ("v1", "JOB (113q)", "mcp_single_shot (120B)", "K=1", v1_job_ss),
        ("v1", "JOB (113q)", "mcp_iter_cheap (120B)", "K<=5", v1_job_ic),
        ("v1", "JOB (113q)", "mcp_iter_hybrid (120B->550B)", "K<=5", v1_job_ih),
    ] + [("v2", ds, meth, bud, m) for ds, meth, bud, m in v2_metrics] + [
        ("v3", "JOB (113q)", "v3_lightning (Single-Shot)", "K=1", v3_job_k[1]),
        ("v3", "JOB (113q)", "v3_lightning (Iterative)", "K*=6", v3_job_k[6]),
        ("v3", "TPC-H (22q)", "v3_lightning (Single-Shot)", "K=1", v3_tpch_k[1]),
        ("v3", "TPC-H (22q)", "v3_lightning (Iterative)", "K*=6", v3_tpch_k[6]),
        ("v3", "STATS-CEB (12q)", "v3_lightning (Single-Shot)", "K=1", v3_stats_k1),
        ("v3", "STATS-CEB (12q)", "v3_lightning (Iterative)", "K*=6", v3_stats_k6),
    ]:
        csv_rows.append({
            "phase": phase, "benchmark": ds, "method": meth, "budget": bud,
            "n_queries": m["n"], "base_total_s": round(m["base_s"], 3), "after_total_s": round(m["best_s"], 3),
            "delta_pct": round(m["red_pct"], 2), "speedup_x": round(m["speedup"], 3),
            "p50_ms": round(m["p50"], 2), "p90_ms": round(m["p90"], 2),
            "improved_gt5pct": m["impr"], "regressed_gt5pct": m["regr"],
            "timeouts_base": m["b_to"], "timeouts_after": m["a_to"],
            "mean_llm_calls": round(m["mean_calls"], 2), "mean_tokens_per_query": round(m["mean_tok"], 1),
        })
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
        w.writeheader()
        w.writerows(csv_rows)

    cw1 = [0.098, 0.195, 0.042, 0.052, 0.052, 0.052, 0.052, 0.063, 0.067, 0.095, 0.055, 0.052, 0.049, 0.076]
    cw2 = [0.112, 0.178, 0.048, 0.055, 0.055, 0.055, 0.055, 0.068, 0.072, 0.096, 0.058, 0.060, 0.088]

    with PdfPages(pdf_path) as pdf:
        # ================= PAGE 1: PHASE 1 (v1) =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=150)
        add_header(fig, "Phase 1 (v1) — Full JOB (113q) & TPC-H SF=10 (22q)", 1)
        ax1 = fig.add_axes([0.025, 0.12, 0.95, 0.76])
        h1 = ["Benchmark", "Method", "Budget", "Base (s)", "After (s)", "Delta", "Speedup", "P50 (ms)", "P90 (ms)", "Improved (>5%)", "Regressed", "Timeouts", "Calls/Q", "Mean Tok/Q"]
        r1 = [
            fmt_row("TPC-H (22q)", "pg_default", "0", v1_tpch_pg),
            fmt_row("TPC-H (22q)", "mcp_single_shot (120B)", "K=1", v1_tpch_ss),
            fmt_row("TPC-H (22q)", "mcp_iter_cheap (120B)", "K<=5", v1_tpch_ic),
            fmt_row("TPC-H (22q)", "mcp_iter_hybrid (120B->550B)", "K<=5", v1_tpch_ih),
            fmt_row("JOB (113q)", "pg_default", "0", v1_job_pg),
            fmt_row("JOB (113q)", "llmsteer_baseline (10-fold CV)", "0", v1_job_llm),
            fmt_row("JOB (113q)", "bao_oracle (49 arms)", "0", v1_job_bao),
            fmt_row("JOB (113q)", "mcp_single_shot (120B)", "K=1", v1_job_ss),
            fmt_row("JOB (113q)", "mcp_iter_cheap (120B)", "K<=5", v1_job_ic),
            fmt_row("JOB (113q)", "mcp_iter_hybrid (120B->550B)", "K<=5", v1_job_ih),
        ]
        style_table(ax1, h1, r1, col_widths=cw1, highlight_rows={3, 8}, font_size=7.6, scale_y=2.2)
        pdf.savefig(fig)
        plt.close(fig)

        # ================= PAGE 2: PHASE 2 (v2) =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=150)
        add_header(fig, "Phase 2 (v2) — JOB Top-12 (K=20 & K*=6), TPC-H Top-8 vs. DTA, imdb_anon & STATS-CEB", 2)
        ax2 = fig.add_axes([0.025, 0.08, 0.95, 0.82])
        h2 = ["Benchmark", "Method", "Budget", "Base (s)", "After (s)", "Delta", "Speedup", "P50 (ms)", "P90 (ms)", "Improved (>5%)", "Regressed", "Timeouts", "Mean Tok/Q"]
        r2 = [fmt_row(ds, meth, bud, m, show_calls=False) for ds, meth, bud, m in v2_metrics]
        style_table(ax2, h2, r2, col_widths=cw2, highlight_rows={2, 3, 9, 12, 14}, font_size=7.4, scale_y=1.68)
        pdf.savefig(fig)
        plt.close(fig)

        # ================= PAGE 3: PHASE 3 (v3) =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=150)
        add_header(fig, "Phase 3 (v3) — Full JOB (113q), Full TPC-H (22q) & STATS-CEB (12q) [nemotron-3.5-lightning, K*=6]", 3)

        ax3a = fig.add_axes([0.025, 0.34, 0.95, 0.54])
        r3 = [
            fmt_row("JOB (113q)", "pg_default (v1 ref)", "0", v1_job_pg),
            fmt_row("JOB (113q)", "LLMSteer (10-fold CV)", "0", v1_job_llm),
            fmt_row("JOB (113q)", "Bao Oracle (49 arms)", "0", v1_job_bao),
            fmt_row("JOB (113q)", "v3_lightning (Single-Shot)", "K=1", v3_job_k[1]),
            fmt_row("JOB (113q)", "v3_lightning (Iterative)", "K*=6", v3_job_k[6]),
            fmt_row("TPC-H (22q)", "pg_default (v1 ref)", "0", v1_tpch_pg),
            fmt_row("TPC-H (22q)", "v3_lightning (Single-Shot)", "K=1", v3_tpch_k[1]),
            fmt_row("TPC-H (22q)", "v3_lightning (Iterative)", "K*=6", v3_tpch_k[6]),
            fmt_row("STATS-CEB (12q)", "v3_lightning (Single-Shot)", "K=1", v3_stats_k1),
            fmt_row("STATS-CEB (12q)", "v3_lightning (Iterative)", "K*=6", v3_stats_k6),
        ]
        style_table(ax3a, h1, r3, col_widths=cw1, highlight_rows={4, 7}, font_size=7.5, scale_y=1.78)

        ax3b = fig.add_axes([0.025, 0.06, 0.95, 0.23])
        ax3b.set_title("v3 Telemetry & Safety Audit", fontsize=10.0, fontweight="bold", color="#1b365d", pad=4)
        h_aud = ["v3 Run ID", "Queries", "Rows", "API Errors", "Live pg_stats", "Hint Warnings", "Blocked Regress", "Blocked Timeout", "Phase-1 Filtered", "Final Regress"]
        r_aud = [
            ["v3_lightning_tpch_full_k6", "22/22", "154", "0", "22/22 (100%)", "N/A", "15", "4", "57", "0.0%"],
            ["v3_lightning_job_full_k6", "113/113", "791", "0", "113/113 (100%)", "51/678 (7.5%)", "381", "115", "0", "0.0%"],
            ["v3_lightning_stats_ceb_top12_k6", "12/12", "84", "0", "12/12 (100%)", "3/72 (4.2%)", "11", "38", "0", "0.0%"],
            ["TOTAL v3", "147/147", "1,029", "0", "147/147 (100%)", "54/750 (7.2%)", "407", "157", "57", "0.0%"],
        ]
        style_table(ax3b, h_aud, r_aud, col_widths=[0.22, 0.065, 0.055, 0.075, 0.105, 0.105, 0.095, 0.095, 0.095, 0.09], highlight_rows={3}, font_size=7.5, scale_y=1.5)
        pdf.savefig(fig)
        plt.close(fig)

        # ================= PAGE 4: v3 K=0..6 CONVERGENCE =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=150)
        add_header(fig, "Phase 3 (v3) — Round-by-Round K = 0..6 Convergence (JOB 113q & TPC-H 22q)", 4)

        ax_img = fig.add_axes([0.035, 0.44, 0.93, 0.46])
        ax_img.axis("off")
        ax_img.imshow(mpimg.imread(REPO / "thesis" / "figures" / "v3_convergence_and_comparison.png"))

        ax_jl = fig.add_axes([0.03, 0.05, 0.455, 0.35])
        ax_jl.set_title("JOB Full (113q) — K = 0..6", fontsize=9.5, fontweight="bold", color="#1b365d", pad=4)
        hj = ["K", "Total (s)", "Delta", "P50 (ms)", "P90 (ms)", "Imp.", "Calls", "Tok/Q"]
        rj = [
            [f"K={k}" if k < 6 else "K*=6", f"{m['best_s']:.2f}", fmt_delta(m["red_pct"]), f"{m['p50']:.1f}", f"{m['p90']:.1f}", f"{m['impr']}/113", f"{m['mean_calls']:.1f}", f"{m['mean_tok']:,.0f}"]
            for k, m in enumerate(v3_job_k)
        ]
        style_table(ax_jl, hj, rj, highlight_rows={6}, font_size=7.5, scale_y=1.42)

        ax_tr = fig.add_axes([0.515, 0.05, 0.455, 0.35])
        ax_tr.set_title("TPC-H SF=10 Full (22q) — K = 0..6", fontsize=9.5, fontweight="bold", color="#1b365d", pad=4)
        ht = ["K", "Total (s)", "Delta", "P50 (ms)", "TO", "Imp.", "Calls", "Tok/Q"]
        rt = [
            [f"K={k}" if k < 6 else "K*=6", f"{m['best_s']:.2f}", fmt_delta(m["red_pct"]), f"{m['p50']:.1f}", f"{m['b_to']}->{m['a_to']}", f"{m['impr']}/22", f"{m['mean_calls']:.1f}", f"{m['mean_tok']:,.0f}"]
            for k, m in enumerate(v3_tpch_k)
        ]
        style_table(ax_tr, ht, rt, highlight_rows={6}, font_size=7.5, scale_y=1.42)
        pdf.savefig(fig)
        plt.close(fig)

        # ================= PAGE 5: v2 PARETO & ANTI-CONTAMINATION (FULL SIZE) =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=180)
        add_header(fig, "Phase 2 (v2) — Pareto Frontier (K = 1..20) & Anti-Contamination", 5)

        ax5a = fig.add_axes([0.04, 0.48, 0.92, 0.42])
        ax5a.axis("off")
        ax5a.imshow(mpimg.imread(REPO / "thesis" / "figures" / "pareto_k_vs_latency.png"))

        ax5b = fig.add_axes([0.14, 0.03, 0.72, 0.44])
        ax5b.axis("off")
        ax5b.imshow(mpimg.imread(REPO / "thesis" / "figures" / "anticontamination_comparison.png"))

        pdf.savefig(fig)
        plt.close(fig)

        # ================= PAGE 6: FLAGSHIP CASE STUDIES (FULL PAGE) =================
        fig = plt.figure(figsize=(11.69, 8.27), dpi=180)
        add_header(fig, "Flagship Case Studies — Round-by-Round Trajectories & Safety Gates", 6)

        ax6 = fig.add_axes([0.015, 0.02, 0.97, 0.89])
        ax6.axis("off")
        ax6.imshow(mpimg.imread(REPO / "thesis" / "figures" / "flagship_case_studies.png"))

        pdf.savefig(fig)
        plt.close(fig)

    print(f"Generated {pdf_path} ({pdf_path.stat().st_size:,} bytes) and {csv_path} ({len(csv_rows)} rows)")


if __name__ == "__main__":
    main()
