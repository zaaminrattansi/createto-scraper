import json

from createto import db
from createto.crawl import classify, extract_links, normalize
from createto.extract import file_year, verify


def fact(value, field="phase"):
    return {"entity_id": "project:x", "field": field, "value": value, "source_url": "https://createto.ca/projects/x",
            "method": "cms"}


def test_sync_facts_tracks_changes(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert db.sync_facts(con, 1, "cms:x", [fact("Pre-development"), fact("Ward 4", "ward")]) == (2, 0)
    # same facts again -> nothing happens
    assert db.sync_facts(con, 2, "cms:x", [fact("Pre-development"), fact("Ward 4", "ward")]) == (0, 0)
    # phase changes -> old one closed and linked to the new one
    assert db.sync_facts(con, 3, "cms:x", [fact("Market Offering"), fact("Ward 4", "ward")]) == (1, 1)

    rows = con.execute("SELECT * FROM facts WHERE field = 'phase' ORDER BY id").fetchall()
    old, new = rows
    assert json.loads(old["value"]) == "Pre-development" and old["valid_to_run"] == 3
    assert old["superseded_by"] == new["id"] and new["valid_to_run"] is None


def test_sync_facts_closes_everything_when_source_disappears(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.sync_facts(con, 1, "cms:x", [fact("A"), fact("B", "ward")])
    assert db.sync_facts(con, 2, "cms:x", []) == (0, 2)


def test_verify_excerpt():
    pages = {1: "Intro", 3: "The reference concept project is just over 900 units,\nwith a third being affordable."}
    assert verify("just over 900 units, with a third", 3, pages) == (3, "exact")
    assert verify("just over 900 units, with a third", 2, pages) == (3, "exact")  # off-by-one page
    assert verify("just over 900 unit with a thrid", 3, pages)[1] == "fuzzy"
    assert verify("the project has 5000 units", 3, pages) is None


def test_normalize_and_classify():
    assert normalize("/news/a/?utm_source=x#top", "https://www.createto.ca/") == "https://createto.ca/news/a"
    assert normalize("https://createto.ca/a b") == normalize("https://createto.ca/a%20b")
    assert normalize("mailto:someone@example.com") is None
    assert classify("https://a-us.storyblok.com/f/1/x/abc/report.PDF") == "file"
    assert classify("https://a-us.storyblok.com/f/1/1000x500/abc/pic.jpg") == "media"
    assert classify("https://createto.ca/projects/x") == "page"
    assert classify("https://www.toronto.ca/news/") == "external"


def test_extract_links_context():
    data = {"props": {"pageProps": {
        "page": {"content": {"resources": [{"title": "Info sheet", "file": {
            "fieldtype": "asset", "filename": "https://a-us.storyblok.com/f/1/x/a/info.pdf"}}],
            "relatedProjects": [{"full_slug": "projects/other", "content": {"doc": {
                "fieldtype": "asset", "filename": "https://a-us.storyblok.com/f/1/x/b/other.pdf"}}}]}},
        "footer": {"link": {"linktype": "story", "cached_url": "about-us"}},
    }}}
    html = (b'<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data).encode() +
            b'</script><a href="/contact-us">Contact</a></html>')
    links = {u: (t, c) for u, t, c in extract_links(html, "https://createto.ca/projects/x")}
    assert links["https://a-us.storyblok.com/f/1/x/a/info.pdf"] == ("Info sheet", "content")
    assert links["https://a-us.storyblok.com/f/1/x/b/other.pdf"][1] == "related"
    assert links["https://createto.ca/about-us"][1] == "related"
    assert links["https://createto.ca/contact-us"] == ("Contact", "html")


def test_file_year():
    assert file_year("https://a-us.storyblok.com/f/1/x/a/meeting-2021-summary.pdf", []) == 2021
    assert file_year("https://www.toronto.ca/legdocs/mmis/2025/ex/bgrd/backgroundfile-1.pdf", []) == 2025
    assert file_year("https://a-us.storyblok.com/f/1/x/a/notice.pdf", ["2024-03-01T00:00:00Z"]) == 2024
    assert file_year("https://a-us.storyblok.com/f/1/x/a/notice.pdf", []) is None
