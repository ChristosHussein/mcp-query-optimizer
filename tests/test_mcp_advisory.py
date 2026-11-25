"""Self-check & integration tests for the extended postgres-mcp advisory tools."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from types import SimpleNamespace

from advisor.agent import _extract_json_proposal
from advisor.agent import _mcp_tools_to_gemini
from advisor.agent import _mcp_tools_to_openai
from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from postgres_mcp.advisory import evaluate_real_indexes
from postgres_mcp.advisory import explain_with_hints
from postgres_mcp.advisory import format_and_validate_index_ddl
from postgres_mcp.advisory import get_table_stats
from postgres_mcp.advisory import normalize_hint_block
from postgres_mcp.advisory import parse_hint_notices
from postgres_mcp.explain import ExplainPlanTool
from postgres_mcp.sql import DbConnPool
from postgres_mcp.sql import SqlDriver

ROOT = Path(__file__).resolve().parent.parent


def test_unit_helpers() -> None:
    assert normalize_hint_block("HashJoin(ci t)") == "/*+ HashJoin(ci t) */"
    assert normalize_hint_block("/*+ SeqScan(t) */") == "/*+ SeqScan(t) */"
    # Nested Leading(...) auto-binarization & redundant negative hint deduplication
    assert normalize_hint_block("Leading((cn mc) t ci) HashJoin(cn mc) NoNestLoop(mc cn) SeqScan(t) NoIndexScan(t)") == (
        "/*+ Leading((((cn mc) t) ci)) HashJoin(cn mc) SeqScan(t) */"
    )
    assert normalize_hint_block("Leading(((a b) c))") == "/*+ Leading(((a b) c)) */"
    assert normalize_hint_block("Leading(a b c d)") == "/*+ Leading(a b c d) */"

    # pglast AST validation for plain, functional (TPC-H q22 substring), and partial indexes
    ddl_expr = format_and_validate_index_ddl(
        "tmp_idx_0", "customer", ["substring(c_phone FROM 1 FOR 2)", "c_acctbal"], using="btree", where="c_acctbal > 0"
    )
    assert "((substring(c_phone FROM 1 FOR 2)), c_acctbal) WHERE c_acctbal > 0" in ddl_expr
    for bad_idx in [
        ("customer", ["c_acctbal); DROP TABLE customer; --"], None),
        ("customer", ["(SELECT max(c_acctbal) FROM customer)"], None),
        ("customer; DROP TABLE x", ["c_acctbal"], None),
    ]:
        try:
            format_and_validate_index_ddl("tmp_idx_bad", bad_idx[0], bad_idx[1], where=bad_idx[2])
            raise AssertionError(f"Expected ValueError for malicious/invalid index: {bad_idx!r}")
        except ValueError:
            pass

    for bad in ["", "/*+ */", "SeqScan(t) */ DROP TABLE t; /*+", "SeqScan(t); SELECT 1"]:
        try:
            normalize_hint_block(bad)
            raise AssertionError(f"Expected ValueError for: {bad!r}")
        except ValueError:
            pass

    parsed = parse_hint_notices([
        ("WARNING", 'pg_hint_plan: hint syntax error at or near "BadHint(x)"', 'Unrecognized hint keyword "BadHint".'),
        (
            "NOTICE",
            "pg_hint_plan:\nused hint:\nSeqScan(t)\nnot used hint:\nIndexScan(z)\nduplication hint:\nerror hint:\n",
            None,
        ),
    ])
    assert parsed.used == ["SeqScan(t)"]
    assert parsed.not_used == ["IndexScan(z)"]
    assert len(parsed.warnings) == 1
    assert not parsed.all_applied

    fake_tool = SimpleNamespace(name="explain_query", description="desc", inputSchema={"type": "object", "properties": {}})
    assert _mcp_tools_to_openai([fake_tool], {"explain_query"})[0]["function"]["name"] == "explain_query"
    assert _mcp_tools_to_gemini([fake_tool], {"explain_query"})[0]["name"] == "explain_query"
    prop = _extract_json_proposal('Analysis:\n```json\n{"action_type": "hints", "hints": "/*+ SeqScan(t) */"}\n```')
    assert prop is not None and prop["action_type"] == "hints"
    print("[PASS] Unit checks: hints, parser, provider tool schemas & JSON extraction")


async def test_live_imdb_tools() -> None:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    imdb_pool = DbConnPool(f"postgresql://postgres:{pw}@localhost:5432/imdb")
    try:
        # 1. get_table_stats
        stats = await get_table_stats(imdb_pool, ["title", "cast_info"])
        assert stats["missing_tables"] == []
        assert stats["tables"]["title"]["est_rows"] > 2_000_000
        assert stats["tables"]["cast_info"]["est_rows"] > 30_000_000
        assert len(stats["tables"]["cast_info"]["indexes"]) >= 4
        col_names = {c["column"] for c in stats["tables"]["title"]["columns"]}
        assert {"id", "kind_id", "production_year"} <= col_names
        print("[PASS] get_table_stats on IMDb (title, cast_info)")

        # 2. explain_with_hints (valid hints on JOB 1a)
        job_1a = (ROOT / "benchmarks" / "job" / "queries" / "1a.sql").read_text(encoding="utf-8")
        res_ok = await explain_with_hints(
            imdb_pool,
            job_1a,
            "HashJoin(mc t) SeqScan(ct)",
            analyze=True,
            timeout_ms=60_000,
        )
        assert res_ok["hint_report"]["all_applied"], f"Expected hints applied: {res_ok['hint_report']}"
        assert res_ok["execution_time_ms"] is not None and res_ok["execution_time_ms"] > 0
        assert " AS " in res_ok["plan_text"], "Expected table aliases in plan_text"
        print(f"[PASS] explain_with_hints (valid) on JOB 1a -> {res_ok['execution_time_ms']:.2f} ms")

        # 2b. Verify previously failing LLM syntax 'Leading((ct mc) t it mi_idx) SeqScan(ct) NoIndexScan(ct)' normalizes & runs with 0 warnings
        res_norm = await explain_with_hints(
            imdb_pool,
            job_1a,
            "Leading((ct mc) t it mi_idx) SeqScan(ct) NoIndexScan(ct)",
            analyze=False,
        )
        assert not res_norm["hint_report"]["warnings"], f"Expected 0 warnings after normalization, got: {res_norm['hint_report']}"
        assert any("Leading" in u for u in res_norm["hint_report"]["used"]), f"Expected Leading used: {res_norm['hint_report']}"
        print("[PASS] explain_with_hints normalizes nested Leading((a b) c) & conflict hints with 0 warnings")

        # 3. explain_with_hints (invalid hint caught via warning/not_used)
        res_bad = await explain_with_hints(
            imdb_pool,
            job_1a,
            "NotARealHint(t) SeqScan(ct)",
            analyze=False,
        )
        assert not res_bad["hint_report"]["all_applied"]
        assert res_bad["hint_report"]["warnings"], "Expected pg_hint_plan warning"
        print("[PASS] explain_with_hints catches invalid hint warnings")

        # 4. Parametric placeholder substitution + HypoPG via ExplainPlanTool
        driver = SqlDriver(conn=imdb_pool)
        explain_tool = ExplainPlanTool(driver)
        param_res = await explain_tool.explain(
            "SELECT count(*) FROM title WHERE production_year = $1 AND kind_id = $2",
            do_analyze=False,
        )
        assert hasattr(param_res, "plan_tree"), f"Parametric EXPLAIN failed: {param_res}"

        hypo_res = await explain_tool.explain_with_hypothetical_indexes(
            "SELECT id FROM title WHERE production_year = 2005",
            [{"table": "title", "columns": ["production_year"], "using": "btree"}],
        )
        assert hasattr(hypo_res, "plan_tree"), f"HypoPG EXPLAIN failed: {hypo_res}"
        assert "btree_title_production_year" in hypo_res.to_text()
        print("[PASS] Parametric placeholder EXPLAIN & HypoPG EXPLAIN")

        # 5. Safety guard: evaluate_real_indexes must refuse non-*_eval DBs
        try:
            await evaluate_real_indexes(
                imdb_pool,
                "SELECT count(*) FROM title",
                [{"table": "title", "columns": ["production_year"]}],
            )
            raise AssertionError("Expected PermissionError on non-*_eval database")
        except PermissionError:
            print("[PASS] evaluate_real_indexes safety guard blocked non-*_eval DB")
    finally:
        await imdb_pool.close()

    # 6. Live Phase-2 test on tpch_eval (small supplier table -> fast build & verify cleanup)
    eval_pool = DbConnPool(f"postgresql://postgres:{pw}@localhost:5432/tpch_eval")
    try:
        p2 = await evaluate_real_indexes(
            eval_pool,
            "SELECT count(*) FROM supplier WHERE s_nationkey = 5",
            [{"table": "supplier", "columns": ["s_nationkey"], "using": "btree"}],
            warmup_runs=1,
            timed_runs=1,
        )
        assert p2["eval_database"] == "tpch_eval"
        assert p2["median_execution_ms"] > 0
        assert "tmp_adv_idx_" in p2["plan_text"]
        # Confirm index was cleanly dropped afterwards
        stats_after = await get_table_stats(eval_pool, ["supplier"])
        assert not any("tmp_adv_idx_" in idx["name"] for idx in stats_after["tables"]["supplier"]["indexes"])
        # Functional + partial index test (TPC-H q22 pattern: substring(s_phone FROM 1 FOR 2))
        p2_expr = await evaluate_real_indexes(
            eval_pool,
            "SELECT count(*) FROM supplier WHERE substring(s_phone FROM 1 FOR 2) = '13' AND s_acctbal > 0",
            [{"table": "supplier", "columns": ["substring(s_phone FROM 1 FOR 2)", "s_acctbal"], "using": "btree", "where": "s_acctbal > 0"}],
            warmup_runs=1,
            timed_runs=1,
        )
        assert p2_expr["median_execution_ms"] > 0
        assert "tmp_adv_idx_" in p2_expr["plan_text"]
        print(f"[PASS] Phase-2 evaluate_real_indexes (plain + functional/partial) on tpch_eval ({p2['median_execution_ms']:.2f} ms, dropped cleanly)")
    finally:
        await eval_pool.close()

    # 7. Real MCP stdio server handshake & tool discovery
    env = dict(os.environ)
    env["DATABASE_URI"] = f"postgresql://postgres:{pw}@localhost:5432/imdb"
    env["EVAL_DATABASE_URI"] = f"postgresql://postgres:{pw}@localhost:5432/tpch_eval"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "import sys, postgres_mcp; sys.argv = ['postgres-mcp', '--access-mode', 'restricted']; postgres_mcp.main()"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            required = {"explain_query", "explain_with_hints", "get_table_stats", "evaluate_real_indexes"}
            assert required <= tools, f"Missing MCP tools: {required - tools}"
            print(f"[PASS] Live MCP stdio server exposes all required tools ({len(tools)} total)")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    test_unit_helpers()
    if "--unit-only" not in sys.argv:
        asyncio.run(test_live_imdb_tools())
    print("ALL MCP ADVISORY TESTS PASSED")
