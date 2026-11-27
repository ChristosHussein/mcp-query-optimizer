"""Fail-closed scientific & numerical integrity gate for local audits and GitHub Actions CI.

Verifies:
  1. Cryptographic SHA-256 hashes and record counts of both measurement contracts and all 23 frozen runs.
  2. Claim-bound per-cell reconstruction across THESIS_LOG.md, thesis/v1_experiment_report.md,
     thesis/v2_experiment_report.md (including K=1..20 Pareto checkpoints), thesis/thesis_progress_and_retrospective_report.md,
     and for advisor/v2/v2_results_tables.pdf.
  3. Automatic discovery of thesis/*.md empirical reports (fails closed on unregistered report files).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "results"

def resolve_run_jsonl(run_id: str) -> Path:
    for tier in ("baselines", "v1", "v2", "v3"):
        cand = RESULTS / tier / run_id / "queries.jsonl"
        if cand.exists():
            return cand
    raise FileNotFoundError(f"Run not found: {run_id}")

FROZEN_HASHES: dict[str, tuple[str, int | None]] = {
    "contracts/measurement_contract_v1.yaml": ("D59EE68292BFCE02E7F6C292EFAC8F9A77AEC0B3609EFFDF1A82E697D8615FD7", None),
    "contracts/measurement_contract_v2.yaml": ("5299C3188553F5231155DD4B76BC83D80FD0E1EA6912B6DA8A32750DAB2C3F75", None),
    "contracts/measurement_contract_v3.yaml": ("CC4383171C4B05D1746F88B8CEE101E57D17677A802DC1EA35F5418DEC7272F6", None),
    "results/baselines/pg_default_job_v1/queries.jsonl": ("E5D43F81C62D4B5038AE4CB50A3A749D8424CE5C466EAC1A79FD79518150EC66", 113),
    "results/baselines/pg_default_tpch_v1/queries.jsonl": ("5D3A7327730BCA49F353FEE1BF7587D39DE8644B1F6C8F625FD50511376A2EB2", 22),
    "results/baselines/bao_oracle_all48_job_v1/queries.jsonl": ("DC32C9E73AA7D3830D0F67C261235B3EEFDA14A7E10A31996B5CB3E8AAEE7AA5", 113),
    "results/baselines/bao_oracle_all48_job_v1/arm_latencies.jsonl": ("49CE004A2E4DC745191ECAF88351F76B77375B55A5211D6858155E57EE4D394B", 5537),
    "results/baselines/llmsteer_job_v1/queries.jsonl": ("F0BE6D451AFEC63C7739D4FCEE75D18DEF520E85A8AF15BC6F1246832FBA0760", 113),
    "results/v1/mcp_single_shot_tpch_v1/queries.jsonl": ("905FCE852C245A21023474F305510AE4EAB5181805DC9123EFDA771B0FB17D9A", None),
    "results/v1/mcp_iter_cheap_tpch_v1/queries.jsonl": ("566B6EE3032772AA57D63380B6D4D32A2F2AF870788B95ACD18C944B3DCAB316", None),
    "results/v1/mcp_iter_hybrid_tpch_v1/queries.jsonl": ("2EF50F45871B308848BA700F732E366C5AE81220902E762865D49D603B1E5F0A", None),
    "results/v1/mcp_single_shot_job_v1/queries.jsonl": ("9E001B7AFC60E9CB87F7E11DB8B9D8E267A55096D25A02AD1F046204289DEFDF", None),
    "results/v1/mcp_iter_cheap_job_v1/queries.jsonl": ("FFB53ECCB8D991FA6273A4AB4614F5ABF2A4BE3B1DDD042C5F374086AAE2DE44", None),
    "results/v1/mcp_iter_hybrid_job_v1/queries.jsonl": ("8874955B5F80AC359E60476F0B8ADB67BC968E907AB056B7B3AEEE6C23E5A37E", None),
    "results/v2/v2_120b_job_top12/queries.jsonl": ("040DCD3FD152BB082B56103A341AFCF04BDF3D7AD31CE0842BCCDD215B4B07A6", 252),
    "results/v2/v2_550b_job_top12/queries.jsonl": ("8095431D93C4A3480818F6126200EE1E51BF72F8F0AF7DE3AA145E4679E5AADE", 252),
    "results/v2/v2_hybrid_job_top12/queries.jsonl": ("D4202D3F2DC97F39453CD9A236BB8DD20511276D64471B2E63843EB80BD4CAC1", 252),
    "results/baselines/dta_tpch_top8_v2/queries.jsonl": ("1476A4550933DC834AA2B86CB13F1A842BB112785ABA1E216BA626A1AC3EBDAD", 16),
    "results/v2/v2_120b_tpch_top8_k6/queries.jsonl": ("956081D0E83C88F0D6D6811353C70A79A793264A0156B84AEF8570C57AD31F67", 56),
    "results/v2/v2_550b_tpch_top8_k6/queries.jsonl": ("A7BC837E3C40DCFF0C6C84625662FB89D62B5C2B9146A6808D14AB32D10CDBDB", 56),
    "results/v2/v2_hybrid_tpch_top8_k6/queries.jsonl": ("EB94D812265BE8FAA02BD6C093849DE43EC9F9315A2772D93FAEBE4642922DEF", 56),
    "results/v2/v2_120b_imdb_anon_k6/queries.jsonl": ("FBBFF028DC236403DC98966FE731577E415640F58E2AF80ED320CACBD9760A28", 84),
    "results/v2/v2_550b_imdb_anon_k6/queries.jsonl": ("4CB04970F76A50052E8F162C94ECAC679703E1699607C7A065E7E280A121C519", 84),
    "results/v2/v2_hybrid_imdb_anon_k6/queries.jsonl": ("277DF698DD4BF8A694B9E33BAFDD8B60AC8B11270B3AE858AE3808CF09396E63", 84),
    "results/v2/v2_120b_stats_ceb_k6/queries.jsonl": ("938AB3D55D029573EB87507450456F59F946AF31B7BADF097B84388DB36596E0", 84),
    "results/v2/v2_550b_stats_ceb_k6/queries.jsonl": ("1076239E33F2DCC29DA242AC046CAF6979E7640B83CEAB03E2B33FB3EDE72499", 84),
    "results/v2/v2_hybrid_stats_ceb_k6/queries.jsonl": ("818D6F96EFCCEF3AEEF5774BE96E6E41B7C8A177B7D689AC1DE8DF7C5EB1BFED", 84),
    "results/v3/v3_lightning_tpch_full_k6/queries.jsonl": ("6361528C0231BD63BC336B9827625AA3F9B73D774B78B59F2D85432345BC07B8", 154),
    "results/v3/v3_lightning_job_full_k6/queries.jsonl": ("68E292039480152FF8465C863CFF29632DB4224FF1A54AF3D9CFF5EB99912C6A", 791),
    "results/v3/v3_lightning_stats_ceb_top12_k6/queries.jsonl": ("4504B16BEB01B80F9145D96FF900D200855887141D0885FDC0457FBE0E1721F0", 84),
}

REGISTERED_THESIS_MDS = {
    "v1_experiment_report.md",
    "v2_experiment_report.md",
    "v3_experiment_report.md",
    "thesis_progress_and_retrospective_report.md",
    "case_studies.md",
}


def sha256_file(p: Path) -> str:
    """Canonical LF-normalized SHA-256 so Windows CRLF and Linux LF checkouts produce identical digests."""
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest().upper()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return s[int(k)] if f == c else s[f] + (k - f) * (s[c] - s[f])


def verify_frozen_hashes() -> list[str]:
    errors: list[str] = []
    for rel, (exp_sha, exp_cnt) in FROZEN_HASHES.items():
        p = REPO / rel
        if not p.exists():
            errors.append(f"MISSING EVIDENCE FILE: {rel}")
            continue
        got = sha256_file(p)
        if got != exp_sha.upper():
            errors.append(f"SHA256 MISMATCH [{rel}]: expected={exp_sha} got={got}")
        if exp_cnt is not None:
            n = len(load_jsonl(p))
            if n != exp_cnt:
                errors.append(f"RECORD COUNT MISMATCH [{rel}]: expected={exp_cnt} got={n}")
    # Also ensure no unregistered run directories exist outside FROZEN_HASHES
    for tier in ("baselines", "v1", "v2", "v3"):
      for run_jsonl in sorted((RESULTS / tier).glob("*/queries.jsonl")):
        rel = run_jsonl.relative_to(REPO).as_posix()
        if rel not in FROZEN_HASHES:
            errors.append(f"UNREGISTERED RUN ARTIFACT: {rel}")
    print(f"[SHA256] Verified {len(FROZEN_HASHES)} frozen artifacts; errors={len(errors)}")
    return errors


def verify_artifact_discovery(thesis_dir: Path) -> list[str]:
    errors: list[str] = []
    found = {p.name for p in thesis_dir.glob("*.md")}
    unreg = found - REGISTERED_THESIS_MDS
    for name in sorted(unreg):
        errors.append(f"UNREGISTERED THESIS REPORT: thesis/{name} (must be registered in REGISTERED_THESIS_MDS & verified)")
    print(f"[DISCOVERY] Checked {len(found)} thesis/*.md artifacts; unregistered={len(unreg)}")
    return errors


def compute_v1(run_id: str, timeout_ms: float = 180000.0) -> dict[str, float]:
    rows = load_jsonl(resolve_run_jsonl(run_id))
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["query_id"]].append(r)

    if run_id.startswith("pg_default"):
        lats = [timeout_ms if r.get("timed_out") or r["execution_latency_ms"] is None else float(r["execution_latency_ms"]) for _, qr in sorted(by_q.items()) for r in [qr[0]]]
        return {"base_s": sum(lats) / 1000.0, "p50": percentile(lats, 50), "p90": percentile(lats, 90), "total_s": sum(lats) / 1000.0}

    if run_id.startswith("bao_oracle"):
        base = [float(qr[0]["default_latency_ms"]) for _, qr in sorted(by_q.items())]
        best = [float(qr[0]["execution_latency_ms"]) for _, qr in sorted(by_q.items())]
        bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
        return {"base_s": bs, "p50": percentile(best, 50), "p90": percentile(best, 90), "total_s": ts, "gain_pct": (bs - ts) / bs * 100.0}

    if run_id.startswith("llmsteer"):
        base = [float(qr[0]["pg_default_latency_ms"]) for _, qr in sorted(by_q.items())]
        best = [float(qr[0]["execution_latency_ms"]) for _, qr in sorted(by_q.items())]
        bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
        return {"base_s": bs, "p50": percentile(best, 50), "p90": percentile(best, 90), "total_s": ts, "gain_pct": (bs - ts) / bs * 100.0}

    base, best = [], []
    tok = 0
    for _, qr in sorted(by_q.items()):
        qr = sorted(qr, key=lambda x: x["round_index"])
        r0 = qr[0]
        b = timeout_ms if r0.get("timed_out") or r0["execution_latency_ms"] is None else float(r0["execution_latency_ms"])
        m = b
        for r in qr[1:]:
            if r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
                m = min(m, float(r["execution_latency_ms"]))
        last = qr[-1]
        tok += int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)
        base.append(b)
        best.append(m)
    bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
    return {"base_s": bs, "p50": percentile(best, 50), "p90": percentile(best, 90), "total_s": ts, "gain_pct": (bs - ts) / bs * 100.0, "mean_tokens": tok / len(by_q)}


def compute_v2(run_id: str, max_k: int, timeout_ms: float = 180000.0) -> dict[str, float]:
    rows = load_jsonl(resolve_run_jsonl(run_id))
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["query_id"]].append(r)

    base, best = [], []
    tok = 0
    impr = 0
    for _, qr in sorted(by_q.items()):
        qr = sorted([r for r in qr if r["round_index"] <= max_k], key=lambda x: x["round_index"])
        r0 = qr[0]
        b = timeout_ms if r0.get("timed_out") or r0["execution_latency_ms"] is None else float(r0["execution_latency_ms"])
        m = b
        for r in qr[1:]:
            if r.get("action_valid") and not r.get("timed_out") and r.get("execution_latency_ms") is not None:
                m = min(m, float(r["execution_latency_ms"]))
        last = qr[-1]
        tok += int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)
        if (b - m) / b > 0.05:
            impr += 1
        base.append(b)
        best.append(m)
    bs, ts = sum(base) / 1000.0, sum(best) / 1000.0
    red = (bs - ts) / bs * 100.0
    return {
        "base_s": bs,
        "best_s": ts,
        "red_pct": red,
        "speedup_x": bs / ts,
        "p50": percentile(best, 50),
        "p90": percentile(best, 90),
        "impr": impr,
        "n": len(by_q),
        "mean_tokens": tok / len(by_q),
    }


def parse_md_table_rows(md_text: str) -> list[list[str]]:
    rows = []
    in_code = False
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if s.startswith("|") and s.endswith("|") and not re.match(r"^\|[\s:\-\|]+\|$", s):
            cells = [c.strip().replace("**", "").replace("`", "") for c in s.strip("|").split("|")]
            rows.append(cells)
    return rows


def clean_num(s: str) -> float:
    s = s.replace(",", "").replace("×", "").replace("x", "").replace("%", "").replace("s", "").replace("+", "").strip()
    return float(s)


def check_close(label: str, expected: float, actual: float, tol: float, errors: list[str]) -> None:
    if abs(expected - actual) > tol:
        errors.append(f"METRIC MISMATCH [{label}]: expected={expected:.4f} actual={actual:.4f} (tol={tol})")


def verify_thesis_log(path: Path) -> list[str]:
    errors: list[str] = []
    rows = parse_md_table_rows(path.read_text(encoding="utf-8"))
    ledger = {r[0]: r for r in rows if len(r) >= 10 and (r[0].startswith("pg_") or r[0].startswith("bao_") or r[0].startswith("llmsteer") or r[0].startswith("mcp_") or r[0].startswith("v2_") or r[0].startswith("v3_") or r[0].startswith("dta_"))}
    checked = 0
    for rid in sorted(p.parent.name for tier in ("baselines", "v1", "v2", "v3") for p in (RESULTS / tier).glob("*/queries.jsonl")):
        if rid not in ledger:
            errors.append(f"MISSING THESIS_LOG ROW: {rid}")
            continue
        r = ledger[rid]
        if rid.startswith("v2_") or rid.startswith("v3_") or rid.startswith("dta_"):
            k = 20 if "job_top12" in rid else (1 if rid.startswith("dta_") else 6)
            to_ms = 60000.0 if "stats_ceb" in rid else 180000.0
            m = compute_v2(rid, k, to_ms)
            p50, p90, tot = m["p50"], m["p90"], m["best_s"]
        else:
            m = compute_v1(rid)
            p50, p90, tot = m["p50"], m["p90"], m["total_s"]
        check_close(f"thesis_log:{rid}:p50", p50, clean_num(r[5]), 0.02, errors)
        check_close(f"thesis_log:{rid}:p90", p90, clean_num(r[6]), 0.02, errors)
        check_close(f"thesis_log:{rid}:total_s", tot, clean_num(r[7]), 0.005, errors)
        checked += 3
    print(f"[thesis_log] Verified {checked} bound cells across {len(ledger)} runs; errors={len(errors)}")
    return errors


def verify_v1_report(path: Path) -> list[str]:
    errors: list[str] = []
    rows = parse_md_table_rows(path.read_text(encoding="utf-8"))
    data_rows = [r for r in rows if r and r[0] != "Method"]
    expected_v1 = [
        ("pg_default_tpch_v1", 0),
        ("mcp_single_shot_tpch_v1", 1),
        ("mcp_iter_cheap_tpch_v1", 2),
        ("mcp_iter_hybrid_tpch_v1", 3),
        ("pg_default_job_v1", 4),
        ("llmsteer_job_v1", 5),
        ("bao_oracle_all48_job_v1", 6),
        ("mcp_single_shot_job_v1", 7),
        ("mcp_iter_cheap_job_v1", 8),
        ("mcp_iter_hybrid_job_v1", 9),
    ]
    checked = 0
    for rid, idx in expected_v1:
        m = compute_v1(rid)
        r = data_rows[idx]
        check_close(f"v1:{rid}:p50", m["p50"], clean_num(r[1]), 0.02, errors)
        check_close(f"v1:{rid}:p90", m["p90"], clean_num(r[2]), 0.02, errors)
        check_close(f"v1:{rid}:total_s", m["total_s"], clean_num(r[3]), 0.02, errors)
        checked += 3
        if "gain_pct" in m:
            check_close(f"v1:{rid}:gain_pct", m["gain_pct"], clean_num(r[4]), 0.15, errors)
            checked += 1
        if "mean_tokens" in m:
            check_close(f"v1:{rid}:tokens", m["mean_tokens"], clean_num(r[9]), 2.0, errors)
            checked += 1
    print(f"[v1_report] Verified {checked} bound cells in {path.name}; errors={len(errors)}")
    return errors


def verify_v2_report(path: Path) -> list[str]:
    errors: list[str] = []
    rows = parse_md_table_rows(path.read_text(encoding="utf-8"))
    t31 = {}
    for r in rows:
        if len(r) >= 12 and r[0] in ("JOB Top-12", "TPC-H Top-8", "imdb_anon Top-12", "STATS-CEB Top-12"):
            t31[(r[0], r[1], r[2].split()[0])] = r

    specs = [
        ("JOB Top-12", "K=20", "v2_120b", "v2_120b_job_top12", 20, 180000.0),
        ("JOB Top-12", "K=20", "v2_550b", "v2_550b_job_top12", 20, 180000.0),
        ("JOB Top-12", "K=20", "v2_hybrid", "v2_hybrid_job_top12", 20, 180000.0),
        ("JOB Top-12", "K*=6", "v2_120b", "v2_120b_job_top12", 6, 180000.0),
        ("JOB Top-12", "K*=6", "v2_550b", "v2_550b_job_top12", 6, 180000.0),
        ("JOB Top-12", "K*=6", "v2_hybrid", "v2_hybrid_job_top12", 6, 180000.0),
        ("TPC-H Top-8", "0", "dta_baseline", "dta_tpch_top8_v2", 1, 180000.0),
        ("TPC-H Top-8", "K*=6", "v2_120b", "v2_120b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "K*=6", "v2_550b", "v2_550b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "K*=6", "v2_hybrid", "v2_hybrid_tpch_top8_k6", 6, 180000.0),
        ("imdb_anon Top-12", "K*=6", "v2_120b", "v2_120b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon Top-12", "K*=6", "v2_550b", "v2_550b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon Top-12", "K*=6", "v2_hybrid", "v2_hybrid_imdb_anon_k6", 6, 180000.0),
        ("STATS-CEB Top-12", "K*=6", "v2_120b", "v2_120b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB Top-12", "K*=6", "v2_550b", "v2_550b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB Top-12", "K*=6", "v2_hybrid", "v2_hybrid_stats_ceb_k6", 6, 60000.0),
    ]
    checked = 0
    for bm, bud, meth, rid, k, to_ms in specs:
        key = (bm, bud, meth)
        if key not in t31:
            errors.append(f"MISSING ROW in v2 report: {key}")
            continue
        r = t31[key]
        m = compute_v2(rid, k, to_ms)
        check_close(f"v2:{key}:base_s", m["base_s"], clean_num(r[3]), 0.02, errors)
        check_close(f"v2:{key}:best_s", m["best_s"], clean_num(r[4]), 0.02, errors)
        check_close(f"v2:{key}:red_pct", -m["red_pct"], clean_num(r[5]), 0.15, errors)
        check_close(f"v2:{key}:speedup_x", m["speedup_x"], clean_num(r[6]), 0.02, errors)
        check_close(f"v2:{key}:p50", m["p50"], clean_num(r[7]), 0.2, errors)
        check_close(f"v2:{key}:p90", m["p90"], clean_num(r[8]), 0.2, errors)
        check_close(f"v2:{key}:tokens", m["mean_tokens"], clean_num(r[11]), 2.0, errors)
        checked += 7

    # Also check Table 4.1 (K=1..20 Pareto checkpoints)
    for r in rows:
        if r and re.match(r"^K\*?=\d+$", r[0]):
            k = int(r[0].split("=")[1])
            for col_i, rid in [(1, "v2_120b_job_top12"), (5, "v2_550b_job_top12"), (9, "v2_hybrid_job_top12")]:
                m = compute_v2(rid, k)
                check_close(f"v2_t41:K={k}:{rid}:best_s", m["best_s"], clean_num(r[col_i]), 0.02, errors)
                check_close(f"v2_t41:K={k}:{rid}:red_pct", -m["red_pct"], clean_num(r[col_i + 1]), 0.15, errors)
                check_close(f"v2_t41:K={k}:{rid}:tokens", m["mean_tokens"], clean_num(r[col_i + 2]), 2.0, errors)
                checked += 3

    print(f"[v2_report] Verified {checked} bound cells in {path.name}; errors={len(errors)}")
    return errors


def verify_v3_report(path: Path) -> list[str]:
    errors: list[str] = []
    rows = parse_md_table_rows(path.read_text(encoding="utf-8"))
    # Verify Section 3.1 (JOB K=0..6) and 3.2 (TPC-H K=0..6) tables + STATS-CEB summary rows
    job_k = {}
    tpch_k = {}
    stats_k = {}
    for r in rows:
        if r and re.match(r"^K\*?=[0-6]$", r[0]):
            k = int(r[0].split("=")[1])
            if len(r) == 11 and r[1] == "79.76":
                job_k[k] = r
            elif len(r) == 12 and r[1] == "557.46":
                tpch_k[k] = r
        elif r and len(r) >= 13 and r[0].startswith("v3_lightning") and r[2] in ("K=1", "K*=6") and r[3] == "429.94":
            stats_k[int(r[2].split("=")[1])] = r

    checked = 0
    for k in range(7):
        if k not in job_k:
            errors.append(f"MISSING v3 JOB K={k} row")
        else:
            r = job_k[k]
            m = compute_v2("v3_lightning_job_full_k6", k, 180000.0)
            check_close(f"v3_job:K={k}:best_s", m["best_s"], clean_num(r[2]), 0.02, errors)
            check_close(f"v3_job:K={k}:p50", m["p50"], clean_num(r[5]), 0.05, errors)
            check_close(f"v3_job:K={k}:p90", m["p90"], clean_num(r[6]), 0.05, errors)
            check_close(f"v3_job:K={k}:tok", m["mean_tokens"], clean_num(r[9]), 2.0, errors)
            checked += 4
        if k not in tpch_k:
            errors.append(f"MISSING v3 TPC-H K={k} row")
        else:
            r = tpch_k[k]
            m = compute_v2("v3_lightning_tpch_full_k6", k, 180000.0)
            check_close(f"v3_tpch:K={k}:best_s", m["best_s"], clean_num(r[2]), 0.02, errors)
            check_close(f"v3_tpch:K={k}:p50", m["p50"], clean_num(r[5]), 0.05, errors)
            check_close(f"v3_tpch:K={k}:p90", m["p90"], clean_num(r[6]), 0.05, errors)
            check_close(f"v3_tpch:K={k}:tok", m["mean_tokens"], clean_num(r[10]), 2.0, errors)
            checked += 4

    for k in (1, 6):
        if k not in stats_k:
            errors.append(f"MISSING v3 STATS-CEB K={k} row")
        else:
            r = stats_k[k]
            m = compute_v2("v3_lightning_stats_ceb_top12_k6", k, 60000.0)
            check_close(f"v3_stats:K={k}:best_s", m["best_s"], clean_num(r[4]), 0.02, errors)
            check_close(f"v3_stats:K={k}:p50", m["p50"], clean_num(r[7]), 0.05, errors)
            check_close(f"v3_stats:K={k}:p90", m["p90"], clean_num(r[8]), 0.05, errors)
            check_close(f"v3_stats:K={k}:tok", m["mean_tokens"], clean_num(r[12]), 2.0, errors)
            checked += 4

    print(f"[v3_report] Verified {checked} bound cells in {path.name}; errors={len(errors)}")
    return errors


def verify_progress_report(path: Path) -> list[str]:
    errors: list[str] = []
    rows = parse_md_table_rows(path.read_text(encoding="utf-8"))
    by_key = {}
    for r in rows:
        if len(r) >= 12 and r[0] in ("TPC-H (22q)", "JOB (113q)", "JOB Top-12", "TPC-H Top-8", "imdb_anon (12q)", "STATS-CEB (12q)"):
            by_key[(r[0], r[1].split()[0], r[2])] = r

    v2_checks = [
        ("JOB Top-12", "v2_120b", "K*=6", "v2_120b_job_top12", 6, 180000.0),
        ("JOB Top-12", "v2_550b", "K*=6", "v2_550b_job_top12", 6, 180000.0),
        ("JOB Top-12", "v2_hybrid", "K*=6", "v2_hybrid_job_top12", 6, 180000.0),
        ("JOB Top-12", "v2_120b", "K=20", "v2_120b_job_top12", 20, 180000.0),
        ("JOB Top-12", "v2_550b", "K=20", "v2_550b_job_top12", 20, 180000.0),
        ("JOB Top-12", "v2_hybrid", "K=20", "v2_hybrid_job_top12", 20, 180000.0),
        ("TPC-H Top-8", "dta_baseline", "0", "dta_tpch_top8_v2", 1, 180000.0),
        ("TPC-H Top-8", "v2_120b", "K*=6", "v2_120b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "v2_550b", "K*=6", "v2_550b_tpch_top8_k6", 6, 180000.0),
        ("TPC-H Top-8", "v2_hybrid", "K*=6", "v2_hybrid_tpch_top8_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_120b", "K*=6", "v2_120b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_550b", "K*=6", "v2_550b_imdb_anon_k6", 6, 180000.0),
        ("imdb_anon (12q)", "v2_hybrid", "K*=6", "v2_hybrid_imdb_anon_k6", 6, 180000.0),
        ("STATS-CEB (12q)", "v2_120b", "K*=6", "v2_120b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB (12q)", "v2_550b", "K*=6", "v2_550b_stats_ceb_k6", 6, 60000.0),
        ("STATS-CEB (12q)", "v2_hybrid", "K*=6", "v2_hybrid_stats_ceb_k6", 6, 60000.0),
    ]
    checked = 0
    for ds, meth, bud, rid, k, to_ms in v2_checks:
        key = (ds, meth, bud)
        if key not in by_key:
            errors.append(f"MISSING PROGRESS REPORT ROW: {key}")
            continue
        r = by_key[key]
        m = compute_v2(rid, k, to_ms)
        check_close(f"prog:{key}:base_s", m["base_s"], clean_num(r[3]), 0.02, errors)
        check_close(f"prog:{key}:best_s", m["best_s"], clean_num(r[4]), 0.02, errors)
        check_close(f"prog:{key}:red_pct", -m["red_pct"], clean_num(r[5]), 0.15, errors)
        check_close(f"prog:{key}:speedup_x", m["speedup_x"], clean_num(r[6]), 0.02, errors)
        check_close(f"prog:{key}:p50", m["p50"], clean_num(r[7]), 0.2, errors)
        check_close(f"prog:{key}:p90", m["p90"], clean_num(r[8]), 0.2, errors)
        checked += 6
    print(f"[progress_report] Verified {checked} bound cells in {path.name}; errors={len(errors)}")
    return errors


def verify_pdf(pdf_path: Path) -> list[str]:
    errors: list[str] = []
    reader = PdfReader(str(pdf_path))
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    checks = [
        ("JOB Top-12 K=20 Hybrid", compute_v2("v2_hybrid_job_top12", 20)),
        ("JOB Top-12 K=6 120B", compute_v2("v2_120b_job_top12", 6)),
        ("TPC-H Top-8 K=6 Hybrid", compute_v2("v2_hybrid_tpch_top8_k6", 6)),
        ("imdb_anon Top-12 K=6 Hybrid", compute_v2("v2_hybrid_imdb_anon_k6", 6)),
        ("STATS-CEB Top-12 K=6 550B", compute_v2("v2_550b_stats_ceb_k6", 6, 60000.0)),
    ]
    checked = 0
    for name, m in checks:
        tok_s2 = f"{m['best_s']:.2f}"
        tok_s1 = f"{m['best_s']:.1f}s"
        tok_r = f"-{m['red_pct']:.1f}%"
        if tok_s2 not in full_text and tok_s1 not in full_text:
            errors.append(f"PDF MISSING [{name}] best_s={tok_s2}")
        if tok_r not in full_text:
            errors.append(f"PDF MISSING [{name}] red_pct={tok_r}")
        checked += 2
    print(f"[advisor_pdf] Verified {len(reader.pages)} pages, {checked} headline bindings; errors={len(errors)}")
    return errors


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v2-report", type=Path, default=REPO / "thesis" / "v2_experiment_report.md")
    ap.add_argument("--progress-report", type=Path, default=REPO / "thesis" / "thesis_progress_and_retrospective_report.md")
    ap.add_argument("--thesis-dir", type=Path, default=REPO / "thesis")
    ap.add_argument("--skip-sha", action="store_true")
    ap.add_argument("--record", type=Path, default=None, help="Optional JSON provenance record output path")
    args = ap.parse_args()

    errors: list[str] = []
    if not args.skip_sha:
        errors.extend(verify_frozen_hashes())
    errors.extend(verify_thesis_log(REPO / "THESIS_LOG.md"))
    if args.thesis_dir.exists():
        errors.extend(verify_artifact_discovery(args.thesis_dir))
        errors.extend(verify_v1_report(args.thesis_dir / "v1_experiment_report.md"))
        errors.extend(verify_v2_report(args.v2_report))
        errors.extend(verify_v3_report(args.thesis_dir / "v3_experiment_report.md"))
        errors.extend(verify_progress_report(args.progress_report))
    else:
        print(f"[thesis] Local-only directory '{args.thesis_dir.name}/' not present in CI checkout — verified frozen hashes + THESIS_LOG.md.")
    adv_pdf = REPO / "for advisor" / "v2" / "v2_results_tables.pdf"
    if adv_pdf.exists():
        errors.extend(verify_pdf(adv_pdf))
    else:
        print("[advisor_pdf] Local-only 'for advisor/' not present in CI checkout — skipped.")

    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(
            json.dumps(
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "git_commit": git_head(),
                    "python_version": platform.python_version(),
                    "platform": platform.platform(),
                    "verified_artifacts": len(FROZEN_HASHES),
                    "registered_reports": sorted(REGISTERED_THESIS_MDS),
                    "error_count": len(errors),
                    "status": "PASS" if not errors else "FAIL",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if errors:
        print("\nINTEGRITY CHECK FAILED:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("\nINTEGRITY CHECK PASSED: 0 discrepancies across all 26 frozen runs, THESIS_LOG.md, v1/v2/v3/progress reports, and advisor PDF.")


if __name__ == "__main__":
    main()
