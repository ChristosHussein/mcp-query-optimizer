"""MCP-driven Agentic Query Optimization Advisory Loop using Gemini Interactions API."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from google import genai
from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

ROOT = Path(__file__).resolve().parent.parent.parent

HINTS_TOOLS = {"explain_query", "explain_with_hints", "get_table_stats", "get_object_details"}
INDEX_TOOLS = {"explain_query", "get_table_stats", "get_object_details"}

HINTS_SYSTEM_PROMPT = """You are an expert PostgreSQL 16 query optimization advisor using pg_hint_plan.
Your goal is to reduce query execution latency by fixing bad join orders, bad join algorithms (e.g., NestedLoop on underestimated intermediates), or suboptimal scan types.

Rules:
1. Always inspect the EXPLAIN ANALYZE plan carefully — pay special attention to [EST_ERR: ... UNDER/OVER] markers and high-loop Nested Loops.
2. ALWAYS use table ALIASES (shown as 'on table AS alias' in the plan), NEVER base table names when an alias exists.
3. Valid pg_hint_plan directives include:
   - Scan hints: SeqScan(alias), IndexScan(alias), IndexOnlyScan(alias), BitmapScan(alias), NoSeqScan(alias), NoIndexScan(alias)
   - Join hints: HashJoin(a b ...), MergeJoin(a b ...), NestLoop(a b ...), NoNestLoop(a b ...), NoHashJoin(a b ...)
   - Join order: Leading((a b) c) or Leading(a b c)
4. You may call get_table_stats or explain_with_hints(analyze=False) at most 2 times to inspect statistics or check estimated plans/hint validity cheaply. Do NOT call explain_with_hints with analyze=True — the evaluator will execute and time your final JSON proposal for this round.
5. Do NOT repeat a hint set that already failed or regressed in a previous round.
6. Keep reasoning concise and end your turn with a single valid JSON object inside ```json ... ``` with exact keys:
   {
     "action_type": "hints",
     "hints": "/*+ ... */",
     "rationale": "brief 1-2 sentence diagnosis",
     "converged": false
   }
   Set "converged": true if you believe the current best plan cannot be meaningfully improved further."""

INDEX_SYSTEM_PROMPT = """You are an expert PostgreSQL 16 index advisor using HypoPG.
Your goal is to propose high-impact B-tree (single or composite) indexes to speed up analytical TPC-H queries.

