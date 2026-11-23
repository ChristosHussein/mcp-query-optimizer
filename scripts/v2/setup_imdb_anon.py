"""Create a 100% leak-free schema-anonymized clone of IMDb ('imdb_anon') and rewrite JOB Top-12 queries.

Renames ALL 21 tables -> t01..t21, ALL columns -> c01..cN, and ALL indexes -> idx_01..idx_NN
inside database 'imdb_anon' (cloned from 'imdb'), so ZERO IMDb identifiers appear anywhere in
SQL, EXPLAIN ANALYZE, pg_stats, or pg_hint_plan, while preserving 100% identical data skew,
physical pages, and cardinality estimation errors.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
JOB_TOP12 = ["16b", "19d", "17e", "17a", "17f", "31c", "25c", "10c", "17d", "17c", "17b", "31a"]


def get_dsn(dbname: str) -> str:
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    return f"postgresql://postgres:{pw}@{host}:{port}/{dbname}"


def rewrite_job_query(sql: str, tbl_map: dict[str, str], col_map: dict[str, dict[str, str]]) -> str:
    """Deterministically rewrite a JOB query using opaque table, column, and alias names."""
    # Parse FROM list: "table AS alias"
    m = re.search(r"\bFROM\b(.*?)\bWHERE\b", sql, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError("Could not locate FROM ... WHERE in JOB query")
    alias_to_tbl: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    from_parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    new_from: list[str] = []
    for idx, part in enumerate(from_parts, 1):
        pm = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s+([a-zA-Z_][a-zA-Z0-9_]*)$", part, flags=re.IGNORECASE)
        if not pm:
            raise ValueError(f"Unexpected FROM item: {part!r}")
        orig_tbl, orig_alias = pm.group(1).lower(), pm.group(2)
        new_alias = f"a{idx:02d}"
        alias_to_tbl[orig_alias] = orig_tbl
        alias_map[orig_alias] = new_alias
        new_from.append(f"{tbl_map[orig_tbl]} AS {new_alias}")

    # Rewrite SELECT output aliases (AS something -> AS out_1, out_2, ...)
    sel_m = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, flags=re.IGNORECASE | re.DOTALL)
    sel_body = sel_m.group(1)
    where_body = sql[m.end():]

    def replace_refs(fragment: str) -> str:
        # Replace alias.column outside single-quoted string literals
        parts = re.split(r"('(?:''|[^'])*')", fragment)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            # Sort aliases longest-first to avoid prefix collisions
            for orig_alias in sorted(alias_to_tbl.keys(), key=len, reverse=True):
                orig_tbl = alias_to_tbl[orig_alias]
                new_alias = alias_map[orig_alias]
                def _sub(match: re.Match[str]) -> str:
                    orig_col = match.group(1).lower()
                    return f"{new_alias}.{col_map[orig_tbl][orig_col]}"
                seg = re.sub(
                    rf"\b{re.escape(orig_alias)}\.([a-zA-Z_][a-zA-Z0-9_]*)\b",
                    _sub,
                    seg,
                )
            parts[i] = seg
        return "".join(parts)

    new_sel = replace_refs(sel_body)
    # Anonymize SELECT AS output column labels too (e.g. AS member_in_charnamed_movie -> AS out_01)
    out_idx = 0
    def _anon_out(_m: re.Match[str]) -> str:
        nonlocal out_idx
        out_idx += 1
        return f" AS out_{out_idx:02d}"
    new_sel = re.sub(r"\s+AS\s+[a-zA-Z_][a-zA-Z0-9_]*", _anon_out, new_sel, flags=re.IGNORECASE)
    new_where = replace_refs(where_body)

    return f"SELECT{new_sel}FROM\n    " + ",\n    ".join(new_from) + "\nWHERE" + new_where


def main() -> None:
    load_dotenv(ROOT / ".env")

    # 1) Clone imdb -> imdb_anon (drop if already exists)
    with psycopg.connect(get_dsn("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'imdb_anon'")
            if cur.fetchone():
                print("Database 'imdb_anon' already exists — dropping and recreating cleanly...")
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'imdb_anon' AND pid <> pg_backend_pid()")
                cur.execute("DROP DATABASE imdb_anon")
            print("Cloning imdb -> imdb_anon (8.7 GB, ~1-2 min)...")
            cur.execute("CREATE DATABASE imdb_anon TEMPLATE imdb")
            print("Clone complete.")

    # 2) Introspect tables, columns, and indexes in imdb_anon and rename everything
    tbl_map: dict[str, str] = {}
    col_map: dict[str, dict[str, str]] = {}
    idx_map: dict[str, str] = {}

    with psycopg.connect(get_dsn("imdb_anon"), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            tables = [r[0] for r in cur.fetchall()]
            for i, tbl in enumerate(tables, 1):
                new_tbl = f"t{i:02d}"
                tbl_map[tbl] = new_tbl
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                    (tbl,),
                )
                cols = [r[0] for r in cur.fetchall()]
                col_map[tbl] = {}
                for j, col in enumerate(cols, 1):
                    new_col = f"c{j:02d}"
                    col_map[tbl][col] = new_col
                    cur.execute(f'ALTER TABLE public."{tbl}" RENAME COLUMN "{col}" TO "{new_col}"')
                cur.execute(f'ALTER TABLE public."{tbl}" RENAME TO "{new_tbl}"')

            # Rename all indexes so EXPLAIN never leaks original IMDb index names
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY indexname")
            indexes = [r[0] for r in cur.fetchall()]
            for k, idx_name in enumerate(indexes, 1):
                new_idx = f"idx_{k:02d}"
                idx_map[idx_name] = new_idx
                cur.execute(f'ALTER INDEX public."{idx_name}" RENAME TO "{new_idx}"')
        conn.commit()
    print(f"Renamed {len(tbl_map)} tables, {sum(len(v) for v in col_map.values())} columns, {len(idx_map)} indexes in 'imdb_anon'.")

    # 3) Rewrite Top-12 JOB queries into benchmarks/imdb_anon/queries/
    out_qdir = ROOT / "benchmarks" / "imdb_anon" / "queries"
    out_qdir.mkdir(parents=True, exist_ok=True)
    (ROOT / "benchmarks" / "imdb_anon" / "mapping.json").write_text(
        json.dumps({"tables": tbl_map, "columns": col_map, "indexes": idx_map}, indent=2) + "\n",
        encoding="utf-8",
    )

    with psycopg.connect(get_dsn("imdb_anon"), autocommit=True) as conn:
        with conn.cursor() as cur:
            for qid in JOB_TOP12:
                orig_sql = (ROOT / "benchmarks" / "job" / "queries" / f"{qid}.sql").read_text(encoding="utf-8")
                anon_sql = rewrite_job_query(orig_sql, tbl_map, col_map)
                (out_qdir / f"{qid}.sql").write_text(anon_sql, encoding="utf-8")
                # Smoke-verify EXPLAIN succeeds cleanly
                cur.execute(f"EXPLAIN {anon_sql.strip().rstrip(';')}")
                _ = cur.fetchall()
                print(f"  [OK] Rewrote & EXPLAIN-verified imdb_anon/{qid}.sql")

    print("imdb_anon setup complete!")


if __name__ == "__main__":
    main()
