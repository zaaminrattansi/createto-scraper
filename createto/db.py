import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT, finished_at TEXT, args TEXT, stats TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    run_id INT, url TEXT, status INT, bytes INT, ms INT, error TEXT, at TEXT
);
-- what happened to each url in each run, incl. skipped/blocked links
CREATE TABLE IF NOT EXISTS crawl_log (
    run_id INT, url TEXT, kind TEXT, outcome TEXT, reason TEXT, found_on TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY, kind TEXT, sha256 TEXT, etag TEXT, last_modified TEXT, content_type TEXT,
    status TEXT, error TEXT, first_run INT, last_run INT, changed_run INT
);
-- every distinct version of every page/file we've downloaded
CREATE TABLE IF NOT EXISTS document_versions (
    url TEXT, sha256 TEXT, run_id INT, fetched_at TEXT, bytes INT,
    PRIMARY KEY (url, sha256)
);
CREATE TABLE IF NOT EXISTS links (
    from_url TEXT, to_url TEXT, text TEXT, context TEXT,
    PRIMARY KEY (from_url, to_url)
);
CREATE TABLE IF NOT EXISTS pdf_pages (
    sha256 TEXT, page INT, text TEXT, PRIMARY KEY (sha256, page)
);
CREATE TABLE IF NOT EXISTS pdf_info (
    sha256 TEXT PRIMARY KEY, pages INT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, type TEXT, name TEXT, url TEXT, data TEXT, first_run INT, last_run INT
);
-- a fact is current while valid_to_run is null. scope = the thing that produced it (a cms story or a file)
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    scope TEXT, entity_id TEXT, field TEXT, value TEXT,
    source_url TEXT, source_sha256 TEXT, page INT, excerpt TEXT, locator TEXT,
    method TEXT, model TEXT, prompt_version TEXT,
    effective_at TEXT,
    valid_from_run INT, valid_to_run INT, superseded_by INT
);
CREATE INDEX IF NOT EXISTS facts_scope ON facts (scope, valid_to_run);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY, run_id INT, cache_key TEXT, model TEXT, sha256 TEXT, pages TEXT,
    prompt_tokens INT, completion_tokens INT, cost REAL, ms INT, response TEXT, error TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS llm_cache ON llm_calls (cache_key);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


class Blobs:
    """Raw bytes on disk, addressed by sha256."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, sha):
        return self.root / sha[:2] / sha

    def put(self, data: bytes):
        h = sha256(data)
        p = self.path(h)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return h

    def get(self, sha):
        p = self.path(sha)
        return p.read_bytes() if p.exists() else None


def start_run(con, args):
    cur = con.execute("INSERT INTO runs (started_at, args) VALUES (?, ?)", (now(), json.dumps(args, default=str)))
    con.commit()
    return cur.lastrowid


def save_document(con, run_id, url, kind, sha, etag, last_modified, content_type, size):
    """Upsert the latest state for a url. Returns new | changed | unchanged."""
    prev = con.execute("SELECT sha256 FROM documents WHERE url = ?", (url,)).fetchone()
    if prev is None or prev["sha256"] is None:
        outcome = "new"
    elif prev["sha256"] != sha:
        outcome = "changed"
    else:
        outcome = "unchanged"

    con.execute("INSERT OR IGNORE INTO document_versions VALUES (?,?,?,?,?)", (url, sha, run_id, now(), size))
    con.execute(
        """INSERT INTO documents VALUES (?,?,?,?,?,?, 'ok', NULL, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
             sha256 = excluded.sha256, etag = excluded.etag, last_modified = excluded.last_modified,
             content_type = excluded.content_type, status = 'ok', error = NULL, last_run = excluded.last_run,
             changed_run = CASE WHEN documents.sha256 IS excluded.sha256 THEN documents.changed_run
                                ELSE excluded.changed_run END""",
        (url, kind, sha, etag, last_modified, content_type, run_id, run_id, run_id),
    )
    return outcome


def mark_failed(con, run_id, url, kind, error):
    # keep the old sha256 if we had one, a single bad fetch shouldn't wipe out data
    con.execute(
        """INSERT INTO documents (url, kind, status, error, first_run, last_run) VALUES (?,?, 'failed', ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET status = 'failed', error = excluded.error, last_run = excluded.last_run""",
        (url, kind, error, run_id, run_id),
    )


def log(con, run_id, url, kind, outcome, reason=None, found_on=None):
    con.execute("INSERT OR REPLACE INTO crawl_log VALUES (?,?,?,?,?,?)", (run_id, url, kind, outcome, reason, found_on))


def upsert_entity(con, run_id, id, type, name, url, data):
    con.execute(
        """INSERT INTO entities VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET type=excluded.type, name=excluded.name, url=excluded.url,
             data=excluded.data, last_run=excluded.last_run""",
        (id, type, name, url, json.dumps(data, default=str), run_id, run_id),
    )


def sync_facts(con, run_id, scope, new_facts):
    """Make the current facts for `scope` equal to `new_facts`.

    Facts that are no longer produced get closed (valid_to_run = this run), new ones get inserted.
    If a field's value changed we also link old -> new via superseded_by so the history is easy to follow.
    Returns (added, closed).
    """
    current = con.execute("SELECT * FROM facts WHERE scope = ? AND valid_to_run IS NULL", (scope,)).fetchall()

    def key(f):
        return (f["entity_id"], f["field"], f["value"])

    new_by_key = {}
    for f in new_facts:
        f = {**f, "value": json.dumps(f["value"], sort_keys=True, default=str)}
        new_by_key.setdefault(key(f), f)

    cur_keys = {key(r): r for r in current}
    to_close = [r for k, r in cur_keys.items() if k not in new_by_key]
    to_add = [f for k, f in new_by_key.items() if k not in cur_keys]

    added_ids = {}  # (entity, field) -> [new fact ids]
    for f in to_add:
        c = con.execute(
            """INSERT INTO facts (scope, entity_id, field, value, source_url, source_sha256, page, excerpt, locator,
                                  method, model, prompt_version, effective_at, valid_from_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scope, f["entity_id"], f["field"], f["value"], f["source_url"], f.get("source_sha256"), f.get("page"),
             f.get("excerpt"), f.get("locator"), f["method"], f.get("model"), f.get("prompt_version"),
             f.get("effective_at"), run_id),
        )
        added_ids.setdefault((f["entity_id"], f["field"]), []).append(c.lastrowid)

    # only link old -> new when it's one value replacing one value (a project's phase, ward, ...). a PDF can
    # give ten milestones and there's no way to tell which new one replaced which old one
    closing = Counter((r["entity_id"], r["field"]) for r in to_close)
    for r in to_close:
        k = (r["entity_id"], r["field"])
        new = added_ids.get(k, [])
        superseded_by = new[0] if len(new) == 1 and closing[k] == 1 else None
        con.execute("UPDATE facts SET valid_to_run = ?, superseded_by = ? WHERE id = ?", (run_id, superseded_by, r["id"]))
    return len(to_add), len(to_close)
