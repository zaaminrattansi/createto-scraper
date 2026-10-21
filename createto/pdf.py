import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)


def extract_texts(con, blobs, run_id):
    """Pull per-page text out of every downloaded PDF we haven't processed yet (keyed by sha256)."""
    rows = con.execute(
        """SELECT d.sha256, GROUP_CONCAT(d.url, ' ') AS urls FROM documents d
           LEFT JOIN pdf_info p ON p.sha256 = d.sha256
           WHERE d.kind = 'file' AND d.sha256 IS NOT NULL AND p.sha256 IS NULL
           GROUP BY d.sha256"""
    ).fetchall()

    stats = {"processed": 0, "failed": 0, "no_text": 0, "skipped_type": 0}
    for row in rows:
        data = blobs.get(row["sha256"])
        if not data.startswith(b"%PDF"):
            # docx/xlsx etc. are stored but not parsed for now
            con.execute("INSERT INTO pdf_info VALUES (?, NULL, 'unsupported', NULL)", (row["sha256"],))
            stats["skipped_type"] += 1
            continue
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                # mupdf quietly "repairs" broken files; treat a repaired/empty doc as unreadable rather than
                # replacing good facts with whatever fragments survived
                pages = [page.get_text("text") for page in doc]
                if doc.is_repaired or not pages:
                    raise ValueError("damaged PDF (needed repair)" if doc.is_repaired else "PDF has no pages")
        except Exception as e:
            con.execute("INSERT INTO pdf_info VALUES (?, NULL, 'failed', ?)", (row["sha256"], str(e)[:500]))
            for url in row["urls"].split(" "):
                con.execute("UPDATE crawl_log SET outcome = 'failed', reason = ? WHERE run_id = ? AND url = ?",
                            (f"unreadable PDF: {e}"[:300], run_id, url))
            stats["failed"] += 1
            continue

        con.executemany("INSERT OR REPLACE INTO pdf_pages VALUES (?,?,?)",
                        [(row["sha256"], i + 1, t) for i, t in enumerate(pages)])
        # very little text usually means a scan; we'd need OCR / a vision model for those
        status = "ok" if sum(len(t.strip()) for t in pages) > 50 * len(pages) else "no_text"
        con.execute("INSERT INTO pdf_info VALUES (?,?,?,NULL)", (row["sha256"], len(pages), status))
        stats["processed"] += 1
        if status == "no_text":
            stats["no_text"] += 1
    con.commit()
    return stats
