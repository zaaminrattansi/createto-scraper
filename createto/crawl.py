import json
import re
from collections import deque
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import db
from .config import FILE_EXTS, MEDIA_EXTS, SITE_HOSTS

NEXT_DATA_RE = re.compile(rb'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)


def normalize(url, base=None):
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    path = quote(unquote(parts.path), safe="/:@!$&'()*+,;=-._~") or "/"  # so "a b" and "a%20b" match
    if host in SITE_HOSTS:
        host = "createto.ca"
        if len(path) > 1:
            path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")])
    return urlunsplit(("https" if host in SITE_HOSTS else parts.scheme, host, path, query, ""))


def classify(url):
    parts = urlsplit(url)
    ext = PurePosixPath(parts.path).suffix.lower()
    if ext in MEDIA_EXTS:
        return "media"
    if ext in FILE_EXTS:
        return "file"
    if parts.hostname in SITE_HOSTS:
        if parts.path.startswith(("/_next/", "/api/")):
            return "asset"
        return "page"
    return "external"


def next_data(html: bytes):
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ---- link extraction -------------------------------------------------------
#
# Storyblok gives us links in a few shapes: multilink fields ({linktype, url, cached_url}), asset fields
# ({fieldtype: asset, filename}), and rich text link marks ({type: link, attrs: {href}}). We walk the
# JSON and pick them all up, plus <a href> from the rendered HTML as a fallback.
#
# context matters for attribution: links inside the page's own story are "content", links in
# related/similar teasers, header, footer etc. are "related". Only content links tie a file to a project.

def _walk(obj, ctx, label, out, root=True):
    if isinstance(obj, dict):
        # a story embedded inside another one (e.g. an article's relatedProjects) isn't this page's content
        if not root and "full_slug" in obj and "content" in obj:
            ctx = "related"
        label = obj.get("title") or obj.get("label") or obj.get("name") or obj.get("text") or label
        if not isinstance(label, str):
            label = None
        lt = obj.get("linktype")
        if lt == "story" and obj.get("cached_url"):
            slug = (obj.get("story") or {}).get("full_slug") or obj["cached_url"]
            out.append(("/" + slug.lstrip("/"), label, ctx))
        elif lt in ("url", "asset") and obj.get("url"):
            out.append((obj["url"], label, ctx))
        if obj.get("fieldtype") == "asset" and obj.get("filename"):
            out.append((obj["filename"], obj.get("title") or obj.get("alt") or obj.get("name") or label, ctx))
        if obj.get("type") == "link" and isinstance(obj.get("attrs"), dict) and obj["attrs"].get("href"):
            out.append((obj["attrs"]["href"], label, ctx))
        if obj.get("type") == "text" and isinstance(obj.get("marks"), list):
            for mark in obj["marks"]:
                if mark.get("type") == "link" and (mark.get("attrs") or {}).get("href"):
                    out.append((mark["attrs"]["href"], obj.get("text"), ctx))
        for k, v in obj.items():
            if k != "marks":
                _walk(v, ctx, label, out, False)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, ctx, label, out, False)


def extract_links(html: bytes, page_url: str):
    found = []
    data = next_data(html)
    if data:
        props = data.get("props", {}).get("pageProps", {})
        _walk(props.get("page"), "content", None, found)
        _walk({k: v for k, v in props.items() if k != "page"}, "related", None, found)

    tree = HTMLParser(html)
    for a in tree.css("a[href]"):
        found.append((a.attributes.get("href") or "", a.text(strip=True), "html"))

    rank = {"content": 0, "related": 1, "html": 2}
    links = {}
    for href, text, ctx in found:
        url = normalize(href, page_url) if href and not href.startswith(("mailto:", "tel:", "#")) else None
        if not url or url == page_url:
            continue
        prev = links.get(url)
        if prev is None or rank[ctx] < rank[prev[1]]:
            links[url] = (text or (prev[0] if prev else None), ctx)
        elif not prev[0] and text:
            links[url] = (text, prev[1])
    return [(u, t, c) for u, (t, c) in links.items()]


# ---- crawl -------------------------------------------------------------------

