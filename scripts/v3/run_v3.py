"""v3 Fixed-K* Async MCP Query Optimization Advisor (Definitive Thesis-Spec Runner).

Features:
  - NVIDIA NIM (nvidia/nemotron-3.5-lightning-30b-a3b) or Google Gemini via AsyncOpenAI
  - Strict asyncio.Lock around EVERY PostgreSQL EXPLAIN ANALYZE / Phase-2 real index build
  - Compact query-column-filtered on-demand get_table_stats MCP tool with per-query caching
  - Hardened MCP backend: Leading(((a b) c)) binary normalizer, relaxed warning gate, pglast AST indexes
  - Strict fixed K*=6 rounds (--all-queries runs all 113 JOB / all 22 TPC-H; STATS-CEB Top-12 default)
  - Dynamic exploration timeout cap + fail-fast daily-quota breaker + exact-action deduplication cache
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
WORKER_DEFAULT = "nvidia/nemotron-3.5-super-vl-120b-a12b"
FRONTIER_DEFAULT = "nvidia/nemotron-3-ultra-550b-a55b"
LIGHTNING_DEFAULT = "nvidia/nemotron-3.5-lightning-30b-a3b"
GEMINI_DEFAULT = "gemini-3.1-flash-lite"

JOB_TOP12 = ["16b", "19d", "17e", "17a", "17f", "31c", "25c", "10c", "17d", "17c", "17b", "31a"]
TPCH_TOP8 = ["q17", "q20", "q18", "q13", "q01", "q09", "q10", "q03"]
STATS_CEB_TOP12 = ["s126", "s122", "s120", "s058", "s135", "s068", "s108", "s030", "s107", "s034", "s143", "s070"]

HINTS_SYSTEM_PROMPT = """You are an expert PostgreSQL 16 query optimization advisor using pg_hint_plan.
Your goal is to reduce query execution latency by fixing bad join orders, bad join algorithms (e.g., NestedLoop on underestimated intermediates), or suboptimal scan types.

Rules:
1. Inspect the EXPLAIN ANALYZE plan carefully — pay special attention to [EST_ERR: ... UNDER/OVER] markers and high-loop Nested Loops.
2. ALWAYS use table ALIASES (shown as 'on table AS alias' in the plan), NEVER base table names when an alias exists.
3. Valid pg_hint_plan directives:
   - Scan hints: SeqScan(alias), IndexScan(alias), IndexOnlyScan(alias), BitmapScan(alias), NoSeqScan(alias), NoIndexScan(alias)
   - Join hints: HashJoin(a b ...), MergeJoin(a b ...), NestLoop(a b ...), NoNestLoop(a b ...), NoHashJoin(a b ...)
   - Join order: Leading(a b c) for join order only, or strictly binary nested Leading(((a b) c)) with double outer parens for directed join trees. Never combine positive and negative hints on the exact same target.
4. In Round 1, call get_table_stats first to inspect table sizes, existing indexes, n_distinct, and correlation before proposing hints; you may also call explain_with_hints(analyze=False) (at most 2 tool calls total per round). Do NOT use analyze=True.
5. NEVER repeat a hint set already listed in Previous Rounds History — always explore a genuinely different join order, join operator, or scan strategy.
6. End your turn with a single valid JSON object inside ```json ... ``` with exact keys:
   {"action_type": "hints", "hints": "/*+ ... */", "rationale": "brief 1-2 sentence diagnosis", "converged": false}"""

INDEX_SYSTEM_PROMPT = """You are an expert PostgreSQL 16 index advisor using HypoPG.
Your goal is to propose high-impact B-tree (single, composite, immutable expression, or partial) indexes to speed up analytical queries.

