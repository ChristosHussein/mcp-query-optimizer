"""LLMSteer baseline (Akioyamen et al., 2024): PCA + RBF-SVM over Gemini SQL query embeddings.

Reads per-arm latencies from a completed Bao Oracle run (arm_latencies.jsonl), selects the single
highest-impact alternative hint set over PostgreSQL default (Arm 0), embeds all JOB SQL queries
using Gemini 'gemini-embedding-001' (cached locally), and evaluates 10-fold stratified CV.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from google import genai
from sklearn.decomposition import PCA
from sklearn.metrics import accuracy_score
from sklearn.metrics import precision_score
from sklearn.metrics import recall_score
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

ROOT = Path(__file__).resolve().parents[2]


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (k - f) * (s[c] - s[f])


def load_or_compute_embeddings(query_ids: list[str], queries: list[str], model: str, cache_path: Path) -> np.ndarray:
    if cache_path.exists():
        data = np.load(cache_path, allow_pickle=True)
        cached_ids = list(data["query_ids"])
        if cached_ids == query_ids and str(data["model"]) == model:
            print(f"Loaded cached embeddings from {cache_path}")
            return np.asarray(data["embeddings"], dtype=np.float32)

    client = genai.Client()
    vecs: list[list[float]] = []
    batch_size = 20
    for i in range(0, len(queries), batch_size):
        batch = queries[i : i + batch_size]
        for attempt in range(5):
            try:
                resp = client.models.embed_content(model=model, contents=batch)
                break
            except Exception as exc:
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    wait_s = 30 * (attempt + 1)
                    print(f"  [429 rate limit on batch {i // batch_size + 1}] sleeping {wait_s}s...")
                    time.sleep(wait_s)
                else:
                    raise
        for emb in resp.embeddings:
            vecs.append(list(emb.values))

    arr = np.asarray(vecs, dtype=np.float32)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache_path, query_ids=np.asarray(query_ids), model=model, embeddings=arr)
    print(f"Computed and cached {arr.shape} embeddings to {cache_path}")
    return arr


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Missing GEMINI_API_KEY. Set it in .env before running LLMSteer embeddings.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--bao-arm-jsonl", required=True, help="Path to arm_latencies.jsonl from run_bao_oracle.py")
    parser.add_argument("--embed-model", default="gemini-embedding-001")
    parser.add_argument("--n-components", type=int, default=50, help="PCA components (capped to train fold size)")
    parser.add_argument("--folds", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    # Load per-arm latencies
    by_q: dict[str, dict[int, tuple[str, float]]] = defaultdict(dict)
    with Path(args.bao_arm_jsonl).open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            by_q[row["query_id"]][int(row["arm_index"])] = (row["arm_label"], float(row["execution_latency_ms"]))

    query_ids = sorted(by_q.keys())
    qdir = ROOT / "benchmarks" / "job" / "queries"
    sqls = [(qdir / f"{qid}.sql").read_text(encoding="utf-8") for qid in query_ids]

    # Pick single best alternative arm with highest total latency reduction if applied perfectly (LLMSteer Section 3)
    all_arm_ids = sorted({a for m in by_q.values() for a in m if a != 0})
    best_alt_arm = -1
    best_alt_label = ""
    best_potential_total = float("inf")
    for arm_id in all_arm_ids:
        pot_total = sum(min(by_q[q][0][1], by_q[q][arm_id][1]) for q in query_ids)
        if pot_total < best_potential_total:
            best_potential_total = pot_total
            best_alt_arm = arm_id
            best_alt_label = by_q[query_ids[0]][arm_id][0]

    pg_ms = np.array([by_q[q][0][1] for q in query_ids], dtype=np.float64)
    alt_ms = np.array([by_q[q][best_alt_arm][1] for q in query_ids], dtype=np.float64)
    y = (alt_ms < pg_ms).astype(int)

    print(f"Selected alternative arm {best_alt_arm} ({best_alt_label}): {int(y.sum())}/{len(y)} queries faster than default PG.")

    cache_path = ROOT / "results" / "cache" / f"job_embeddings_{args.embed_model}.npz"
    X = load_or_compute_embeddings(query_ids, sqls, args.embed_model, cache_path)

    skf = StratifiedKFold(n_splits=min(args.folds, int(min(y.sum(), (1 - y).sum()))), shuffle=True, random_state=args.seed)
    oof_pred = np.zeros(len(y), dtype=int)
    oof_score = np.zeros(len(y), dtype=np.float64)

    for train_idx, test_idx in skf.split(X, y):
        n_comp = min(args.n_components, len(train_idx) - 1, X.shape[1])
        clf = make_pipeline(
            StandardScaler(),
            PCA(n_components=n_comp, random_state=args.seed),
            SVC(kernel="rbf", C=1.0, class_weight="balanced", probability=True, random_state=args.seed),
        )
        clf.fit(X[train_idx], y[train_idx])
        oof_pred[test_idx] = clf.predict(X[test_idx])
        oof_score[test_idx] = clf.predict_proba(X[test_idx])[:, 1]

    steer_ms = np.where(oof_pred == 1, alt_ms, pg_ms)
    opt_binary_ms = np.minimum(pg_ms, alt_ms)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"llmsteer_job_{ts}"
    out_dir = ROOT / "results" / "baselines" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    commit = git_commit()

    with (out_dir / "queries.jsonl").open("w", encoding="utf-8") as fh:
        for i, qid in enumerate(query_ids):
            fh.write(json.dumps({
                "run_id": run_id,
                "git_commit": commit,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": "job",
                "query_id": qid,
                "method_id": "llmsteer_baseline",
                "round_index": 0,
                "proposed_action": best_alt_label if oof_pred[i] == 1 else "default",
                "action_valid": True,
                "execution_latency_ms": round(float(steer_ms[i]), 3),
                "pg_default_latency_ms": round(float(pg_ms[i]), 3),
                "alt_latency_ms": round(float(alt_ms[i]), 3),
                "true_label": int(y[i]),
                "pred_label": int(oof_pred[i]),
                "pred_prob": round(float(oof_score[i]), 4),
                "timed_out": False,
                "llm_calls": 1,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }) + "\n")

    improved = int(np.sum(steer_ms < pg_ms * 0.95))
    regressed = int(np.sum(steer_ms > pg_ms * 1.05))
    summary: dict[str, Any] = {
        "run_id": run_id,
        "git_commit": commit,
        "benchmark": "job",
        "method_id": "llmsteer_baseline",
        "embed_model": args.embed_model,
        "alt_arm_index": best_alt_arm,
        "alt_arm_label": best_alt_label,
        "query_count": len(query_ids),
        "pg_default_p50_ms": round(percentile(pg_ms.tolist(), 50), 3),
        "pg_default_p90_ms": round(percentile(pg_ms.tolist(), 90), 3),
        "pg_default_total_s": round(float(pg_ms.sum()) / 1000.0, 3),
        "llmsteer_p50_ms": round(percentile(steer_ms.tolist(), 50), 3),
        "llmsteer_p90_ms": round(percentile(steer_ms.tolist(), 90), 3),
        "llmsteer_total_s": round(float(steer_ms.sum()) / 1000.0, 3),
        "binary_optimal_total_s": round(float(opt_binary_ms.sum()) / 1000.0, 3),
        "improved_pct": round(100.0 * improved / len(query_ids), 2),
        "regression_pct": round(100.0 * regressed / len(query_ids), 2),
        "accuracy": round(float(accuracy_score(y, oof_pred)), 4),
        "precision": round(float(precision_score(y, oof_pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, oof_pred, zero_division=0)), 4),
        "auroc": round(float(roc_auc_score(y, oof_score)), 4),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\nSummary:", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