class Crawler:
    def __init__(self, cfg, con, blobs, fetcher, run_id):
        self.cfg = cfg
        self.con = con
        self.blobs = blobs
        self.fetcher = fetcher
        self.run_id = run_id
        self.robots = {}
        self.stats = {"pages": {}, "files": {}, "skipped": 0, "blocked": 0}

    def bump(self, kind, outcome):
        self.stats[kind][outcome] = self.stats[kind].get(outcome, 0) + 1

    def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            rp = RobotFileParser()
            res = self.fetcher.get(origin + "/robots.txt")
            # no robots.txt (or an error fetching it) means no restrictions
            rp.parse(res.body.decode("utf-8", "replace").splitlines() if res.ok else [])
            self.robots[origin] = rp
        return self.robots[origin].can_fetch("*", url)

    def sitemap_urls(self):
        urls, todo, seen = [], [urljoin(self.cfg.start_url, "/sitemap.xml")], set()
        while todo:
            sm = todo.pop()
            if sm in seen:
                continue
            seen.add(sm)
            res = self.fetcher.get(sm)
            if not res.ok:
                continue
            locs = re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", res.body)
            for loc in (l.decode() for l in locs):
                if loc.endswith(".xml"):
                    todo.append(loc)
                else:
                    urls.append(loc)
        return urls

    def run(self):
        start = normalize(self.cfg.start_url)
        queue = deque([start])
        seen, files, found_on = set(), {}, {}
        if self.cfg.follow_links:
            for u in filter(None, (normalize(u) for u in self.sitemap_urls())):
                queue.append(u)
                found_on.setdefault(u, "sitemap.xml")
            # re-check pages from earlier runs even if nothing links to them now, so a removed page shows up
            # as a 404 (and its facts get closed) instead of silently staying current
            for r in self.con.execute("SELECT url FROM documents WHERE kind = 'page'").fetchall():
                queue.append(r["url"])
                found_on.setdefault(r["url"], "previous run")

        while queue:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            links = self.fetch_page(url, found_on.get(url))
            for link, _text, _ctx in links:
                kind = classify(link)
                if kind == "page":
                    if self.cfg.follow_links and link not in seen:
                        queue.append(link)
                        found_on.setdefault(link, url)
                elif kind == "file":
                    files.setdefault(link, url)
                elif link not in seen:
                    seen.add(link)
                    reason = {"media": "image/video asset", "asset": "site asset",
                              "external": "external page (not crawled)"}[kind]
                    db.log(self.con, self.run_id, link, kind, "skipped", reason, url)
                    self.stats["skipped"] += 1
            self.con.commit()

        for url, found_on in files.items():
            self.fetch_file(url, found_on)
            self.con.commit()
        return self.stats

    def fetch_page(self, url, found_on=None):
        if not self.allowed(url):
            db.log(self.con, self.run_id, url, "page", "blocked", "robots.txt", found_on)
            self.stats["blocked"] += 1
            return []

        doc = self.con.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        have = doc and doc["sha256"] and self.blobs.get(doc["sha256"]) is not None
        res = self.fetcher.get(url, etag=doc["etag"] if have else None)

        if res.not_modified:
            body = self.blobs.get(doc["sha256"])
            self.con.execute("UPDATE documents SET last_run = ?, status = 'ok', error = NULL WHERE url = ?",
                             (self.run_id, url))
            outcome = "unchanged"
        elif res.ok:
            if res.content_type and "html" not in res.content_type:
                db.log(self.con, self.run_id, url, "page", "skipped", f"not html ({res.content_type})", found_on)
                self.stats["skipped"] += 1
                return []
            body = res.body
            sha = self.blobs.put(body)
            outcome = db.save_document(self.con, self.run_id, url, "page", sha, res.etag, res.last_modified,
                                       res.content_type, len(body))
        else:
            reason = res.error or f"HTTP {res.status}"
            db.mark_failed(self.con, self.run_id, url, "page", reason)
            db.log(self.con, self.run_id, url, "page", "failed", reason, found_on)
            self.bump("pages", "failed")
            return []

        db.log(self.con, self.run_id, url, "page", outcome, None, found_on)
        self.bump("pages", outcome)
        return extract_links(body, url)  # links table is (re)built in parse.py

    def fetch_file(self, url, found_on):
        if not self.allowed(url):
            db.log(self.con, self.run_id, url, "file", "blocked", "robots.txt", found_on)
            self.stats["blocked"] += 1
            return

        doc = self.con.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        have = doc and doc["sha256"] and self.blobs.get(doc["sha256"]) is not None
        res = self.fetcher.get(url, etag=doc["etag"] if have else None,
                               last_modified=doc["last_modified"] if have else None,
                               max_bytes=self.cfg.max_file_mb * 1024 * 1024)

        if res.not_modified:
            self.con.execute("UPDATE documents SET last_run = ?, status = 'ok', error = NULL WHERE url = ?",
                             (self.run_id, url))
            outcome = "unchanged"
        elif res.ok and res.content_type and "html" in res.content_type:
            # usually a login wall or a "document not found" page served with a 200
            outcome = None
            res.error = f"expected a file, got {res.content_type}"
        elif res.ok:
            sha = self.blobs.put(res.body)
            outcome = db.save_document(self.con, self.run_id, url, "file", sha, res.etag, res.last_modified,
                                       res.content_type, len(res.body))
        else:
            outcome = None

        if outcome is None:
            reason = res.error or f"HTTP {res.status}"
            db.mark_failed(self.con, self.run_id, url, "file", reason)
            db.log(self.con, self.run_id, url, "file", "failed", reason, found_on)
            self.bump("files", "failed")
            return
        db.log(self.con, self.run_id, url, "file", outcome, None, found_on)
        self.bump("files", outcome)