Rules:
1. Inspect the EXPLAIN ANALYZE plan for expensive Seq Scans, large Hash/Merge Joins, or Sorts on unindexed filter/join columns.
2. You MUST test candidate indexes with explain_query(analyze=False, hypothetical_indexes=[...]) first to confirm PostgreSQL actually uses the index and reduces estimated Total Cost.
3. Prefer concise, high-impact composite indexes (filter equality columns first, then join/range/order-by columns). Propose at most 3 indexes per round.
4. Do NOT propose indexes that already exist (check get_table_stats).
5. End your turn with a single valid JSON object inside ```json ... ``` with exact keys:
   {
     "action_type": "indexes",
     "indexes": [{"table": "lineitem", "columns": ["l_shipdate", "l_discount"], "using": "btree"}],
     "rationale": "brief 1-2 sentence diagnosis",
     "converged": false
   }
   Set "converged": true if you believe no further index will meaningfully improve latency."""


@dataclass
class AdvisorConfig:
    benchmark: str  # "job" | "tpch"
    method_id: str  # "mcp_single_shot" | "mcp_iter_cheap" | "mcp_iter_hybrid" | "mcp_iter_frontier"
    worker_model: str = "nvidia:nvidia/nemotron-3.5-super-vl-120b-a12b"
    advisor_model: str = "nvidia:nvidia/nemotron-3-ultra-550b-a55b"
    max_rounds: int = 5
    escalate_after_round: int = 2
    escalation_min_gain_pct: float = 25.0
    max_tool_calls_per_round: int = 2
    early_stop_latency_ms: float = 50.0
    no_improve_patience: int = 2
    statement_timeout_s: int = 180
    hypopg_min_cost_reduction_pct: float = 5.0
    eval_warmup_runs: int = 1
    eval_timed_runs: int = 1


def _extract_json_proposal(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = fenced if fenced else re.findall(r"(\{.*\})", text, flags=re.DOTALL)
    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "action_type" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _mcp_tools_to_gemini(mcp_tools: list[Any], allowed: set[str]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for t in mcp_tools:
        if t.name not in allowed:
            continue
        schema = dict(t.inputSchema) if t.inputSchema else {"type": "object", "properties": {}}
        declarations.append({
            "type": "function",
            "name": t.name,
            "description": t.description or t.name,
            "parameters": schema,
        })
    return declarations


def _mcp_tools_to_openai(mcp_tools: list[Any], allowed: set[str]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for t in mcp_tools:
        if t.name not in allowed:
            continue
        schema = dict(t.inputSchema) if t.inputSchema else {"type": "object", "properties": {}}
        declarations.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or t.name,
                "parameters": schema,
            },
        })
    return declarations


def _call_with_retry(fn: Any, max_retries: int = 4) -> Any:
    """Retry transient rate-limit (429) or 5xx API errors with exponential backoff."""
    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            msg = str(exc).lower()
            retryable = any(
                k in msg
                for k in (
                    "429",
                    "529",
                    "overloaded",
                    "rate limit",
                    "resource_exhausted",
                    "503",
                    "502",
                    "temporarily unavailable",
                    "timed out",
                    "timeout",
                    "connection",
                )
            )
            if not retryable or attempt == max_retries:
                raise
            time.sleep(delay)
            delay *= 2.0


def _usage_counts(interaction: Any) -> tuple[int, int, int]:
    u = getattr(interaction, "usage", None)
    if not u:
        return 0, 0, 0
    in_tok = int(getattr(u, "total_input_tokens", 0) or 0)
    out_tok = int(getattr(u, "total_output_tokens", 0) or 0)
    thought_tok = int(getattr(u, "total_thought_tokens", 0) or 0)
    return in_tok, out_tok, thought_tok


class QueryAdvisorRunner:
    """Runs the MCP-driven advisory loop for queries using a live postgres-mcp server over stdio."""

    def __init__(self, config: AdvisorConfig, database_uri: str, eval_database_uri: str | None = None):
        self.config = config
        self.database_uri = database_uri
        self.eval_database_uri = eval_database_uri
        self._genai_client: genai.Client | None = None
        self._nvidia_client: OpenAI | None = None

    def _get_genai(self) -> genai.Client:
        if self._genai_client is None:
            self._genai_client = genai.Client()
        return self._genai_client

    def _get_nvidia(self) -> OpenAI:
        if self._nvidia_client is None:
            api_key = os.environ.get("NVIDIA_API_KEY")
            if not api_key:
                raise RuntimeError("Missing NVIDIA_API_KEY environment variable for 'nvidia:*' model.")
            self._nvidia_client = OpenAI(base_url=NVIDIA_NIM_BASE_URL, api_key=api_key, timeout=120.0, max_retries=0)
        return self._nvidia_client

    def _select_model(self, round_idx: int, baseline_ms: float, best_ms: float) -> str:
        if self.config.method_id == "mcp_iter_frontier":
            return self.config.advisor_model
        if self.config.method_id in ("mcp_single_shot", "mcp_iter_cheap"):
            return self.config.worker_model
        # mcp_iter_hybrid: Cheap Worker for initial rounds, escalate to Frontier Advisor if insufficient gain
        if round_idx <= self.config.escalate_after_round:
            return self.config.worker_model
        gain_pct = ((baseline_ms - best_ms) / baseline_ms * 100.0) if baseline_ms > 0 else 0.0
        if gain_pct < self.config.escalation_min_gain_pct and best_ms > self.config.early_stop_latency_ms:
            return self.config.advisor_model
        return self.config.worker_model

    async def _call_mcp(self, session: ClientSession, name: str, arguments: dict[str, Any]) -> str:
        res = await session.call_tool(name, arguments=arguments)
        parts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
        return "\n".join(parts)

    async def _run_llm_round(
        self,
        session: ClientSession,
        model: str,
        system_prompt: str,
        prompt: str,
        gemini_tools: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
    ) -> tuple[str | None, int, int, int, int, float]:
        """Run one advisory round against either Gemini Interactions API or NVIDIA NIM (OpenAI-compatible)."""
        calls = 0
        in_tok = 0
        out_tok = 0
        thought_tok = 0
        wall_ms = 0.0
        tool_calls_used = 0

        if model.startswith("nvidia:"):
            nim_model = model.removeprefix("nvidia:")
            client = self._get_nvidia()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            while True:
                tools_allowed = tool_calls_used < self.config.max_tool_calls_per_round
                t0 = time.perf_counter()
                req_kwargs: dict[str, Any] = {
                    "model": nim_model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                if tools_allowed and openai_tools:
                    req_kwargs["tools"] = openai_tools
                    req_kwargs["tool_choice"] = "auto"
                resp = _call_with_retry(lambda: client.chat.completions.create(**req_kwargs))
                wall_ms += (time.perf_counter() - t0) * 1000.0
                calls += 1
                if resp.usage:
                    in_tok += int(resp.usage.prompt_tokens or 0)
                    out_tok += int(resp.usage.completion_tokens or 0)
                    details = getattr(resp.usage, "completion_tokens_details", None)
                    if details and getattr(details, "reasoning_tokens", None):
                        thought_tok += int(details.reasoning_tokens)

                msg = resp.choices[0].message
                if not msg.tool_calls or not tools_allowed:
                    text = msg.content or getattr(msg, "reasoning_content", None)
                    return text, calls, in_tok, out_tok, thought_tok, wall_ms

                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump(exclude_none=True) for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    tool_calls_used += 1
                    if tool_calls_used > self.config.max_tool_calls_per_round:
                        tool_out = "Tool call budget reached. Output your final ```json ... ``` proposal now."
                    else:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            # Prevent expensive analyze=True inside exploration tool calls
                            if tc.function.name in ("explain_query", "explain_with_hints") and args.get("analyze"):
                                args["analyze"] = False
                            tool_out = await self._call_mcp(session, tc.function.name, args)
                        except Exception as exc:
                            tool_out = f"Tool error: {exc}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_out[:8000],
                    })
                if tool_calls_used >= self.config.max_tool_calls_per_round:
                    messages.append({
                        "role": "user",
                        "content": "Tool budget for this round is exhausted. Output ONLY your final ```json ... ``` proposal now.",
                    })

        # Default: Gemini Interactions API
        client = self._get_genai()
        user_input: Any = prompt
        prev_id: str | None = None
        while True:
            tools_allowed = tool_calls_used < self.config.max_tool_calls_per_round
            t0 = time.perf_counter()
            g_kwargs: dict[str, Any] = {
                "model": model,
                "input": user_input,
                "system_instruction": system_prompt,
                "previous_interaction_id": prev_id,
            }
            if tools_allowed and gemini_tools:
                g_kwargs["tools"] = gemini_tools
            interaction = _call_with_retry(lambda: client.interactions.create(**g_kwargs))
            wall_ms += (time.perf_counter() - t0) * 1000.0
            calls += 1
            i_t, o_t, th_t = _usage_counts(interaction)
            in_tok += i_t
            out_tok += o_t
            thought_tok += th_t

            fn_results: list[dict[str, Any]] = []
            for step in interaction.steps or []:
                if step.type == "function_call":
                    tool_calls_used += 1
                    if tool_calls_used > self.config.max_tool_calls_per_round:
                        tool_out = "Tool budget reached. Output your final ```json ... ``` proposal now."
                    else:
                        try:
                            args = dict(step.arguments or {})
                            if step.name in ("explain_query", "explain_with_hints") and args.get("analyze"):
                                args["analyze"] = False
                            tool_out = await self._call_mcp(session, step.name, args)
                        except Exception as exc:
                            tool_out = f"Tool error: {exc}"
                    fn_results.append({
                        "type": "function_result",
                        "name": step.name,
                        "call_id": step.id,
                        "result": [{"type": "text", "text": tool_out[:8000]}],
                    })

            if not fn_results or not tools_allowed:
                return interaction.output_text, calls, in_tok, out_tok, thought_tok, wall_ms

            user_input = fn_results
            prev_id = interaction.id

    async def optimize_query(
        self,
        session: ClientSession,
        gemini_tools: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
        query_id: str,
        sql: str,
        run_id: str,
        git_commit: str,
    ) -> list[dict[str, Any]]:
        """Run baseline + up to max_rounds advisory iterations and return per-round log records."""
        records: list[dict[str, Any]] = []
        is_hints = self.config.benchmark == "job"
        system_prompt = HINTS_SYSTEM_PROMPT if is_hints else INDEX_SYSTEM_PROMPT

        # Round 0: Warm-up + measure default PostgreSQL EXPLAIN ANALYZE via MCP (avoids cold-cache bias)
        for _ in range(max(0, self.config.eval_warmup_runs)):
            warm_out = await self._call_mcp(session, "explain_query", {"sql": sql, "analyze": True})
            if warm_out.startswith("Error:"):
                break
        base_plan_text = await self._call_mcp(session, "explain_query", {"sql": sql, "analyze": True})
        if base_plan_text.startswith("Error:"):
            print(f"  [WARN] Baseline EXPLAIN ANALYZE error on {query_id}: {base_plan_text[:300]}")
        base_ms = _parse_execution_time_ms(base_plan_text)
        base_cost = _parse_root_cost(base_plan_text)

        records.append({
            "run_id": run_id,
            "git_commit": git_commit,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "benchmark": self.config.benchmark,
            "query_id": query_id,
            "method_id": self.config.method_id,
            "round_index": 0,
            "model_advisor": None,
            "model_worker": None,
            "model_used_this_round": None,
            "proposed_action": None,
            "action_valid": True,
            "estimated_cost": base_cost,
            "execution_latency_ms": base_ms,
            "timed_out": base_ms is None,
            "llm_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thought_tokens": 0,
            "llm_wall_time_ms": 0.0,
        })

        if base_ms is None or base_ms <= self.config.early_stop_latency_ms:
            return records

        best_ms = base_ms
        best_action: Any = None
        last_plan_text: str | None = None
        no_improve_streak = 0
        history_lines: list[str] = []

        cum_llm_calls = 0
        cum_in_tok = 0
        cum_out_tok = 0
        cum_thought_tok = 0
        cum_llm_wall_ms = 0.0

        max_rounds = 1 if self.config.method_id == "mcp_single_shot" else self.config.max_rounds

        for r in range(1, max_rounds + 1):
            model = self._select_model(r, base_ms, best_ms)
            prompt = self._build_round_prompt(
                sql, base_plan_text, last_plan_text, base_ms, best_ms, best_action, history_lines, r, max_rounds
            )

            try:
                final_text, r_calls, r_in, r_out, r_th, r_wall = await self._run_llm_round(
                    session, model, system_prompt, prompt, gemini_tools, openai_tools
                )
            except Exception as exc:
                print(f"  [WARN] LLM API error on {query_id} round {r} ({model}): {exc}")
                records.append({
                    "run_id": run_id,
                    "git_commit": git_commit,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "benchmark": self.config.benchmark,
                    "query_id": query_id,
                    "method_id": self.config.method_id,
                    "round_index": r,
                    "model_advisor": self.config.advisor_model if self.config.method_id in ("mcp_iter_hybrid", "mcp_iter_frontier") else None,
                    "model_worker": self.config.worker_model,
                    "model_used_this_round": model,
                    "proposed_action": None,
                    "action_valid": False,
                    "estimated_cost": None,
                    "execution_latency_ms": None,
                    "timed_out": False,
                    "feedback": f"LLM API error: {exc}",
                    "rationale": None,
                    "llm_calls": cum_llm_calls,
                    "prompt_tokens": cum_in_tok,
                    "completion_tokens": cum_out_tok,
                    "thought_tokens": cum_thought_tok,
                    "llm_wall_time_ms": round(cum_llm_wall_ms, 2),
                })
                no_improve_streak += 1
                if no_improve_streak >= self.config.no_improve_patience:
                    break
                continue
            cum_llm_calls += r_calls
            cum_in_tok += r_in
            cum_out_tok += r_out
            cum_thought_tok += r_th
            cum_llm_wall_ms += r_wall

            proposal = _extract_json_proposal(final_text)
            eval_out = await self._evaluate_proposal(session, sql, base_cost, proposal)
            if eval_out.get("plan_text"):
                last_plan_text = eval_out["plan_text"]

            exec_ms = eval_out["execution_latency_ms"]
            improved = (
                eval_out["action_valid"]
                and exec_ms is not None
                and exec_ms < best_ms * 0.98
            )
            if improved:
                best_ms = exec_ms
                best_action = eval_out["proposed_action"]
                no_improve_streak = 0
            else:
                no_improve_streak += 1

            records.append({
                "run_id": run_id,
                "git_commit": git_commit,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": self.config.benchmark,
                "query_id": query_id,
                "method_id": self.config.method_id,
                "round_index": r,
                "model_advisor": self.config.advisor_model if self.config.method_id in ("mcp_iter_hybrid", "mcp_iter_frontier") else None,
                "model_worker": self.config.worker_model,
                "model_used_this_round": model,
                "proposed_action": eval_out["proposed_action"],
                "action_valid": eval_out["action_valid"],
                "estimated_cost": eval_out["estimated_cost"],
                "execution_latency_ms": exec_ms,
                "timed_out": eval_out["timed_out"],
                "feedback": eval_out["feedback"],
                "rationale": (proposal or {}).get("rationale"),
                "llm_calls": cum_llm_calls,
                "prompt_tokens": cum_in_tok,
                "completion_tokens": cum_out_tok,
                "thought_tokens": cum_thought_tok,
                "llm_wall_time_ms": round(cum_llm_wall_ms, 2),
            })

            history_lines.append(
                f"Round {r} ({model}): action={eval_out['proposed_action']} | valid={eval_out['action_valid']} "
                f"| est_cost={eval_out['estimated_cost']} | actual_ms={exec_ms} | note={eval_out['feedback']}"
            )

            if (proposal and proposal.get("converged")) or best_ms <= self.config.early_stop_latency_ms:
                break
            if no_improve_streak >= self.config.no_improve_patience:
                break

        return records

    def _build_round_prompt(
        self,
        sql: str,
        base_plan: str,
        last_plan: str | None,
        base_ms: float,
        best_ms: float,
        best_action: Any,
        history: list[str],
        round_idx: int,
        max_rounds: int,
    ) -> str:
        hist_block = "\n".join(history) if history else "(none yet — this is Round 1)"
        last_block = f"\n### Last Evaluated Plan\n```\n{last_plan}\n```\n" if last_plan else ""
        return (
            f"### Round {round_idx}/{max_rounds}\n"
            f"Baseline latency: {base_ms:.2f} ms | Best latency so far: {best_ms:.2f} ms | Best action: {best_action}\n\n"
            f"### SQL Query\n```sql\n{sql.strip()}\n```\n\n"
            f"### Baseline EXPLAIN ANALYZE\n```\n{base_plan}\n```\n"
            f"{last_block}\n"
            f"### Previous Rounds History\n{hist_block}\n\n"
            "Analyze the bottlenecks, optionally inspect pg_stats or test plans via MCP tools, "
            "and output your next optimization proposal as a ```json ... ``` block."
        )

    async def _evaluate_proposal(
        self,
        session: ClientSession,
        sql: str,
        base_cost: float | None,
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not proposal:
            return {
                "proposed_action": None,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": "Failed to parse JSON proposal from LLM output.",
            }

        if self.config.benchmark == "job":
            hints = proposal.get("hints")
            if not hints or not isinstance(hints, str):
                return {
                    "proposed_action": None,
                    "action_valid": False,
                    "estimated_cost": None,
                    "execution_latency_ms": None,
                    "timed_out": False,
                    "feedback": "Missing 'hints' string in JSON proposal.",
                }
            # Warm-up + timed measurement for fair warm-cache comparison against Round 0
            raw = ""
            for _ in range(max(0, self.config.eval_warmup_runs) + 1):
                raw = await self._call_mcp(
                    session,
                    "explain_with_hints",
                    {"sql": sql, "hints": hints, "analyze": True, "timeout_seconds": self.config.statement_timeout_s},
                )
                if raw.startswith("Error:"):
                    break
            if raw.startswith("Error:"):
                return {
                    "proposed_action": hints,
                    "action_valid": False,
                    "estimated_cost": None,
                    "execution_latency_ms": float(self.config.statement_timeout_s * 1000) if "timeout" in raw.lower() else None,
                    "timed_out": "timeout" in raw.lower() or "canceling statement" in raw.lower(),
                    "feedback": raw,
                }
            data = json.loads(raw)
            hr = data["hint_report"]
            fb = f"used={hr['used']} not_used={hr['not_used']} warnings={hr['warnings']}"
            return {
                "proposed_action": data["hints"],
                "action_valid": bool(hr["used"]) and not hr["errors"] and not hr["warnings"],
                "estimated_cost": data["total_cost"],
                "execution_latency_ms": data["execution_time_ms"],
                "timed_out": False,
                "feedback": fb,
                "plan_text": data.get("plan_text"),
            }

        # TPC-H 2-phase evaluation: Phase 1 HypoPG EXPLAIN -> Phase 2 evaluate_real_indexes on *_eval clone
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

        hypo_raw = await self._call_mcp(
            session,
            "explain_query",
            {"sql": sql, "analyze": False, "hypothetical_indexes": indexes},
        )
        if hypo_raw.startswith("Error:"):
            return {
                "proposed_action": indexes,
                "action_valid": False,
                "estimated_cost": None,
                "execution_latency_ms": None,
                "timed_out": False,
                "feedback": f"Phase-1 HypoPG error: {hypo_raw}",
            }

        hypo_cost = _parse_root_cost(hypo_raw)
        if base_cost and hypo_cost is not None:
            reduction_pct = (base_cost - hypo_cost) / base_cost * 100.0
            if reduction_pct < self.config.hypopg_min_cost_reduction_pct:
                return {
                    "proposed_action": indexes,
                    "action_valid": True,
                    "estimated_cost": hypo_cost,
                    "execution_latency_ms": None,
                    "timed_out": False,
                    "feedback": f"Filtered in Phase-1 HypoPG: cost reduction only {reduction_pct:.1f}% (< {self.config.hypopg_min_cost_reduction_pct}%).",
                }

        # Passed Phase 1 -> materialize & measure on isolated *_eval clone
        p2_raw = await self._call_mcp(
            session,
            "evaluate_real_indexes",
            {
                "sql": sql,
                "indexes": indexes,
                "warmup_runs": self.config.eval_warmup_runs,
                "timed_runs": self.config.eval_timed_runs,
                "timeout_seconds": self.config.statement_timeout_s,
            },
        )
        if p2_raw.startswith("Error:"):
            return {
                "proposed_action": indexes,
                "action_valid": False,
                "estimated_cost": hypo_cost,
                "execution_latency_ms": float(self.config.statement_timeout_s * 1000) if "canceling statement" in p2_raw.lower() else None,
                "timed_out": "canceling statement" in p2_raw.lower(),
                "feedback": f"Phase-2 eval error: {p2_raw}",
            }
        p2 = json.loads(p2_raw)
        return {
            "proposed_action": indexes,
            "action_valid": True,
            "estimated_cost": p2["total_cost"],
            "execution_latency_ms": p2["median_execution_ms"],
            "timed_out": False,
            "feedback": f"Phase-2 materialized in {p2['index_build_times_ms']} ms -> actual {p2['median_execution_ms']} ms",
            "plan_text": p2.get("plan_text"),
        }

    async def open_mcp_and_run(
        self,
        queries: list[tuple[str, str]],
        run_id: str,
        git_commit: str,
        out_jsonl: Path,
        resume: bool = False,
    ) -> list[dict[str, Any]]:
        """Spawn our extended postgres-mcp server via stdio and run optimization across all queries."""
        done_qids: set[str] = set()
        if resume and out_jsonl.exists():
            with out_jsonl.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        done_qids.add(json.loads(line)["query_id"])
            if done_qids:
                print(f"Resuming run '{run_id}': skipping {len(done_qids)} already-logged queries.")

        env = dict(os.environ)
        env["DATABASE_URI"] = self.database_uri
        env["PG_MCP_TIMEOUT_SECONDS"] = str(self.config.statement_timeout_s)
        if self.eval_database_uri:
            env["EVAL_DATABASE_URI"] = self.eval_database_uri
        env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "import sys, postgres_mcp; sys.argv = ['postgres-mcp', '--access-mode', 'unrestricted']; postgres_mcp.main()"],
            env=env,
        )

        allowed = HINTS_TOOLS if self.config.benchmark == "job" else INDEX_TOOLS
        all_records: list[dict[str, Any]] = []

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                gemini_tools = _mcp_tools_to_gemini(tools_resp.tools, allowed)
                openai_tools = _mcp_tools_to_openai(tools_resp.tools, allowed)

                out_jsonl.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if (resume and out_jsonl.exists()) else "w"
                with out_jsonl.open(mode, encoding="utf-8") as fh:
                    for idx, (qid, sql) in enumerate(queries, 1):
                        if qid in done_qids:
                            continue
                        q_records = await self.optimize_query(
                            session, gemini_tools, openai_tools, qid, sql, run_id, git_commit
                        )
                        for rec in q_records:
                            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                        all_records.extend(q_records)
                        base_ms = q_records[0]["execution_latency_ms"]
                        best_ms = min(
                            (r["execution_latency_ms"] for r in q_records if r["action_valid"] and r["execution_latency_ms"] is not None),
                            default=base_ms,
                        )
                        print(f"[{idx:3d}/{len(queries)}] {qid:6s} | base={base_ms} ms -> best={best_ms} ms ({len(q_records)-1} rounds)")

        return all_records


def _parse_execution_time_ms(plan_text: str) -> float | None:
    m = re.search(r"Execution Time:\s*([0-9.]+)\s*ms", plan_text)
    return float(m.group(1)) if m else None


def _parse_root_cost(plan_text: str) -> float | None:
    m = re.search(r"Cost:\s*[0-9.]+\.\.([0-9.]+)", plan_text)
    return float(m.group(1)) if m else None
