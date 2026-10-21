"""Build the saved-copy fixtures used for the variant demos (unchanged / changed source / failed file).

Takes one project page and its files out of an existing crawl (data/) and writes three fixture dirs:
  fixtures/base            - exactly as downloaded
  fixtures/changed_source  - CMS phase edited on the page + one PDF amended with an extra sentence
  fixtures/failed_file     - one PDF corrupted, the other returns HTTP 500

    uv run python scripts/make_fixtures.py
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pymupdf

PAGE = "https://createto.ca/projects/st-lawrence-centre-for-the-arts"
DATA = Path("data")
OUT = Path("fixtures")

con = sqlite3.connect(DATA / "createto.db")
con.row_factory = sqlite3.Row


def blob(url):
    sha = con.execute("SELECT sha256 FROM documents WHERE url = ?", (url,)).fetchone()["sha256"]
    return (DATA / "blobs" / sha[:2] / sha).read_bytes()


def write(name, entries):
    d = OUT / name
    shutil.rmtree(d, ignore_errors=True)
    (d / "files").mkdir(parents=True)
    manifest = {}
    for i, (url, body, ctype, status) in enumerate(entries):
        entry = {"status": status, "content_type": ctype}
        if body is not None:
            fname = f"files/{i:02d}_{url.rstrip('/').rsplit('/', 1)[-1][:60]}"
            (d / fname).write_bytes(body)
            entry["file"] = fname
        manifest[url] = entry
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {d} ({len(entries)} urls)")


files = [r["to_url"] for r in con.execute(
    "SELECT l.to_url FROM links l JOIN documents d ON d.url = l.to_url AND d.kind = 'file' "
    "WHERE l.from_url = ? ORDER BY l.to_url", (PAGE,))]
html = blob(PAGE)
pdfs = [blob(u) for u in files]

# 1. as downloaded
write("base", [(PAGE, html, "text/html", 200)] + [(u, b, "application/pdf", 200) for u, b in zip(files, pdfs)])

# 2. the page moves to the next phase, and the first PDF gets amended
changed_html = html.replace(b'"phase":"develomentConcept"', b'"phase":"preConstruction"', 1)
assert changed_html != html, "phase string not found in page"
doc = pymupdf.open(stream=pdfs[0], filetype="pdf")
doc[0].insert_text((50, 40), "AMENDED: Construction is now expected to begin in Spring 2028.", fontsize=10)
amended = doc.tobytes()
write("changed_source", [(PAGE, changed_html, "text/html", 200),
                         (files[0], amended, "application/pdf", 200),
                         (files[1], pdfs[1], "application/pdf", 200)])

# 3. same as (2) but the amended PDF now comes back truncated/corrupt and the other one errors
write("failed_file", [(PAGE, changed_html, "text/html", 200),
                      (files[0], amended[: len(amended) // 3], "application/pdf", 200),
                      (files[1], None, "application/pdf", 500)])
