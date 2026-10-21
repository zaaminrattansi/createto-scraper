"""Turn the Storyblok story embedded in each page (__NEXT_DATA__) into entities + facts.

No LLM here: CreateTO's CMS already has typed fields for projects and articles, so we just map them.
"""

from . import db
from .crawl import extract_links, next_data, normalize

PROJECT_FIELDS = {
    "address": "address",
    "ward": "ward",
    "councillorName": "councillor",
    "majorIntersection": "major_intersection",
    "siteArea": "site_area",
    "currentUse": "current_use",
    "proposedUse": "proposed_use",
    "surroundingUse": "surrounding_use",
    "delivery": "delivery",
    "summary": "summary",
}


def rich_text(node):
    """Flatten Storyblok rich text into plain text."""
    if isinstance(node, list):
        return "".join(rich_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    inner = rich_text(node.get("content", []))
    if node.get("type") in ("paragraph", "heading", "list_item", "blockquote"):
        return inner.strip() + "\n"
    return inner


def stats_from(blocks):
    out = []
    for block in blocks or []:
        for item in block.get("stats", []) if isinstance(block, dict) else []:
            if item.get("value") or item.get("label"):
                out.append({"label": item.get("label"), "value": item.get("value")})
    return out


def parse_project(page, props, url):
    c = page["content"]
    phases = props.get("projectPhases") or {}
    types = props.get("projectTypes") or {}
    rec = {
        "id": f"project:{page['uuid']}",
        "name": page.get("name"),
        "slug": page.get("full_slug"),
        "url": url,
        "phase": phases.get(c.get("phase"), c.get("phase")) or None,
        "phase_code": c.get("phase") or None,
        "types": [types.get(t, t) for t in c.get("type") or []],
        "initiatives": [i.get("name") for i in c.get("initiatives") or [] if isinstance(i, dict)],
        "parent_project": c.get("parentProject") or None,
        "location": None,
        "stats": stats_from(c.get("stats")),
        "description": rich_text(c.get("body")).strip() or None,
        "first_published_at": page.get("first_published_at"),
        "updated_at": page.get("published_at"),
        "cms_id": page.get("id"),
    }
    for src, dst in PROJECT_FIELDS.items():
        v = c.get(src)
        rec[dst] = v.strip() if isinstance(v, str) and v.strip() else None
    try:
        rec["location"] = {"lat": float(c["latitude"]), "lng": float(c["longitude"])}
    except (KeyError, TypeError, ValueError):
        pass
    if c.get("timeline"):
        rec["timeline"] = c["timeline"]  # TODO: haven't seen a populated one yet
    return rec


def parse_article(page, props, url):
    c = page["content"]
    rec = {
        "id": f"article:{page['uuid']}",
        "title": c.get("title") or page.get("name"),
        "slug": page.get("full_slug"),
        "url": url,
        "published_at": page.get("first_published_at"),
        "updated_at": page.get("published_at"),
        "topics": [t.get("name") for t in c.get("topics") or [] if isinstance(t, dict)],
        "importance": c.get("importance") or None,
        "event_starts_at": c.get("eventStartsAt") or None,
        "event_ends_at": c.get("eventEndsAt") or None,
        "external_url": (c.get("externalUrl") or {}).get("url") if isinstance(c.get("externalUrl"), dict)
        else (c.get("externalUrl") or None),
        "related_projects": [f"project:{p['uuid']}" for p in c.get("relatedProjects") or [] if isinstance(p, dict)],
        "initiatives": [i.get("name") for i in c.get("initiatives") or [] if isinstance(i, dict)],
        "body": rich_text(c.get("body")).strip() or None,
        "cms_id": page.get("id"),
    }
    return rec


# fields we turn into versioned facts (the rest just ride along on the entity record)
PROJECT_FACTS = ["name", "phase", "address", "location", "ward", "councillor", "major_intersection", "site_area",
                 "current_use", "proposed_use", "surrounding_use", "delivery", "types", "initiatives", "stats"]
ARTICLE_FACTS = ["title", "published_at", "topics", "event_starts_at", "event_ends_at", "related_projects"]


def external_articles(obj, out):
    """News cards that just link off-site (e.g. toronto.ca releases). They have no page of their own on
    createto.ca, so the cards embedded in listing/teaser blocks are the only place we see them."""
    if isinstance(obj, dict):
        if obj.get("isExternal") and obj.get("id") and obj.get("href") and obj.get("title"):
            out[obj["id"]] = obj
        for v in obj.values():
            external_articles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            external_articles(v, out)


def cms_facts(rec, fields, url, sha, effective_at):
    return [
        {"entity_id": rec["id"], "field": f, "value": rec[f], "source_url": url, "source_sha256": sha,
         "locator": f"__NEXT_DATA__ story field '{f}'", "method": "cms", "effective_at": effective_at}
        for f in fields
        if rec.get(f) not in (None, "", [], {})
    ]


def parse_pages(con, blobs, run_id, since):
    stats = {"projects": 0, "articles": 0, "articles_out_of_range": 0, "pages": 0, "facts_added": 0,
             "facts_closed": 0}
    rows = con.execute(
        "SELECT url, sha256, status, error FROM documents WHERE kind = 'page' AND sha256 IS NOT NULL").fetchall()
    page_urls = {r["url"] for r in rows}
    externals = {}  # card id -> (card, page it was seen on, page sha)
    for row in rows:
        url, sha = row["url"], row["sha256"]
        scope = f"cms:{url}"
        if row["status"] == "failed" and row["error"] in ("HTTP 404", "HTTP 410"):
            # the page is gone: close out whatever we knew from it, and drop its links so files it used to
            # link to don't stay attached to it
            a, c = db.sync_facts(con, run_id, scope, [])
            stats["facts_closed"] += c
            con.execute("DELETE FROM links WHERE from_url = ?", (url,))
            con.execute("UPDATE entities SET data = json_set(data, '$.removed_in_run', ?) "
                        "WHERE url = ? AND json_extract(data, '$.removed_in_run') IS NULL", (run_id, url))
            continue

        html = blobs.get(sha)
        links = extract_links(html, url)
        con.execute("DELETE FROM links WHERE from_url = ?", (url,))
        con.executemany("INSERT OR IGNORE INTO links VALUES (?,?,?,?)", [(url, u, t, c) for u, t, c in links])

        data = next_data(html)
        props = (data or {}).get("props", {}).get("pageProps", {})
        page = props.get("page") or {}
        component = (page.get("content") or {}).get("component")

        cards = {}
        external_articles(props, cards)
        for cid, card in cards.items():
            externals.setdefault(cid, (card, url, sha))

        # some urls render another page's story (e.g. /projects/bloor-kipling/block-5 -> bloor-kipling).
        # only take facts from the story's own url so we don't double count
        canonical = normalize(f"https://createto.ca/{page.get('full_slug', '')}")
        if component in ("project", "article") and canonical != url and canonical in page_urls:
            _, c = db.sync_facts(con, run_id, scope, [])
            stats["facts_closed"] += c
            stats["aliases"] = stats.get("aliases", 0) + 1
            continue

        facts = []
        if component == "project":
            rec = parse_project(page, props, url)
            db.upsert_entity(con, run_id, rec["id"], "project", rec["name"], url, rec)
            facts = cms_facts(rec, PROJECT_FACTS, url, sha, rec["updated_at"])
            stats["projects"] += 1
        elif component == "article":
            rec = parse_article(page, props, url)
            in_range = (rec["published_at"] or "") >= since.isoformat()
            rec["in_range"] = in_range
            db.upsert_entity(con, run_id, rec["id"], "article", rec["title"], url, rec)
            if in_range:
                facts = cms_facts(rec, ARTICLE_FACTS, url, sha, rec["published_at"])
                stats["articles"] += 1
            else:
                stats["articles_out_of_range"] += 1
        else:
            name = page.get("name") or (props.get("meta") or {}).get("title")
            eid = f"page:{page['uuid']}" if page.get("uuid") else f"page:{url}"
            db.upsert_entity(con, run_id, eid, component or "page", name, url,
                             {"id": eid, "name": name, "url": url, "component": component})
            stats["pages"] += 1

        a, c = db.sync_facts(con, run_id, scope, facts)
        stats["facts_added"] += a
        stats["facts_closed"] += c

    stats["external_articles"] = 0
    for cid, (card, seen_on, sha) in externals.items():
        rec = {
            "id": f"article:{cid}", "title": card["title"], "url": card["href"], "external": True,
            "seen_on": seen_on, "published_at": card.get("createdAt"), "updated_at": card.get("updatedAt"),
            "excerpt": card.get("excerpt") or None, "initiatives": card.get("initiatives") or [],
        }
        rec["in_range"] = (rec["published_at"] or "") >= since.isoformat()
        db.upsert_entity(con, run_id, rec["id"], "article", rec["title"], card["href"], rec)
        if rec["in_range"]:
            facts = [{"entity_id": rec["id"], "field": f, "value": rec[f], "source_url": seen_on, "source_sha256": sha,
                      "locator": "news card in __NEXT_DATA__", "method": "cms", "effective_at": rec["published_at"]}
                     for f in ("title", "published_at") if rec.get(f)]
            a, c = db.sync_facts(con, run_id, f"card:{cid}", facts)
            stats["facts_added"] += a
            stats["facts_closed"] += c
            stats["external_articles"] += 1
    con.commit()
    return stats
