"""Thesis extensions for postgres-mcp: pg_hint_plan execution/validation, pg_stats inspection, and 2-phase real index evaluation."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

import pglast
from pglast.ast import IndexStmt
from pglast.ast import SubLink
from pglast.parser import ParseError
from pglast.visitors import Visitor
from psycopg.rows import dict_row

from ..artifacts import ExplainPlanArtifact
from ..sql import DbConnPool
from ..sql import SqlBindParams
from ..sql import SqlDriver


@dataclass
class HintReport:
    used: list[str] = field(default_factory=list)
    not_used: list[str] = field(default_factory=list)
    duplicated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def all_applied(self) -> bool:
        return bool(self.used) and not self.not_used and not self.errors and not self.warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_applied": self.all_applied,
            "used": self.used,
            "not_used": self.not_used,
            "duplicated": self.duplicated,
            "errors": self.errors,
            "warnings": self.warnings,
        }


_POS_SCAN_HINTS = {"seqscan", "indexscan", "indexonlyscan", "bitmapscan", "tidscan"}
_NEG_SCAN_HINTS = {"noseqscan", "noindexscan", "noindexonlyscan", "nobitmapscan", "notidscan"}
_POS_JOIN_HINTS = {"hashjoin", "mergejoin", "nestloop"}
_NEG_JOIN_HINTS = {"nohashjoin", "nomergejoin", "nonestloop"}


def _binarize_leading_body(body: str) -> str:
    """Convert nested Leading(...) bodies like '(a b) c d' into pg_hint_plan binary trees '(((a b) c) d)'."""
    if "(" not in body:
        return body.strip()
    tokens = re.findall(r"\(|\)|[^\s()]+", body)
    stack: list[list[Any]] = [[]]
    for tok in tokens:
        if tok == "(":
            sub: list[Any] = []
            stack[-1].append(sub)
            stack.append(sub)
        elif tok == ")":
            if len(stack) == 1:
                return body.strip()
            stack.pop()
        else:
            stack[-1].append(tok)
    if len(stack) != 1 or not stack[0]:
        return body.strip()

    def _fold(node: Any) -> str:
        if isinstance(node, str):
            return node
        items = [s for s in (_fold(x) for x in node) if s]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        acc = f"({items[0]} {items[1]})"
        for nxt in items[2:]:
            acc = f"({acc} {nxt})"
        return acc

    folded = _fold(stack[0])
    return folded if folded.startswith("(") else f"({folded})"


def _split_hint_directives(inner: str) -> list[tuple[str, str]] | None:
    """Parse 'Name(body) Name2(body2)' into [(Name, body), ...] when parentheses are balanced."""
    directives: list[tuple[str, str]] = []
    i, n = 0, len(inner)
    while i < n:
        while i < n and inner[i].isspace():
            i += 1
        if i >= n:
            break
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", inner[i:])
        if not m:
            return None
        name = m.group(1)
        i += m.end()
        start = i
        depth = 1
        while i < n and depth > 0:
            if inner[i] == "(":
                depth += 1
            elif inner[i] == ")":
                depth -= 1
            i += 1
        if depth != 0:
            return None
        directives.append((name, inner[start : i - 1].strip()))
    return directives


def normalize_hint_block(hints: str) -> str:
    """Normalize a pg_hint_plan comment block, binarize nested Leading(...), drop redundant negations, and block injection."""
    text = hints.strip()
    if not text:
        raise ValueError("Hints string must not be empty.")

    if text.startswith("/*+") and text.endswith("*/"):
        inner = text[3:-2].strip()
    else:
        inner = text

    if not inner:
        raise ValueError("Hint block contains no directives.")
    if "*/" in inner or "/*" in inner or ";" in inner:
        raise ValueError("Invalid characters ('/*', '*/', or ';') inside hint block.")

    parsed = _split_hint_directives(inner)
    if parsed:
        pos_scans = {
            body.split()[0].lower()
            for name, body in parsed
            if name.lower() in _POS_SCAN_HINTS and len(body.split()) == 1
        }
        pos_joins = {
            frozenset(t.lower() for t in body.split())
            for name, body in parsed
            if name.lower() in _POS_JOIN_HINTS and "(" not in body and len(body.split()) >= 2
        }
        cleaned: list[str] = []
        for name, body in parsed:
            low = name.lower()
            if low == "leading":
                body = _binarize_leading_body(body)
            elif low in _NEG_SCAN_HINTS and len(body.split()) == 1 and body.split()[0].lower() in pos_scans:
                continue
            elif low in _NEG_JOIN_HINTS and "(" not in body and frozenset(t.lower() for t in body.split()) in pos_joins:
                continue
            cleaned.append(f"{name}({body})")
        if cleaned:
            inner = " ".join(cleaned)

    return f"/*+ {inner} */"


def parse_hint_notices(notices: list[tuple[str, str, str | None]]) -> HintReport:
    """Parse PostgreSQL NOTICE/WARNING diagnostics emitted by pg_hint_plan 1.6+ debug_print."""
    report = HintReport()
    section_map = {
        "used hint:": report.used,
        "not used hint:": report.not_used,
        "duplication hint:": report.duplicated,
        "error hint:": report.errors,
    }

    for severity, primary, detail in notices:
        if "pg_hint_plan" not in primary:
            continue
        if severity.upper() == "WARNING":
            msg = f"{primary} ({detail})" if detail else primary
            report.warnings.append(msg)
            continue

        current_bucket: list[str] | None = None
        for raw_line in primary.splitlines():
            line = raw_line.strip()
            if not line or line == "pg_hint_plan:":
                continue
            low = line.lower()
            if low in section_map:
                current_bucket = section_map[low]
            elif current_bucket is not None:
                current_bucket.append(line)

    return report


def _strip_leading_hints(sql: str) -> str:
    """Remove any existing leading /*+ ... */ hint comment from a SQL query."""
    return re.sub(r"^\s*/\*\+.*?\*/\s*", "", sql, flags=re.DOTALL).strip()


def _has_bind_variables(sql: str) -> bool:
    return bool(re.search(r"\$\d+", sql))


async def explain_with_hints(
    pool: DbConnPool,
    sql: str,
    hints: str,
    analyze: bool = False,
    timeout_ms: int = 180_000,
) -> dict[str, Any]:
    """Run EXPLAIN or EXPLAIN ANALYZE with pg_hint_plan hints and return plan + exact hint validation report."""
    hint_block = normalize_hint_block(hints)
    clean_sql = _strip_leading_hints(sql).rstrip(";")
    if not clean_sql:
        raise ValueError("SQL query must not be empty.")

    # Reuse postgres-mcp's parametric placeholder substitution when $1, $2... are present
    use_generic_plan = False
    if _has_bind_variables(clean_sql):
        driver = SqlDriver(conn=pool)
        has_like = bool(re.search(r"\bLIKE\b", clean_sql, re.IGNORECASE))
        if analyze or has_like:
            clean_sql = await SqlBindParams(driver).replace_parameters(clean_sql)
        else:
            use_generic_plan = True

    explain_opts = ["FORMAT JSON"]
    if analyze:
        explain_opts.append("ANALYZE")
    if use_generic_plan:
        explain_opts.append("GENERIC_PLAN")

    explain_sql = f"{hint_block}\nEXPLAIN ({', '.join(explain_opts)}) {clean_sql}"

    notices: list[tuple[str, str, str | None]] = []

    def _on_notice(diag: Any) -> None:
        notices.append((diag.severity or "", diag.message_primary or "", diag.message_detail))

    async_pool = await pool.pool_connect()
    async with async_pool.connection() as conn:
        conn.add_notice_handler(_on_notice)
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("BEGIN TRANSACTION READ ONLY")
                try:
                    await cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    await cur.execute("SET LOCAL pg_hint_plan.enable_hint = on")
                    await cur.execute("SET LOCAL pg_hint_plan.debug_print = on")
                    await cur.execute("SET LOCAL pg_hint_plan.message_level = notice")
                    await cur.execute(explain_sql)
                    row = await cur.fetchone()
                finally:
                    await cur.execute("ROLLBACK")
        finally:
            conn.remove_notice_handler(_on_notice)

    if not row or "QUERY PLAN" not in row:
        raise RuntimeError("No QUERY PLAN returned from PostgreSQL.")

    plan_json = row["QUERY PLAN"][0]
    artifact = ExplainPlanArtifact.from_json_data(plan_json)
    hint_report = parse_hint_notices(notices)

    return {
        "hints": hint_block,
        "hint_report": hint_report.to_dict(),
        "total_cost": plan_json["Plan"].get("Total Cost"),
        "planning_time_ms": artifact.planning_time,
        "execution_time_ms": artifact.execution_time,
        "plan_text": artifact.to_text(),
    }


def _downsample_list(values: list[Any] | None, max_items: int) -> list[Any] | None:
    if values is None or len(values) <= max_items:
        return values
    if max_items < 2:
        return values[:max_items]
    step = (len(values) - 1) / (max_items - 1)
    return [values[round(i * step)] for i in range(max_items)]


async def get_table_stats(
    pool: DbConnPool,
    tables: list[str],
    schema: str = "public",
    max_mcv: int = 5,
    max_hist_bins: int = 8,
) -> dict[str, Any]:
    """Fetch compact table sizes, existing indexes, and pg_stats (n_distinct, correlation, MCVs, histograms) in one call."""
    if not tables:
        raise ValueError("Provide at least one table name.")

    clean_tables = [t.strip().lower() for t in tables if t.strip()]
    async_pool = await pool.pool_connect()
    async with async_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    c.relname AS table_name,
                    c.reltuples::bigint AS est_rows,
                    pg_relation_size(c.oid) AS table_bytes,
                    pg_total_relation_size(c.oid) AS total_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relname = ANY(%s)
                  AND c.relkind IN ('r', 'p', 'm')
                ORDER BY c.relname
                """,
                (schema, clean_tables),
            )
            table_rows = await cur.fetchall()

            await cur.execute(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = %s AND tablename = ANY(%s)
                ORDER BY tablename, indexname
                """,
                (schema, clean_tables),
            )
            index_rows = await cur.fetchall()

            await cur.execute(
                """
                SELECT
                    tablename,
                    attname,
                    null_frac,
                    n_distinct,
                    correlation,
                    to_json(most_common_vals) AS most_common_vals,
                    most_common_freqs,
                    to_json(histogram_bounds) AS histogram_bounds
                FROM pg_stats
                WHERE schemaname = %s AND tablename = ANY(%s)
                ORDER BY tablename, attname
                """,
                (schema, clean_tables),
            )
            stat_rows = await cur.fetchall()

    result: dict[str, Any] = {}
    for tr in table_rows:
        tname = tr["table_name"]
        result[tname] = {
            "est_rows": int(tr["est_rows"]),
            "table_mb": round(tr["table_bytes"] / (1024 * 1024), 2),
            "total_mb": round(tr["total_bytes"] / (1024 * 1024), 2),
            "indexes": [],
            "columns": [],
        }

    for ir in index_rows:
        tname = ir["tablename"]
        if tname in result:
            result[tname]["indexes"].append({"name": ir["indexname"], "def": ir["indexdef"]})

    for sr in stat_rows:
        tname = sr["tablename"]
        if tname not in result:
            continue
        col_info: dict[str, Any] = {
            "column": sr["attname"],
            "null_frac": round(float(sr["null_frac"]), 4) if sr["null_frac"] is not None else None,
            "n_distinct": round(float(sr["n_distinct"]), 4) if sr["n_distinct"] is not None else None,
            "correlation": round(float(sr["correlation"]), 4) if sr["correlation"] is not None else None,
        }
        if sr["most_common_vals"] and sr["most_common_freqs"]:
            freqs = [round(float(f), 4) for f in sr["most_common_freqs"][:max_mcv]]
            col_info["top_mcv"] = list(zip(sr["most_common_vals"][:max_mcv], freqs))
        if sr["histogram_bounds"]:
            col_info["hist_sample"] = _downsample_list(sr["histogram_bounds"], max_hist_bins)
        result[tname]["columns"].append(col_info)

    missing = [t for t in clean_tables if t not in result]
    return {"schema": schema, "tables": result, "missing_tables": missing}


_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_ALLOWED_USING = {"btree", "hash", "gin", "gist", "brin"}


class _SubqueryBlocker(Visitor):
    def __init__(self) -> None:
        self.has_sublink = False

    def visit_SubLink(self, ancestors: Any, node: SubLink) -> None:
        self.has_sublink = True


def format_and_validate_index_ddl(
    idx_name: str,
    table: str,
    columns: list[str],
    using: str = "btree",
    where: str | None = None,
) -> str:
    """Build and AST-validate a CREATE INDEX statement (supports plain columns, immutable expressions, and partial WHERE)."""
    tbl = table.strip()
    method = using.strip().lower()
    if not _IDENT_RE.match(tbl):
        raise ValueError(f"Invalid table identifier: {table}")
    if not _IDENT_RE.match(idx_name):
        raise ValueError(f"Invalid index name: {idx_name}")
    if method not in _ALLOWED_USING:
        raise ValueError(f"Unsupported index method: {using}")
    if not columns:
        raise ValueError("Index must specify at least one column or expression.")

    formatted_cols: list[str] = []
    for raw_col in columns:
        col = str(raw_col).strip()
        if not col:
            raise ValueError("Empty column/expression in index definition.")
        if _IDENT_RE.match(col):
            formatted_cols.append(col)
        else:
            wrapped = col if (col.startswith("(") and col.endswith(")")) else f"({col})"
            formatted_cols.append(wrapped)

    where_sql = f" WHERE {where.strip()}" if (where and str(where).strip()) else ""
    ddl = f"CREATE INDEX {idx_name} ON {tbl} USING {method} ({', '.join(formatted_cols)}){where_sql}"

    try:
        stmts = pglast.parse_sql(ddl)
    except ParseError as exc:
        raise ValueError(f"Invalid index expression/predicate syntax: {exc}") from exc

    if len(stmts) != 1 or not isinstance(stmts[0].stmt, IndexStmt):
        raise ValueError("Index DDL failed single-statement IndexStmt AST check.")
    stmt: IndexStmt = stmts[0].stmt
    if stmt.relation.relname != tbl or stmt.relation.schemaname is not None:
        raise ValueError("Unexpected relation target in parsed IndexStmt.")
    sub_check = _SubqueryBlocker()
    sub_check(stmt)
    if sub_check.has_sublink:
        raise ValueError("Subqueries are not permitted inside index expressions or predicates.")

    return ddl


async def evaluate_real_indexes(
    eval_pool: DbConnPool,
    sql: str,
    indexes: list[dict[str, Any]],
    warmup_runs: int = 1,
    timed_runs: int = 1,
    timeout_ms: int = 180_000,
) -> dict[str, Any]:
    """Phase-2 evaluation: create real index(es) on an isolated *_eval clone DB, measure EXPLAIN ANALYZE, then drop them."""
    if not indexes:
        raise ValueError("Provide at least one index definition to evaluate.")

    created_names: list[str] = []
    build_times_ms: list[float] = []
    clean_sql = sql.strip().rstrip(";")

    async_pool = await eval_pool.pool_connect()
    async with async_pool.connection() as conn:
        await conn.set_autocommit(True)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT current_database() AS db")
            db_name = (await cur.fetchone())["db"]
            if not db_name.endswith("_eval"):
                raise PermissionError(
                    f"Refusing to materialize indexes on '{db_name}'. Phase-2 evaluation is restricted to '*_eval' clone databases."
                )

            try:
                touched_tables: set[str] = set()
                for i, idx in enumerate(indexes):
                    table = str(idx["table"]).strip()
                    cols = [str(c).strip() for c in idx["columns"]]
                    using = str(idx.get("using", "btree")).strip().lower()
                    where = idx.get("where")
                    idx_name = f"tmp_adv_idx_{int(time.time() * 1000)}_{i}"
                    ddl = format_and_validate_index_ddl(idx_name, table, cols, using=using, where=where)
                    t0 = time.perf_counter()
                    await cur.execute(ddl)
                    build_times_ms.append(round((time.perf_counter() - t0) * 1000, 2))
                    created_names.append(idx_name)
                    touched_tables.add(table)

                for table in sorted(touched_tables):
                    await cur.execute(f"ANALYZE {table}")

                await cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
                for _ in range(max(0, warmup_runs)):
                    await cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                    await cur.fetchone()

                timings_ms: list[float] = []
                last_plan_json: dict[str, Any] = {}
                for _ in range(max(1, timed_runs)):
                    await cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {clean_sql}")
                    last_plan_json = (await cur.fetchone())["QUERY PLAN"][0]
                    timings_ms.append(float(last_plan_json["Execution Time"]))

                timings_sorted = sorted(timings_ms)
                median_ms = timings_sorted[len(timings_sorted) // 2]
                artifact = ExplainPlanArtifact.from_json_data(last_plan_json)

                return {
                    "eval_database": db_name,
                    "created_indexes": created_names,
                    "index_build_times_ms": build_times_ms,
                    "timed_execution_ms": timings_ms,
                    "median_execution_ms": round(median_ms, 3),
                    "total_cost": last_plan_json["Plan"].get("Total Cost"),
                    "plan_text": artifact.to_text(),
                }
            finally:
                await cur.execute("RESET statement_timeout")
                for idx_name in created_names:
                    await cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
                await conn.set_autocommit(False)
