"""LLM fact extraction from PDF text.

Every fact has to come with a page number and a verbatim excerpt; we check the excerpt is actually on that
page (exact, then fuzzy) and drop anything we can't find. Results are cached on (file sha, pages, prompt
version, model) so unchanged files never cost anything on a re-run.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
from rapidfuzz import fuzz

from . import db

PROMPT_VERSION = "v1"

SYSTEM = """You extract facts from documents published by CreateTO, the City of Toronto's real estate agency.
The documents are staff reports, board minutes, community meeting summaries, info sheets, notices and reports
about city-owned sites and development projects.

Extract concrete, useful facts: milestones and decisions with dates, housing unit counts, costs/budgets/funding,
building size (storeys, floor area, height), site area, partners (developers, operators, builders) and status
statements about where a project is.

Rules:
- Only extract what the text explicitly says. No inference, no outside knowledge.
- Every fact needs the page number (from the === PAGE N === markers) and an excerpt copied VERBATIM from that
  page: one sentence or phrase, max ~250 characters, enough to support the fact.
- Use ISO dates (YYYY-MM-DD, or YYYY-MM / YYYY if that's all the text gives).
- Put numbers in `amount` (plain number, no commas) with a `unit` like "units", "CAD", "storeys", "sq m", "acres".
- Skip boilerplate (contact info, accessibility notices, land acknowledgements).
- If the document has no useful facts, return an empty list."""

FACT_TYPES = ["milestone", "decision", "housing_units", "cost", "funding", "building_size", "site_area",
              "partner", "status", "other"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "facts"],
    "properties": {
        "document": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "document_type", "document_date", "summary"],
            "properties": {
                "title": {"type": "string"},
                "document_type": {"type": "string", "enum": [
                    "staff_report", "board_minutes", "board_agenda", "meeting_summary", "presentation",
                    "info_sheet", "notice", "annual_report", "plan_or_study", "other"]},
                "document_date": {"type": ["string", "null"]},
                "summary": {"type": "string"},
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "description", "date", "amount", "unit", "project", "page", "excerpt"],
                "properties": {
                    "type": {"type": "string", "enum": FACT_TYPES},
                    "description": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "amount": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "project": {"type": ["string", "null"], "description": "project/site name if stated"},
                    "page": {"type": "integer"},
                    "excerpt": {"type": "string"},
                },
            },
        },
    },
}

YEAR_RE = re.compile(r"(?<!\d)(20[0-3]\d|19[89]\d)(?!\d)")


def norm(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    s = re.sub(r"-\s*\n\s*", "", s)  # words hyphenated across lines
    return re.sub(r"\s+", " ", s).strip().lower()


def verify(excerpt, page, pages):
    """Find the excerpt on the claimed page (or a neighbour). Returns (page, 'exact'|'fuzzy') or None."""
    e = norm(excerpt)
    if len(e) < 8:
        return None
    for p in (page, page - 1, page + 1):
        text = pages.get(p)
        if text and e in norm(text):
            return p, "exact"
    for p in (page, page - 1, page + 1):
        text = pages.get(p)
        if text and fuzz.partial_ratio(e, norm(text)) >= 90:
            return p, "fuzzy"
    return None


def chunk_pages(pages, max_pages, max_chars):
    """Split [(page_no, text)] into groups that fit in one request."""
    chunks, cur, size = [], [], 0
    for no, text in pages[:max_pages]:
        if cur and size + len(text) > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((no, text[:max_chars]))
        size += len(text)
    if cur:
        chunks.append(cur)
    return chunks


def file_year(url, linked_from_dates):
    years = [int(y) for y in YEAR_RE.findall(url.rsplit("/", 1)[-1])] or \
            [int(y) for y in YEAR_RE.findall(url)]
    if years:
        return max(years)
    if linked_from_dates:
        return int(max(linked_from_dates)[:4])
    return None


def load_jobs(con, cfg):
    """Work out which files to send to the LLM, and who each file belongs to."""
    files = con.execute(
        """SELECT d.url, d.sha256, i.pages FROM documents d JOIN pdf_info i ON i.sha256 = d.sha256
           WHERE d.kind = 'file' AND i.status = 'ok' ORDER BY d.url"""
    ).fetchall()
    jobs, skipped = [], []
    for f in files:
        owners = con.execute(
            """SELECT e.id, e.type, e.name, e.data, l.text, l.context FROM links l
               JOIN entities e ON e.url = l.from_url
               WHERE l.to_url = ? ORDER BY CASE l.context WHEN 'content' THEN 0 WHEN 'html' THEN 1 ELSE 2 END""",
            (f["url"],),
        ).fetchall()
        content = [o for o in owners if o["context"] == "content"] or owners
        dates = [json.loads(o["data"]).get("published_at") for o in content if o["type"] == "article"]
        year = file_year(f["url"], [d for d in dates if d])
        if year is not None and year < cfg.since.year:
            skipped.append((f["url"], f"dated {year}, before --since {cfg.since}"))
            continue
        projects = [o for o in content if o["type"] == "project"]
        owner = (projects or content or [None])[0]
        jobs.append({
            "url": f["url"], "sha": f["sha256"],
            "entity_id": owner["id"] if owner else f"file:{f['url']}",
            "owner_name": owner["name"] if owner else None,
            "link_text": next((o["text"] for o in owners if o["text"]), None),
            "linked_from": sorted({o["id"] for o in owners}),
        })
    if cfg.max_llm_docs is not None:
        skipped += [(j["url"], "over --max-llm-docs") for j in jobs[cfg.max_llm_docs:]]
        jobs = jobs[: cfg.max_llm_docs]
    return jobs, skipped


def call_llm(client, model, job, chunk):
    pages_txt = "\n\n".join(f"=== PAGE {no} ===\n{text}" for no, text in chunk)
    user = (f"Document URL: {job['url']}\n"
            f"Link text on CreateTO: {job['link_text'] or 'n/a'}\n"
            f"Linked from: {job['owner_name'] or 'n/a'}\n\n{pages_txt}")
    t = time.monotonic()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "extraction", "strict": True, "schema": SCHEMA}},
        temperature=0,
        extra_body={"usage": {"include": True}},
    )
    usage = resp.usage.model_dump() if resp.usage else {}
    return {
        "content": resp.choices[0].message.content,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost": usage.get("cost"),
        "ms": int((time.monotonic() - t) * 1000),
    }


def run_extraction(con, cfg, run_id):
    stats = {"files": 0, "llm_calls": 0, "cache_hits": 0, "errors": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "cost_usd": 0.0, "facts_kept": 0, "facts_dropped": 0, "facts_added": 0,
             "facts_closed": 0, "skipped": 0}
    jobs, skipped = load_jobs(con, cfg)
    for url, reason in skipped:
        db.log(con, run_id, f"llm:{url}", "llm", "skipped", reason)
    stats["skipped"] = len(skipped)

    # figure out every (file, chunk) request and which ones we already have cached.
    # the same PDF is sometimes uploaded under several URLs, so todo is keyed on the cache key -> one call each
    todo = {}
    for job in jobs:
        rows = con.execute("SELECT page, text FROM pdf_pages WHERE sha256 = ? ORDER BY page", (job["sha"],)).fetchall()
        job["pages"] = {r["page"]: r["text"] for r in rows}
        job["chunks"] = []
        for chunk in chunk_pages([(r["page"], r["text"]) for r in rows], cfg.max_pages_per_doc, 60_000):
            pages_key = f"{chunk[0][0]}-{chunk[-1][0]}"
            key = db.sha256(f"{job['sha']}|{pages_key}|{PROMPT_VERSION}|{cfg.model}".encode())
            hit = con.execute("SELECT response FROM llm_calls WHERE cache_key = ? AND error IS NULL "
                              "ORDER BY id LIMIT 1", (key,)).fetchone()
            ch = {"key": key, "pages": pages_key, "chunk": chunk, "response": hit["response"] if hit else None}
            job["chunks"].append(ch)
            if hit:
                stats["cache_hits"] += 1
            else:
                todo.setdefault(key, []).append((job, ch))

    if todo:
        if not cfg.api_key:
            raise SystemExit("OPENROUTER_API_KEY is not set (see .env.example), or run with --no-llm")
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=cfg.api_key, max_retries=3)

        def work(items):
            job, ch = items[0]
            try:
                return items, call_llm(client, cfg.model, job, ch["chunk"]), None
            except Exception as e:
                return items, None, f"{type(e).__name__}: {e}"[:500]

        with ThreadPoolExecutor(cfg.llm_workers) as pool:
            for items, out, err in pool.map(work, todo.values()):
                job, ch = items[0]
                stats["llm_calls"] += 1
                if err:
                    stats["errors"] += 1
                    con.execute("INSERT INTO llm_calls (run_id, cache_key, model, sha256, pages, error, at) "
                                "VALUES (?,?,?,?,?,?,?)", (run_id, ch["key"], cfg.model, job["sha"], ch["pages"],
                                                           err, db.now()))
                    continue
                for _, same in items:
                    same["response"] = out["content"]
                stats["prompt_tokens"] += out["prompt_tokens"] or 0
                stats["completion_tokens"] += out["completion_tokens"] or 0
                stats["cost_usd"] += out["cost"] or 0
                con.execute(
                    """INSERT INTO llm_calls (run_id, cache_key, model, sha256, pages, prompt_tokens,
                       completion_tokens, cost, ms, response, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, ch["key"], cfg.model, job["sha"], ch["pages"], out["prompt_tokens"],
                     out["completion_tokens"], out["cost"], out["ms"], out["content"], db.now()),
                )
        con.commit()

    for job in jobs:
        if any(ch["response"] is None for ch in job["chunks"]):
            continue  # failed this time; keep whatever facts we had for it
        facts, doc_meta = [], None
        for ch in job["chunks"]:
            try:
                out = json.loads(ch["response"])
            except json.JSONDecodeError:
                continue
            doc_meta = doc_meta or out.get("document")
            for f in out.get("facts", []):
                found = verify(f.get("excerpt", ""), f.get("page", 0), job["pages"])
                if not found:
                    stats["facts_dropped"] += 1
                    continue
                page, how = found
                value = {k: f[k] for k in ("description", "date", "amount", "unit", "project") if f.get(k) is not None}
                value["verification"] = how
                facts.append({
                    "entity_id": job["entity_id"], "field": f["type"], "value": value,
                    "source_url": job["url"], "source_sha256": job["sha"], "page": page,
                    "excerpt": f["excerpt"], "locator": f"page {page}", "method": "llm", "model": cfg.model,
                    "prompt_version": PROMPT_VERSION, "effective_at": f.get("date"),
                })
        # filenames don't always carry a year, so also check the date the model read off the document itself
        doc_year = YEAR_RE.match((doc_meta or {}).get("document_date") or "")
        if doc_year and int(doc_year.group(1)) < cfg.since.year:
            db.log(con, run_id, f"llm:{job['url']}", "llm", "skipped",
                   f"document dated {doc_meta['document_date']}, before --since {cfg.since}")
            stats["skipped"] += 1
            stats["facts_dropped"] += len(facts)
            facts = []
        stats["facts_kept"] += len(facts)
        a, c = db.sync_facts(con, run_id, f"llm:{job['url']}", facts)
        stats["facts_added"] += a
        stats["facts_closed"] += c
        stats["files"] += 1

        db.upsert_entity(con, run_id, f"file:{job['url']}", "file", (doc_meta or {}).get("title") or job["link_text"],
                         job["url"], {"url": job["url"], "sha256": job["sha"], "pages": len(job["pages"]),
                                      "link_text": job["link_text"], "about": job["entity_id"],
                                      "linked_from": job["linked_from"], "document": doc_meta})
    con.commit()
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats
