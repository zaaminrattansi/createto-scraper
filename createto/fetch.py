import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db
from .config import SITE_HOSTS, USER_AGENT


@dataclass
class Result:
    url: str
    status: int | None
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    final_url: str | None = None
    error: str | None = None

    @property
    def ok(self):
        return self.status == 200

    @property
    def not_modified(self):
        return self.status == 304


class Fetcher:
    def __init__(self, cfg, con, run_id):
        self.cfg = cfg
        self.con = con
        self.run_id = run_id
        self.last_hit = {}  # host -> monotonic time of last request
        self.count = 0
        transport = replay_transport(cfg.replay_dir) if cfg.replay_dir else None
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=cfg.timeout,
            follow_redirects=True,
            transport=transport,
        )

    def _wait(self, host):
        if self.cfg.replay_dir:
            return
        delay = self.cfg.site_delay if host in SITE_HOSTS else self.cfg.file_delay
        elapsed = time.monotonic() - self.last_hit.get(host, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self.last_hit[host] = time.monotonic()

    def get(self, url, etag=None, last_modified=None, max_bytes=None) -> Result:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        host = urlsplit(url).hostname or ""
        res = None
        for attempt in range(4):
            self._wait(host)
            start = time.monotonic()
            res = self._get_once(url, headers, max_bytes)
            ms = int((time.monotonic() - start) * 1000)
            self.count += 1
            self.con.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                (self.run_id, url, res.status, len(res.body), ms, res.error, db.now()),
            )
            retryable = res.status in (429, 500, 502, 503, 504) or (res.status is None and "too large" not in (res.error or ""))
            if not retryable or attempt == 3:
                break
            time.sleep(2 ** (attempt + 1))
        return res

    def _get_once(self, url, headers, max_bytes):
        try:
            with self.client.stream("GET", url, headers=headers) as r:
                size = int(r.headers.get("content-length") or 0)
                if max_bytes and size > max_bytes:
                    return Result(url, None, error=f"too large ({size} bytes)")
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        return Result(url, None, error=f"too large (>{max_bytes} bytes)")
                    chunks.append(chunk)
                return Result(
                    url,
                    r.status_code,
                    b"".join(chunks),
                    etag=r.headers.get("etag"),
                    last_modified=r.headers.get("last-modified"),
                    content_type=(r.headers.get("content-type") or "").split(";")[0].strip() or None,
                    final_url=str(r.url),
                )
        except httpx.HTTPError as e:
            return Result(url, None, error=f"{type(e).__name__}: {e}")

    def close(self):
        self.client.close()


def replay_transport(fixture_dir: Path):
    """Serve saved copies instead of hitting the network.

    fixture_dir/manifest.json maps url -> {"file": ..., "status": ..., "content_type": ...}.
    ETags are the sha256 of the file, and If-None-Match is honoured, so re-runs behave like the real site.
    """
    manifest = json.loads((fixture_dir / "manifest.json").read_text())

    def handler(request: httpx.Request):
        entry = manifest.get(str(request.url))
        if entry is None:
            return httpx.Response(404, text="not in fixture")
        status = entry.get("status", 200)
        if status != 200:
            return httpx.Response(status)
        body = (fixture_dir / entry["file"]).read_bytes()
        etag = f'"{db.sha256(body)[:16]}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=body, headers={"etag": etag, "content-type": entry.get("content_type", "")})

    return httpx.MockTransport(handler)