Rules:
1. Inspect the EXPLAIN ANALYZE plan for expensive Seq Scans, large Hash/Merge Joins, or Sorts on unindexed filter/join columns.
2. In Round 1, call get_table_stats first to inspect existing indexes and column stats (n_distinct, correlation), and test candidate indexes with explain_query(analyze=False, hypothetical_indexes=[...]) (at most 2 tool calls total per round) to confirm PostgreSQL uses them and lowers Total Cost.
3. Prefer plain columns first; immutable expressions (e.g. substring(c_phone FROM 1 FOR 2)) and optional "where" predicates are also AST-supported when needed. Propose at most 3 indexes per round.
4. NEVER repeat an index set already listed in Previous Rounds History — always explore a genuinely different column combination or covering index.
5. End your turn with a single valid JSON object inside ```json ... ``` with exact keys:
   {"action_type": "indexes", "indexes": [{"table": "lineitem", "columns": ["l_partkey", "l_quantity"], "using": "btree"}], "rationale": "brief 1-2 sentence diagnosis", "converged": false}"""


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def parse_execution_time_ms(text: str) -> float | None:
    m = re.search(r"Execution Time:\s*([0-9.]+)\s*ms", text)
    return float(m.group(1)) if m else None


def parse_root_cost(text: str) -> float | None:
    m = re.search(r"\(Cost:\s*[0-9.]+\.\.([0-9.]+)\)", text)
    return float(m.group(1)) if m else None


def _normalize_proposal(parsed: Any) -> dict[str, Any] | None:
    if not isinstance(parsed, dict):
        return None
    if "action_type" in parsed:
        return parsed
    if isinstance(parsed.get("hints"), str):
        parsed["action_type"] = "hints"
        return parsed
    if isinstance(parsed.get("indexes"), list):
        parsed["action_type"] = "indexes"
        return parsed
    return None


def extract_json_proposal(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    for raw in reversed(fenced):
        try:
            norm = _normalize_proposal(json.loads(raw))
            if norm is not None:
                return norm
        except json.JSONDecodeError:
            continue
    # Fallback: scan backwards for valid JSON objects without greedy .* spanning prose
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"\{", text))):
        try:
            parsed, _ = dec.raw_decode(text, m.start())
            norm = _normalize_proposal(parsed)
            if norm is not None:
                return norm
        except json.JSONDecodeError:
            continue
    return None


def extract_from_tables(sql: str) -> list[str]:
    """Extract all table names across FROM lists, explicit JOINs, CTEs, and subqueries."""
    tables: list[str] = []
    # 1) Explicit JOIN <table>
    for m in re.finditer(r"\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_.\"]*)", sql, flags=re.IGNORECASE):
        tok = m.group(1).strip('"').split(".")[-1]
        if tok and tok not in tables:
            tables.append(tok)
    # 2) All FROM ... blocks (comma-separated tables in main query or subqueries)
    for m in re.finditer(
        r"\bFROM\s+(.*?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|\bJOIN\b|\bLEFT\b|\bRIGHT\b|\bINNER\b|\bOUTER\b|\)|\Z)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        for part in m.group(1).split(","):
            tok = part.strip().split()[0].strip().strip('"').split(".")[-1] if part.strip() else ""
            if tok and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", tok) and tok.upper() not in ("SELECT", "LATERAL") and tok not in tables:
                tables.append(tok)
    return tables


class FatalQuotaError(RuntimeError):
    """Raised on unrecoverable daily/plan quota exhaustion so we abort immediately instead of logging empty rounds."""


async def call_nim_with_retry(client: AsyncOpenAI, req: dict[str, Any], max_retries: int = 7) -> Any:
    delay = 3.0
    for attempt in range(max_retries + 1):
        try:
            return await client.chat.completions.create(**req)
        except Exception as exc:
            msg = str(exc).lower()
            if any(k in msg for k in ("exceeded your current quota", "requestsperday", "perday")):
                raise FatalQuotaError(str(exc)) from exc
            retryable = any(
                k in msg
                for k in (
                    "429", "529", "overloaded", "rate limit", "resource_exhausted",
                    "500", "502", "503", "504", "deadline", "timed out", "timeout", "connection",
                )
            )
            if not retryable or attempt == max_retries:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 60.0)
    raise RuntimeError("Unreachable")


def compact_table_stats(raw_json: str, sql_idents: set[str]) -> str:
    """Filter get_table_stats JSON to columns referenced in the SQL query to prevent prompt bloat."""
    try:
        data = json.loads(raw_json)
    except Exception:
        return raw_json
    tables = data.get("tables")
    if not isinstance(tables, dict):
        return raw_json
    for tbl in tables.values():
        cols = tbl.get("columns")
        if isinstance(cols, list) and cols:
            kept = [c for c in cols if str(c.get("column", "")).lower() in sql_idents]
            if kept:
                tbl["columns"] = kept
    return json.dumps(data, separators=(",", ":"))


async def call_mcp(session: ClientSession, db_lock: asyncio.Lock, name: str, args: dict[str, Any], serialize: bool) -> str:
    if serialize:
        async with db_lock:
            res = await session.call_tool(name, arguments=args)
    else:
        res = await session.call_tool(name, arguments=args)
    return "\n".join(c.text for c in res.content if getattr(c, "type", None) == "text")


async def optimize_one_query(
    client: AsyncOpenAI,
    session: ClientSession,
    db_lock: asyncio.Lock,
    file_lock: asyncio.Lock,
    out_fh: Any,
    openai_tools: list[dict[str, Any]],
    qid: str,
    sql: str,
    args: argparse.Namespace,
    run_id: str,
    commit: str,
) -> None:
    is_hints = args.benchmark in ("job", "imdb_anon", "stats_ceb")
    system_prompt = HINTS_SYSTEM_PROMPT if is_hints else INDEX_SYSTEM_PROMPT
    schema_name = "public"

    # Round 0: Strictly serialized warm-up + baseline EXPLAIN ANALYZE
    base_plan = ""
    async with db_lock:
        for attempt in range(2):
            base_plan = ""
            for _ in range(2):
                res = await session.call_tool("explain_query", arguments={"sql": sql, "analyze": True})
                txt = "\n".join(c.text for c in res.content if getattr(c, "type", None) == "text")
                base_plan = txt
                if txt.startswith("Error:") or parse_execution_time_ms(txt) is None:
                    break
            low = base_plan.lower()
            if parse_execution_time_ms(base_plan) is not None or any(k in low for k in ("timeout", "canceling", "timed out")):
                break
            await asyncio.sleep(1.0)

    base_timed_out = False
    parsed_base_ms = parse_execution_time_ms(base_plan)
    if parsed_base_ms is None:
        low = base_plan.lower()
        if "timeout" in low or "canceling statement" in low or "timed out" in low:
            base_timed_out = True
            est_plan = await call_mcp(session, db_lock, "explain_query", {"sql": sql, "analyze": False}, serialize=False)
            base_plan = (
                f"NOTE: Baseline EXPLAIN ANALYZE timed out after {args.timeout_s}s; "
                f"showing estimated EXPLAIN plan below.\n{est_plan}"
            )
    base_ms = float(args.timeout_s * 1000) if base_timed_out else parsed_base_ms
    base_cost = parse_root_cost(base_plan)
    stats_block = ""

    records: list[dict[str, Any]] = [{
        "run_id": run_id,
        "git_commit": commit,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": args.benchmark,
        "query_id": qid,
        "method_id": args.method,
        "round_index": 0,
        "model_advisor": args.advisor_model if args.method.endswith(("_550b", "_hybrid")) else None,
        "model_worker": args.worker_model if args.method.endswith(("_120b", "_hybrid", "_gemini", "_lightning")) else None,
        "model_used_this_round": None,
        "proposed_action": None,
        "action_valid": True,
        "estimated_cost": base_cost,
        "execution_latency_ms": base_ms,
        "timed_out": base_timed_out or (base_ms is None),
        "tools_called": ["get_table_stats (prefetched)"] if stats_block else [],
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thought_tokens": 0,
        "llm_wall_time_ms": 0.0,
    }]

    if base_ms is None:
        async with file_lock:
            out_fh.write(json.dumps(records[0]) + "\n")
            out_fh.flush()
        print(f"  [ERR] {qid} baseline failed: {base_plan[:300]}")
        return

    if args.skip_fast_ms > 0 and base_ms <= args.skip_fast_ms:
        async with file_lock:
            out_fh.write(json.dumps(records[0]) + "\n")
            out_fh.flush()
        print(f"  [SKIP<=50ms] {qid:4s} | base={base_ms:8.1f} ms")
        return

    best_ms = base_ms
    best_action: Any = None
    last_plan: str | None = None
    history: list[str] = []
    action_cache: dict[str, dict[str, Any]] = {}
    stats_cache: dict[str, str] = {}
    sql_idents = {m.lower() for m in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", sql)}

    cum_calls = 0
    cum_in = 0
    cum_out = 0
    cum_th = 0
    cum_wall = 0.0

    for r in range(1, args.fixed_rounds + 1):
        # Model selection
        if args.method.endswith("_550b"):
            model = args.advisor_model
        elif args.method.endswith(("_120b", "_gemini", "_lightning")):
            model = args.worker_model
        else:
            # *_hybrid: Alternating round-robin (odd rounds = 120B Worker, even rounds = 550B Frontier)
            model = args.worker_model if (r % 2 == 1) else args.advisor_model

        hist_block = "\n".join(history) if history else "(none yet — Round 1)"
        last_block = f"\n### Last Evaluated Plan\n```\n{last_plan}\n```\n" if last_plan else ""
        prompt = (
            f"### Round {r}/{args.fixed_rounds}\n"
            f"Baseline latency: {base_ms:.2f} ms | Best latency so far: {best_ms:.2f} ms | Best action: {best_action}\n\n"
            f"### SQL Query\n```sql\n{sql.strip()}\n```\n\n"
            f"### Baseline EXPLAIN ANALYZE\n```\n{base_plan}\n```\n"
            f"{stats_block}{last_block}\n"
            f"### Previous Rounds History\n{hist_block}\n\n"
            "Propose a NEW optimization (not yet tried above) as a ```json ... ``` block."
        )

        for _rate_window in range(3):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            tool_calls_used = 0
            tools_called = []
            final_text = None
            api_err = None
            try:
                while True:
                    tools_allowed = tool_calls_used < 2
                    req: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": 0.2,
                    }
                    if args.provider == "nvidia" and args.reasoning_budget > 0:
                        req["extra_body"] = {
                            "chat_template_kwargs": {"enable_thinking": True},
                            "reasoning_budget": args.reasoning_budget,
                        }
                    if tools_allowed and openai_tools:
                        req["tools"] = openai_tools
                        req["tool_choice"] = "auto"

                    t0 = time.perf_counter()
                    resp = await call_nim_with_retry(client, req)
                    cum_wall += (time.perf_counter() - t0) * 1000.0
                    cum_calls += 1
                    if resp.usage:
                        cum_in += int(resp.usage.prompt_tokens or 0)
                        cum_out += int(resp.usage.completion_tokens or 0)
                        det = getattr(resp.usage, "completion_tokens_details", None)
                        if det and getattr(det, "reasoning_tokens", None):
                            cum_th += int(det.reasoning_tokens)

                    msg = resp.choices[0].message
                    if not msg.tool_calls or not tools_allowed:
                        final_text = msg.content or getattr(msg, "reasoning_content", None)
                        if extract_json_proposal(final_text) is None and not getattr(resp, "_json_nudged", False):
                            messages.append({"role": "assistant", "content": final_text or ""})
                            messages.append({
                                "role": "user",
                                "content": "Output ONLY a single valid ```json ... ``` proposal object with keys action_type, hints/indexes, rationale, converged.",
                            })
                            req2 = {"model": model, "messages": messages, "temperature": 0.2}
                            if args.provider == "nvidia" and args.reasoning_budget > 0:
                                req2["extra_body"] = {
                                    "chat_template_kwargs": {"enable_thinking": True},
                                    "reasoning_budget": args.reasoning_budget,
                                }
                            t1 = time.perf_counter()
                            resp2 = await call_nim_with_retry(client, req2)
                            cum_wall += (time.perf_counter() - t1) * 1000.0
                            cum_calls += 1
                            if resp2.usage:
                                cum_in += int(resp2.usage.prompt_tokens or 0)
                                cum_out += int(resp2.usage.completion_tokens or 0)
                            m2 = resp2.choices[0].message
                            final_text = m2.content or getattr(m2, "reasoning_content", None) or final_text
                        break

                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [tc.model_dump(exclude_none=True) for tc in msg.tool_calls],
                    })
                    for tc in msg.tool_calls:
                        tool_calls_used += 1
                        if tool_calls_used > 2:
                            tool_out = "Tool budget reached. Output your final ```json ... ``` proposal now."
                        else:
                            tools_called.append(tc.function.name)
                            try:
                                t_args = json.loads(tc.function.arguments or "{}")
                                if tc.function.name in ("explain_query", "explain_with_hints"):
                                    t_args["analyze"] = False
                                    t_args["sql"] = sql
                                if tc.function.name == "get_table_stats":
                                    ck = json.dumps(
                                        sorted(str(x).strip().lower() for x in (t_args.get("tables") or [])),
                                    )
                                    if ck in stats_cache:
                                        tool_out = stats_cache[ck]
                                    else:
                                        raw_stats = await call_mcp(session, db_lock, tc.function.name, t_args, serialize=False)
                                        tool_out = compact_table_stats(raw_stats, sql_idents)
                                        stats_cache[ck] = tool_out
                                else:
                                    tool_out = await call_mcp(session, db_lock, tc.function.name, t_args, serialize=False)
                            except Exception as exc:
                                tool_out = f"Tool error: {exc}"
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_out[:6000]})
                    if tool_calls_used >= 2:
                        messages.append({"role": "user", "content": "Tool budget exhausted. Output ONLY your final ```json ... ``` proposal now."})
                break
            except FatalQuotaError:
                raise
            except Exception as exc:
                api_err = f"LLM API error: {exc}"
                if "429" in str(exc) and _rate_window < 2:
                    await asyncio.sleep(65.0)
                    continue
                break

        if api_err:
            eval_out = {
                "proposed_action": None,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": api_err,
            }
            proposal = None
        else:
            proposal = extract_json_proposal(final_text)
            # Dynamic exploration timeout cap: abort bad join plans at 2.5x best seen (min 15s) instead of full 180s
            eff_timeout_s = min(args.timeout_s, max(15, math.ceil(best_ms * 2.5 / 1000.0))) if is_hints else args.timeout_s
            eval_out = await evaluate_proposal(
                session, db_lock, is_hints, sql, base_cost, proposal, eff_timeout_s, action_cache
            )

        if eval_out.get("plan_text"):
            last_plan = eval_out["plan_text"]

        exec_ms = eval_out["execution_latency_ms"]
        if eval_out["action_valid"] and exec_ms is not None and exec_ms < best_ms * 0.98:
            best_ms = exec_ms
            best_action = eval_out["proposed_action"]

        rec = {
            "run_id": run_id,
            "git_commit": commit,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "benchmark": args.benchmark,
            "query_id": qid,
            "method_id": args.method,
            "round_index": r,
            "model_advisor": args.advisor_model if args.method.endswith(("_550b", "_hybrid")) else None,
            "model_worker": args.worker_model if args.method.endswith(("_120b", "_hybrid", "_gemini", "_lightning")) else None,
            "model_used_this_round": f"{args.provider}:{model}",
            "proposed_action": eval_out["proposed_action"],
            "action_valid": eval_out["action_valid"],
            "estimated_cost": eval_out["estimated_cost"],
            "execution_latency_ms": exec_ms,
            "timed_out": eval_out["timed_out"],
            "feedback": eval_out["feedback"],
            "rationale": (proposal or {}).get("rationale"),
            "tools_called": tools_called,
            "llm_calls": cum_calls,
            "prompt_tokens": cum_in,
            "completion_tokens": cum_out,
            "thought_tokens": cum_th,
            "llm_wall_time_ms": round(cum_wall, 2),
        }
        records.append(rec)
        history.append(
            f"R{r} ({model.split('/')[-1]}): action={eval_out['proposed_action']} | valid={eval_out['action_valid']} "
            f"| ms={exec_ms} | note={str(eval_out['feedback'])[:140]}"
        )

    async with file_lock:
        for rec in records:
            out_fh.write(json.dumps(rec) + "\n")
        out_fh.flush()
    print(f"  [DONE] {qid:4s} | base={base_ms:8.1f} ms -> best={best_ms:8.1f} ms ({base_ms/max(best_ms, 1e-6):5.2f}x) | calls={cum_calls} tok={cum_in+cum_out:,}")


async def evaluate_proposal(
    session: ClientSession,
    db_lock: asyncio.Lock,
    is_hints: bool,
    sql: str,
    base_cost: float | None,
    proposal: dict[str, Any] | None,
    timeout_s: int,
    action_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not proposal:
        return {
            "proposed_action": None,
            "action_valid": False,
            "estimated_cost": None,
            "execution_latency_ms": None,
            "timed_out": False,
            "feedback": "Failed to parse JSON proposal.",
        }

    if is_hints:
        hints = proposal.get("hints")
        if not hints or not isinstance(hints, str):
            return {
                "proposed_action": None,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": "Missing 'hints' string.",
            }
        key = re.sub(r"\s+", " ", hints.strip())
        if key in action_cache:
            cached = dict(action_cache[key])
            cached["feedback"] = f"DUPLICATE (already tested earlier, cached ms={cached['execution_latency_ms']}); try a different hint strategy!"
            return cached

        async with db_lock:
            raw = ""
            for _ in range(2):
                res = await session.call_tool(
                    "explain_with_hints",
                    arguments={"sql": sql, "hints": hints, "analyze": True, "timeout_seconds": timeout_s},
                )
                raw = "\n".join(c.text for c in res.content if getattr(c, "type", None) == "text")
                if raw.startswith("Error:"):
                    break

        if raw.startswith("Error:"):
            out = {
                "proposed_action": hints,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": float(timeout_s * 1000) if "timeout" in raw.lower() else None,
                "timed_out": "timeout" in raw.lower() or "canceling statement" in raw.lower(),
                "feedback": raw,
            }
            action_cache[key] = out
            return out

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "proposed_action": hints,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": f"Non-JSON MCP response ({exc}): {raw[:200]}",
            }
        hr = data["hint_report"]
        out = {
            "proposed_action": data["hints"],
            "action_valid": bool(hr["used"]) and not hr["errors"],
            "estimated_cost": data["total_cost"],
            "execution_latency_ms": data["execution_time_ms"],
            "timed_out": False,
            "feedback": f"used={hr['used']} not_used={hr['not_used']} warnings={hr['warnings']}",
            "plan_text": data.get("plan_text"),
        }
        action_cache[key] = out
        return out

    # TPC-H 2-phase evaluation
    indexes = proposal.get("indexes")
    if not indexes or not isinstance(indexes, list):
        return {
            "proposed_action": indexes,
            "action_valid": False,
            "estimated_cost": None,
            "execution_latency_ms": None,
            "timed_out": False,
            "feedback": "Missing or empty 'indexes' list.",
        }
    key = json.dumps(indexes, sort_keys=True)
    if key in action_cache:
        cached = dict(action_cache[key])
        cached["feedback"] = f"DUPLICATE index set (cached ms={cached['execution_latency_ms']}); try a different column combination!"
        return cached

    hypo_raw = await call_mcp(
        session, db_lock, "explain_query", {"sql": sql, "analyze": False, "hypothetical_indexes": indexes}, serialize=False
    )
    if hypo_raw.startswith("Error:"):
        out = {
            "proposed_action": indexes,
            "action_valid": False,
            "estimated_cost": None,
            "execution_latency_ms": None,
            "timed_out": False,
            "feedback": f"Phase-1 HypoPG error: {hypo_raw}",
        }
        action_cache[key] = out
        return out

    hypo_cost = parse_root_cost(hypo_raw)
    if base_cost and hypo_cost is not None:
        red_pct = (base_cost - hypo_cost) / base_cost * 100.0
        if red_pct < 5.0:
            out = {
                "proposed_action": indexes,
                "action_valid": True,
                "estimated_cost": hypo_cost,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": f"Filtered in Phase 1 (HypoPG cost reduction {red_pct:.1f}% < 5%).",
                "plan_text": hypo_raw,
            }
            action_cache[key] = out
            return out

    async with db_lock:
        res = await session.call_tool(
            "evaluate_real_indexes",
            arguments={
                "sql": sql,
                "indexes": indexes,
                "warmup_runs": 1,
                "timed_runs": 1,
                "timeout_seconds": timeout_s,
            },
        )
        p2_raw = "\n".join(c.text for c in res.content if getattr(c, "type", None) == "text")

    if p2_raw.startswith("Error:"):
        out = {
            "proposed_action": indexes,
            "action_valid": False,
            "estimated_cost": hypo_cost,
            "execution_latency_ms": None,
            "timed_out": "timeout" in p2_raw.lower(),
            "feedback": f"Phase-2 eval error: {p2_raw}",
        }
        action_cache[key] = out
        return out

    try:
        p2 = json.loads(p2_raw)
    except json.JSONDecodeError as exc:
        return {
            "proposed_action": indexes,
            "action_valid": False,
            "estimated_cost": hypo_cost,
            "execution_latency_ms": None,
            "timed_out": False,
            "feedback": f"Non-JSON Phase-2 response ({exc}): {p2_raw[:200]}",
        }
    exec_ms = p2.get("median_execution_ms") if p2.get("median_execution_ms") is not None else p2.get("execution_time_ms")
    out = {
        "proposed_action": indexes,
        "action_valid": (not p2.get("timed_out", False)) and (exec_ms is not None),
        "estimated_cost": p2.get("total_cost", hypo_cost),
        "execution_latency_ms": exec_ms,
        "timed_out": bool(p2.get("timed_out", False)),
        "feedback": f"Phase-2 materialized in {p2.get('index_build_times_ms')} ms -> actual {exec_ms} ms",
        "plan_text": p2.get("plan_text"),
    }
    action_cache[key] = out
    return out


async def run_all(args: argparse.Namespace) -> None:
    load_dotenv(ROOT / ".env")
    if args.provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            sys.exit("Missing GEMINI_API_KEY in .env")
        base_url = GEMINI_OPENAI_BASE_URL
    else:
        api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not api_key:
            sys.exit("Missing NVIDIA_API_KEY in .env")
        base_url = NVIDIA_NIM_BASE_URL

    if args.benchmark == "job":
        qdir = ROOT / "benchmarks" / "job" / "queries"
        default_q = (
            sorted((p.stem for p in qdir.glob("*.sql")), key=lambda s: (int(re.match(r"(\d+)", s).group(1)), s))
            if args.all_queries
            else JOB_TOP12
        )
        dbname, eval_db = "imdb", None
    elif args.benchmark == "tpch":
        qdir = ROOT / "benchmarks" / "tpch" / "queries"
        default_q = sorted(p.stem for p in qdir.glob("*.sql")) if args.all_queries else TPCH_TOP8
        dbname, eval_db = "tpch", "tpch_eval"
    elif args.benchmark == "imdb_anon":
        qdir = ROOT / "benchmarks" / "imdb_anon" / "queries"
        default_q = JOB_TOP12
        dbname, eval_db = "imdb_anon", None
    else:
        qdir = ROOT / "benchmarks" / "stats_ceb" / "queries"
        default_q = sorted(p.stem for p in qdir.glob("*.sql")) if args.all_queries else STATS_CEB_TOP12
        dbname, eval_db = "stats_ceb", None

    qids = args.queries if args.queries else default_q
    queries = [(qid, (qdir / f"{qid}.sql").read_text(encoding="utf-8")) for qid in qids]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.run_id or f"{args.method}_{args.benchmark}_{ts}"
    ver_dir = "v3" if args.method.startswith("v3_") else "v2"
    out_jsonl = ROOT / "results" / ver_dir / run_id / "queries.jsonl"
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    done_qids: set[str] = set()
    if args.resume and out_jsonl.exists():
        with out_jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("round_index") == args.fixed_rounds:
                        done_qids.add(rec["query_id"])
        if done_qids:
            print(f"Resuming '{run_id}': skipping {len(done_qids)} completed queries.")

    pending = [(q, s) for q, s in queries if q not in done_qids]
    if not pending:
        print("All queries already completed.")
        return

    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    env = dict(os.environ)
    env["DATABASE_URI"] = f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"
    env["PG_MCP_TIMEOUT_SECONDS"] = str(args.timeout_s)
    if eval_db:
        env["EVAL_DATABASE_URI"] = f"postgresql://postgres:{pw}@{host}:{port}/{eval_db}"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys, postgres_mcp; sys.argv = ['postgres-mcp', '--access-mode', 'unrestricted']; postgres_mcp.main()"],
        env=env,
    )
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0, max_retries=0)
    commit = git_commit()

    t_start = time.perf_counter()
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            allowed = {"explain_with_hints", "get_table_stats"} if args.benchmark != "tpch" else {"explain_query", "get_table_stats"}
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or t.name,
                        "parameters": dict(t.inputSchema) if t.inputSchema else {"type": "object", "properties": {}},
                    },
                }
                for t in tools_resp.tools
                if t.name in allowed
            ]
            db_lock = asyncio.Lock()
            file_lock = asyncio.Lock()
            sem = asyncio.Semaphore(args.concurrency)
            mode = "a" if (args.resume and out_jsonl.exists()) else "w"

            with out_jsonl.open(mode, encoding="utf-8") as out_fh:
                async def _task(qid: str, sql: str) -> None:
                    async with sem:
                        try:
                            await optimize_one_query(
                                client, session, db_lock, file_lock, out_fh, openai_tools, qid, sql, args, run_id, commit
                            )
                        except FatalQuotaError as exc:
                            print(f"  [FATAL QUOTA] Aborting run on {qid}: {exc}")
                            raise
                        except Exception as exc:
                            print(f"  [ERR] Unhandled exception on {qid}: {exc}")

                await asyncio.gather(*[_task(q, s) for q, s in pending])

    print(f"\nFinished '{run_id}' ({len(pending)} queries x K={args.fixed_rounds}) in {(time.perf_counter()-t_start)/60:.1f} min -> {out_jsonl}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--benchmark", choices=["job", "tpch", "imdb_anon", "stats_ceb"], required=True)
    p.add_argument("--method", choices=["v2_120b", "v2_550b", "v2_hybrid", "v3_120b", "v3_550b", "v3_hybrid", "v3_gemini", "v3_lightning"], required=True)
    p.add_argument("--provider", choices=["nvidia", "gemini"], default="nvidia")
    p.add_argument("--worker-model", default=None)
    p.add_argument("--advisor-model", default=FRONTIER_DEFAULT)
    p.add_argument("--fixed-rounds", type=int, default=6)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--all-queries", action="store_true", help="Run all queries in the benchmark directory (e.g., all 113 JOB or 22 TPC-H)")
    p.add_argument("--skip-fast-ms", type=float, default=0.0, help="Optional Round-0 skip threshold in ms (e.g. 50.0)")
    p.add_argument("--reasoning-budget", type=int, default=0, help="0 = unbounded thinking (NVIDIA NIM only)")
    p.add_argument("--timeout-s", type=int, default=180)
    p.add_argument("--queries", nargs="*", help="Optional override list of query IDs")
    p.add_argument("--run-id", default=None)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    if args.method == "v3_gemini" and args.provider == "nvidia":
        args.provider = "gemini"
    if not args.worker_model:
        if args.provider == "gemini":
            args.worker_model = GEMINI_DEFAULT
        elif args.method == "v3_lightning":
            args.worker_model = LIGHTNING_DEFAULT
        else:
            args.worker_model = WORKER_DEFAULT

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
