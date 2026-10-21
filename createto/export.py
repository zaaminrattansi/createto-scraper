import json
from collections import Counter
from pathlib import Path


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def export_all(con, out_dir: Path):
    run_times = {r["id"]: r["started_at"] for r in con.execute("SELECT id, started_at FROM runs")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM entities")}

    def docs_for(url):
        rows = con.execute(
            """SELECT l.to_url, l.text, e.data FROM links l LEFT JOIN entities e ON e.id = 'file:' || l.to_url
               WHERE l.from_url = ? AND l.context = 'content' AND l.to_url IN
                 (SELECT url FROM documents WHERE kind = 'file')""", (url,)).fetchall()
        out = []
        for r in rows:
            meta = (json.loads(r["data"]).get("document") or {}) if r["data"] else {}
            out.append({"url": r["to_url"], "link_text": r["text"], "title": meta.get("title"),
                        "document_type": meta.get("document_type"), "document_date": meta.get("document_date")})
        return out

    projects = []
    for r in con.execute("SELECT data FROM entities WHERE type = 'project' ORDER BY name"):
        rec = json.loads(r["data"])
        rec["documents"] = docs_for(rec["url"])
        projects.append(rec)

    news = []
    for r in con.execute("SELECT data FROM entities WHERE type = 'article'"):
        rec = json.loads(r["data"])
        if rec.get("in_range"):
            rec["documents"] = docs_for(rec["url"])
            news.append(rec)
    news.sort(key=lambda a: a.get("published_at") or "", reverse=True)

    facts = []
    for f in con.execute("SELECT * FROM facts ORDER BY entity_id, field, valid_from_run"):
        facts.append({
            "id": f["id"],
            "entity_id": f["entity_id"],
            "entity_name": names.get(f["entity_id"]),
            "field": f["field"],
            "value": json.loads(f["value"]),
            "source_url": f["source_url"],
            "page": f["page"],
            "excerpt": f["excerpt"],
            "locator": f["locator"],
            "method": f["method"],
            "model": f["model"],
            "prompt_version": f["prompt_version"],
            "effective_at": f["effective_at"],
            "observed_at": run_times.get(f["valid_from_run"]),
            "valid_to": run_times.get(f["valid_to_run"]),
            "current": f["valid_to_run"] is None,
            "superseded_by": f["superseded_by"],
        })

    files = []
    for d in con.execute(
            """SELECT d.*, i.pages, i.status AS text_status, i.error AS text_error, e.data AS meta
               FROM documents d LEFT JOIN pdf_info i ON i.sha256 = d.sha256
               LEFT JOIN entities e ON e.id = 'file:' || d.url WHERE d.kind = 'file' ORDER BY d.url"""):
        links = con.execute("SELECT from_url, text, context FROM links WHERE to_url = ?", (d["url"],)).fetchall()
        meta = json.loads(d["meta"]) if d["meta"] else {}
        files.append({
            "url": d["url"], "status": d["status"], "error": d["error"] or d["text_error"],
            "sha256": d["sha256"], "content_type": d["content_type"], "last_modified": d["last_modified"],
            "pages": d["pages"], "text_status": d["text_status"],
            "about": meta.get("about"), "document": meta.get("document"),
            "linked_from": [dict(l) for l in links],
        })

    # skipped / blocked / failed urls from the latest crawl
    last = con.execute("SELECT MAX(run_id) FROM crawl_log").fetchone()[0]
    crawl_log = [dict(r) for r in con.execute(
        "SELECT url, kind, outcome, reason, found_on FROM crawl_log WHERE run_id = ? ORDER BY outcome, kind, url",
        (last,))]

    counts = {
        "crawl_log.jsonl": write_jsonl(out_dir / "crawl_log.jsonl", crawl_log),
        "projects.jsonl": write_jsonl(out_dir / "projects.jsonl", projects),
        "news.jsonl": write_jsonl(out_dir / "news.jsonl", news),
        "facts.jsonl": write_jsonl(out_dir / "facts.jsonl", facts),
        "files.jsonl": write_jsonl(out_dir / "files.jsonl", files),
    }
    return counts


def run_report(con, run_id, extra):
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    req = con.execute("SELECT status, error, bytes FROM requests WHERE run_id = ?", (run_id,)).fetchall()
    by_status = Counter(str(r["status"]) if r["status"] else "error" for r in req)
    outcomes = Counter((r["kind"], r["outcome"]) for r in
                       con.execute("SELECT kind, outcome FROM crawl_log WHERE run_id = ?", (run_id,)))
    cur = con.execute("SELECT method, COUNT(*) n FROM facts WHERE valid_to_run IS NULL GROUP BY method").fetchall()
    return {
        "run_id": run_id,
        "started_at": run["started_at"],
        "args": json.loads(run["args"] or "{}"),
        **extra,
        "requests": {"total": len(req), "by_status": dict(by_status),
                     "bytes_downloaded": sum(r["bytes"] or 0 for r in req)},
        "crawl_log": {f"{k}:{o}": n for (k, o), n in sorted(outcomes.items())},
        "facts_current": {r["method"]: r["n"] for r in cur},
    }
