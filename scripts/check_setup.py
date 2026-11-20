"""Safe connectivity check for PostgreSQL 16 (Docker), NVIDIA NIM models, and Gemini API (never prints keys)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from google import genai
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env")

    # 1. PostgreSQL check
    pw = os.environ.get("POSTGRES_PASSWORD", "postgres")
    for db in ("imdb", "tpch", "tpch_eval"):
        try:
            with psycopg.connect(f"postgresql://postgres:{pw}@localhost:5432/{db}", connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_database(), count(*) FROM pg_tables WHERE schemaname = 'public'")
                    name, tbls = cur.fetchone()
                    print(f"[OK] PostgreSQL '{name}': {tbls} public tables")
        except Exception as e:
            print(f"[FAIL] PostgreSQL '{db}': {e}")

    # 2. NVIDIA NIM check
    nv_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not nv_key or nv_key.startswith("your_"):
        print("[SKIP] NVIDIA_API_KEY not set in .env")
    else:
        client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=nv_key, timeout=20.0, max_retries=0)
        try:
            models = [m.id for m in client.models.list().data]
            minimax_matches = [m for m in models if "minimax" in m.lower()]
            nemotron_matches = [m for m in models if "nemotron-3-ultra" in m.lower() or "nemotron" in m.lower()]
            print(f"[OK] NVIDIA NIM catalog reachable ({len(models)} models)", flush=True)
            print(f"     minimax matches : {minimax_matches[:10]}", flush=True)
            print(f"     nemotron matches: {[m for m in nemotron_matches if 'ultra' in m.lower()][:10]}", flush=True)

            targets: list[str] = []
            for pref in ("minimax-m3.1-flash", "nemotron-3-ultra-550b-a55b"):
                hit = next((m for m in models if pref in m.lower()), None)
                if hit:
                    targets.append(hit)

            for mid in targets:
                try:
                    print(f"  -> pinging NVIDIA '{mid}'...", flush=True)
                    resp = client.chat.completions.create(
                        model=mid,
                        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                        max_tokens=16,
                        temperature=0.0,
                    )
                    msg = resp.choices[0].message
                    reply = (msg.content or getattr(msg, "reasoning_content", "") or "").strip()[:40]
                    toks = resp.usage.total_tokens if resp.usage else "?"
                    print(f"[OK] NVIDIA NIM '{mid}' -> reply={reply!r} (tokens={toks})", flush=True)
                except Exception as e:
                    print(f"[FAIL] NVIDIA NIM '{mid}': {e}", flush=True)
        except Exception as e:
            print(f"[FAIL] NVIDIA NIM list/auth failed: {e}", flush=True)

    # 3. Gemini API check
    gem_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gem_key or gem_key.startswith("your_"):
        print("[SKIP] GEMINI_API_KEY not set in .env", flush=True)
    else:
        gclient = genai.Client(api_key=gem_key, http_options={"timeout": 20000})
        for gmodel in ("gemini-3.8-flash", "gemini-4-argon"):
            try:
                print(f"  -> pinging Gemini '{gmodel}'...", flush=True)
                inter = gclient.interactions.create(
                    model=gmodel,
                    input="Reply with exactly: OK",
                )
                reply = (inter.output_text or "").strip()[:40]
                toks = getattr(getattr(inter, "usage", None), "total_tokens", "?")
                print(f"[OK] Gemini '{gmodel}' -> reply={reply!r} (tokens={toks})", flush=True)
            except Exception as e:
                print(f"[FAIL] Gemini '{gmodel}': {e}", flush=True)

        try:
            emb = gclient.models.embed_content(model="gemini-embedding-001", contents="SELECT 1;")
            dim = len(emb.embeddings[0].values)
            print(f"[OK] Gemini 'gemini-embedding-001' -> dim={dim}", flush=True)
        except Exception as e:
            print(f"[FAIL] Gemini 'gemini-embedding-001': {e}", flush=True)


if __name__ == "__main__":
    main()
