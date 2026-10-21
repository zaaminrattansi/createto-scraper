import json
import time
from datetime import datetime
from pathlib import Path

import typer

from . import db
from .config import Config
from .crawl import Crawler
from .export import export_all, run_report
from .extract import run_extraction
from .fetch import Fetcher
from .parse import parse_pages
from .pdf import extract_texts

app = typer.Typer(add_completion=False, no_args_is_help=True)

DataDir = typer.Option(Path("data"), help="Where the db, raw blobs and exports go")


@app.callback()
def main():
    """Scrape createto.ca into a versioned, source-cited dataset."""


def _since(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def process(cfg, con, run_id, stats):
    """Everything after the crawl. Needs no network except the LLM calls."""
    blobs = db.Blobs(cfg.blob_dir)
    t = time.monotonic()
    stats["parse"] = parse_pages(con, blobs, run_id, cfg.since)
    stats["pdf_text"] = extract_texts(con, blobs, run_id)
    if cfg.use_llm:
        stats["llm"] = run_extraction(con, cfg, run_id)
    stats["export"] = export_all(con, cfg.data_dir / "export")
    stats["process_seconds"] = round(time.monotonic() - t, 1)


def finish(cfg, con, run_id, stats, started):
    stats["total_seconds"] = round(time.monotonic() - started, 1)
    report = run_report(con, run_id, stats)
    con.execute("UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?", (db.now(), json.dumps(report), run_id))
    con.commit()
    out = cfg.data_dir / "runs" / f"run_{run_id:03d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print_summary(report)
    print(f"\nfull report: {out}")


def print_summary(r):
    llm = r.get("llm", {})
    crawl = r.get("crawl", {})
    rows = [
        ("time", f"{r['total_seconds']}s"),
        ("requests", f"{r['requests']['total']}  {r['requests']['by_status']}"),
        ("downloaded", f"{r['requests']['bytes_downloaded'] / 1e6:.1f} MB"),
        ("pages", crawl.get("pages")),
        ("files", crawl.get("files")),
        ("skipped / blocked links", f"{crawl.get('skipped')} / {crawl.get('blocked')}"),
        ("PDFs text-extracted", r.get("pdf_text", {}).get("processed")),
        ("projects / articles", f"{r['parse']['projects']} / {r['parse']['articles']}"),
        ("facts added / closed", f"{r['parse']['facts_added'] + llm.get('facts_added', 0)} / "
                                 f"{r['parse']['facts_closed'] + llm.get('facts_closed', 0)}"),
        ("current facts", r["facts_current"]),
        ("LLM calls (cache hits)", f"{llm.get('llm_calls', 0)} ({llm.get('cache_hits', 0)})"),
        ("LLM cost", f"${llm.get('cost_usd', 0):.4f}"),
    ]
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"{k:<{w}}  {v}")


@app.command()
def run(
    data_dir: Path = DataDir,
    replay: Path = typer.Option(None, help="Serve responses from a saved fixture dir instead of the network"),
    seed: str = typer.Option(None, help="Only crawl this page (and its files) instead of the whole site"),
    since: str = typer.Option(None, help="Only keep news / extract files from this date on (YYYY-MM-DD). "
                                         "Default: 5 years ago"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM extraction step"),
    max_llm_docs: int = typer.Option(None, help="Cap how many files go to the LLM (for testing)"),
    model: str = typer.Option(None, help="OpenRouter model id"),
):
    """Full pipeline: crawl -> parse -> PDF text -> LLM facts -> export."""
    cfg = Config(data_dir=data_dir, replay_dir=replay, use_llm=not no_llm, max_llm_docs=max_llm_docs)
    if seed:
        cfg.start_url, cfg.follow_links = seed, False
    if since:
        cfg.since = _since(since)
    if model:
        cfg.model = model

    started = time.monotonic()
    con = db.connect(cfg.db_path)
    run_id = db.start_run(con, {"command": "run", "replay": replay, "seed": seed, "since": cfg.since,
                                "model": cfg.model if cfg.use_llm else None})
    fetcher = Fetcher(cfg, con, run_id)
    try:
        stats = {"crawl": Crawler(cfg, con, db.Blobs(cfg.blob_dir), fetcher, run_id).run()}
    finally:
        fetcher.close()
        con.commit()
    process(cfg, con, run_id, stats)
    finish(cfg, con, run_id, stats, started)


@app.command()
def reprocess(
    data_dir: Path = DataDir,
    since: str = typer.Option(None),
    no_llm: bool = typer.Option(False, "--no-llm"),
    max_llm_docs: int = typer.Option(None),
    model: str = typer.Option(None),
):
    """Re-run parse / extraction / export over what's already downloaded (no crawling)."""
    cfg = Config(data_dir=data_dir, use_llm=not no_llm, max_llm_docs=max_llm_docs)
    if since:
        cfg.since = _since(since)
    if model:
        cfg.model = model
    started = time.monotonic()
    con = db.connect(cfg.db_path)
    run_id = db.start_run(con, {"command": "reprocess", "since": cfg.since,
                                "model": cfg.model if cfg.use_llm else None})
    stats = {}
    process(cfg, con, run_id, stats)
    finish(cfg, con, run_id, stats, started)


@app.command()
def export(data_dir: Path = DataDir):
    """Just rewrite the export files from the db."""
    con = db.connect(Config(data_dir=data_dir).db_path)
    print(export_all(con, data_dir / "export"))


if __name__ == "__main__":
    app()
