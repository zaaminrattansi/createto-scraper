"""The variant checks from scripts/demo_variants.sh as a test, against the saved copies in fixtures/.
No network and no LLM (the PDFs still get downloaded + text-extracted, just not sent to a model)."""
import json
from pathlib import Path

from createto import cli, db

FIXTURES = Path(__file__).parent.parent / "fixtures"
PAGE = "https://createto.ca/projects/st-lawrence-centre-for-the-arts"


def run(data_dir, fixture):
    cli.run(data_dir=data_dir, replay=fixture, seed=PAGE, since="2021-01-01", no_llm=True, max_llm_docs=None,
            model=None)
    con = db.connect(data_dir / "createto.db")
    run_id = con.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    log = {r["url"]: r["outcome"] for r in con.execute(
        "SELECT url, outcome FROM crawl_log WHERE run_id = ? AND kind IN ('page', 'file')", (run_id,))}
    return con, run_id, log


def current_facts(con):
    return con.execute("SELECT COUNT(*) FROM facts WHERE valid_to_run IS NULL").fetchone()[0]


def phases(con):
    return con.execute("SELECT id, value, valid_to_run, superseded_by FROM facts WHERE field = 'phase' "
                       "ORDER BY id").fetchall()


def test_variants(tmp_path):
    con, _, log = run(tmp_path, FIXTURES / "base")
    assert log[PAGE] == "new" and len(log) == 3
    n = current_facts(con)
    assert n > 0

    # unchanged re-run: all 304s, nothing added or closed
    con, _, log = run(tmp_path, FIXTURES / "base")
    assert set(log.values()) == {"unchanged"}
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == n

    # changed source: old phase closed and linked to the new one
    con, run_id, log = run(tmp_path, FIXTURES / "changed_source")
    assert log[PAGE] == "changed"
    old, new = phases(con)
    assert json.loads(old["value"]) == "Development Concept" and old["valid_to_run"] == run_id
    assert old["superseded_by"] == new["id"]
    assert json.loads(new["value"]) == "Pre-construction" and new["valid_to_run"] is None

    # failed file: both PDFs logged as failed, nothing we already knew gets closed
    before = current_facts(con)
    con, _, log = run(tmp_path, FIXTURES / "failed_file")
    assert [o for u, o in log.items() if u != PAGE] == ["failed", "failed"]
    assert current_facts(con) == before

    # page removed (404): its facts get closed and the project is marked as removed
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "manifest.json").write_text(json.dumps({PAGE: {"status": 404}}))
    con, run_id, log = run(tmp_path, gone)
    assert log[PAGE] == "failed"
    assert con.execute("SELECT COUNT(*) FROM facts WHERE valid_to_run IS NULL AND scope = ?",
                       (f"cms:{PAGE}",)).fetchone()[0] == 0
    project = json.loads(con.execute("SELECT data FROM entities WHERE type = 'project'").fetchone()["data"])
    assert project["removed_in_run"] == run_id
