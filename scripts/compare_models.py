"""Quick model bake-off: same documents through a few models, compare facts / verification / cost.

    uv run python scripts/compare_models.py --n 8
"""
import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from createto import db
from createto.config import Config
from createto.extract import call_llm, chunk_pages, load_jobs, verify

MODELS = ["anthropic/claude-haiku-5.5", "openai/gpt-5.6-luna", "google/gemini-3.1-flash-lite"]

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=8)
ap.add_argument("--models", nargs="*", default=MODELS)
args = ap.parse_args()

cfg = Config()
con = db.connect(cfg.db_path)
jobs, _ = load_jobs(con, cfg)
random.Random(7).shuffle(jobs)
jobs = jobs[: args.n]
for j in jobs:
    rows = con.execute("SELECT page, text FROM pdf_pages WHERE sha256 = ? ORDER BY page", (j["sha"],)).fetchall()
    j["pages"] = {r["page"]: r["text"] for r in rows}
    j["chunk"] = chunk_pages([(r["page"], r["text"]) for r in rows], 20, 60_000)[0]

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=cfg.api_key)
results = {}
for model in args.models:
    def one(j):
        try:
            return j, call_llm(client, model, j, j["chunk"])
        except Exception as e:
            return j, {"error": str(e)[:200]}
    tot = {"facts": 0, "verified": 0, "exact": 0, "cost": 0.0, "ms": [], "errors": 0}
    with ThreadPoolExecutor(8) as pool:
        for j, out in pool.map(one, jobs):
            if "error" in out:
                tot["errors"] += 1
                continue
            facts = json.loads(out["content"])["facts"]
            tot["facts"] += len(facts)
            for f in facts:
                v = verify(f["excerpt"], f["page"], j["pages"])
                tot["verified"] += bool(v)
                tot["exact"] += bool(v and v[1] == "exact")
            tot["cost"] += out["cost"] or 0
            tot["ms"].append(out["ms"])
    tot["ms"] = sorted(tot["ms"])[len(tot["ms"]) // 2] if tot["ms"] else None
    results[model] = tot

print(f"{len(jobs)} docs, {sum(len(j['chunk']) for j in jobs)} pages\n")
print(f"{'model':32} {'facts':>6} {'verified':>9} {'exact':>6} {'cost $':>8} {'p50 ms':>7} {'errors':>6}")
for m, t in results.items():
    pct = f"{100 * t['verified'] / t['facts']:.0f}%" if t["facts"] else "-"
    print(f"{m:32} {t['facts']:>6} {pct:>9} {t['exact']:>6} {t['cost']:>8.4f} {t['ms'] or '-':>7} {t['errors']:>6}")
