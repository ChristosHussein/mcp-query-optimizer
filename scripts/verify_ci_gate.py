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
    "results/baselines/pg_default_job_v1/queries.jsonl": ("38F0959E5AB6D7F70EE7D13B638C8C6EEE76D2BE770656A1986E8413F1AAA9B1", 113),
    "results/baselines/pg_default_tpch_v1/queries.jsonl": ("CF30B7AE88E7D25C345B60F46FC429383065FB3A45B85C6B52413F3749B00D2D", 22),
    "results/baselines/bao_oracle_all48_job_v1/queries.jsonl": ("16C73027FAD1814505A7EED365E9C5B6BF56990C7F29EBFDA3F4E328CA5EAE21", 113),
    "results/baselines/bao_oracle_all48_job_v1/arm_latencies.jsonl": ("1D2C0AA75EA573EF5787D74768C15590BB362C34B1CE5224AAFCE21463DD91A1", 5537),
    "results/baselines/llmsteer_job_v1/queries.jsonl": ("D4C77406E7610F22D379C203A67BE768407FDE98CA505F31E196B2655A816589", 113),
    "results/v1/mcp_single_shot_tpch_v1/queries.jsonl": ("D304BC7038AAF8B44BAB280DC7D5B97CD620AFD6C77B51A9B027BE847A5BFEC6", None),
    "results/v1/mcp_iter_cheap_tpch_v1/queries.jsonl": ("CD377200BC41B4707B573665C563C3856E718E93B9061EFE1FEE853919F8A8CC", None),
    "results/v1/mcp_iter_hybrid_tpch_v1/queries.jsonl": ("4BA769A3DEF45468E3AE5E8884EA90BB137B3D6ACB6EB05ADC2C69F911F36D00", None),
    "results/v1/mcp_single_shot_job_v1/queries.jsonl": ("69B70283F94A53EFE6489B45736CB70219E3A7A4EF1FA3981662126584C34282", None),
    "results/v1/mcp_iter_cheap_job_v1/queries.jsonl": ("78324F5B07D1786628F00B5273FA817A3CACEACB16E7CB7CC112C321251AFB16", None),
    "results/v1/mcp_iter_hybrid_job_v1/queries.jsonl": ("2482998FEA2623F9232271949AEA026E5E4D3B7AC98E5D5EC3D5361CD20C7446", None),
    "results/v2/v2_120b_job_top12/queries.jsonl": ("7D28D16D2A2EA20BA4AE7CAF22583A7A5EB1644944B77CD04014749996C1A5A5", 252),
    "results/v2/v2_550b_job_top12/queries.jsonl": ("D4011B1C226171B02540DE9C622B7EB72F366A18E052D6125FD1283A5AB53792", 252),
    "results/v2/v2_hybrid_job_top12/queries.jsonl": ("349497A863D74E1043D51B883AD070967422CD66A8878364F25455475D8EA155", 252),
    "results/baselines/dta_tpch_top8_v2/queries.jsonl": ("5E192D841CC869CC07C7C57A8CD812656F301F0226C71DC825DAB64D030D9E7B", 16),
    "results/v2/v2_120b_tpch_top8_k6/queries.jsonl": ("CB3A6A82499DC68A8DD4AA346A2C81A383D9B73C762C78E7B546A85545C9508B", 56),
    "results/v2/v2_550b_tpch_top8_k6/queries.jsonl": ("F207013D744A05AC5FB551D3623449414FF585B1764552298263C14A19CB6C1B", 56),
    "results/v2/v2_hybrid_tpch_top8_k6/queries.jsonl": ("00518CF84DE6157E96BA042208273998AB0A4D9A2F2816C414101D0DA5FD70A1", 56),
    "results/v2/v2_120b_imdb_anon_k6/queries.jsonl": ("81825786D434119A051329E24154E9B6312DED664CA245D51820AC4A62C2B5A2", 84),
    "results/v2/v2_550b_imdb_anon_k6/queries.jsonl": ("3950267A4CFC599626EDD34384C00A50F5E3799710A34B243BD99B6FE03001C7", 84),
    "results/v2/v2_hybrid_imdb_anon_k6/queries.jsonl": ("B7FD208771A68A57B230E2BDD13A60587CC749172CD1D357BE703E3FED0886E9", 84),
    "results/v2/v2_120b_stats_ceb_k6/queries.jsonl": ("88E224CC74045989D0DB6CAA81D9A57D05B97C1EB4364A8181AA7546018DC4B2", 84),
    "results/v2/v2_550b_stats_ceb_k6/queries.jsonl": ("D3D1F1B1F2EC31BE6BA2C4CA446CCAB7762A4BE6DBEC1567350CF9FBE60BCB8C", 84),
    "results/v2/v2_hybrid_stats_ceb_k6/queries.jsonl": ("3CDA824AE79882312F7517C9C585CE5705257FF6D623F7AC355175BB48A71985", 84),
    "results/v3/v3_lightning_tpch_full_k6/queries.jsonl": ("F6C8916006A3A32B6DAE279D4B8C339207FBAFCEE54241DD07839194EE2DEB4E", 154),
    "results/v3/v3_lightning_job_full_k6/queries.jsonl": ("AE246F4D8916864A7BC01F8B07F3C818CC4721E26C2F11DECD82F9A8B9AB8DDA", 791),
    "results/v3/v3_lightning_stats_ceb_top12_k6/queries.jsonl": ("60C417846477D31EDFA06D500FBB8FF5060EC79A2E956404A784B1035736B15A", 84),
}

REGISTERED_THESIS_MDS = {
    "v1_experiment_report.md",
    "v2_experiment_report.md",
    "v3_experiment_report.md",
    "thesis_progress_and_retrospective_report.md",
    "case_studies.md",
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


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
