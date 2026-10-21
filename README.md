# createto-scraper

Crawls [createto.ca](https://createto.ca) and the files it links to, and turns them into a versioned dataset
where every fact points back to its source (page URL, or file URL + page number + verbatim excerpt).
Re-running it only does work for things that changed.

## Setup

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # add OPENROUTER_API_KEY (only needed for the PDF extraction step)
```

## Running

```bash
uv run createto run                      # full crawl + extraction, writes to data/
uv run createto run                      # run it again: conditional requests, cached LLM results
uv run createto run --no-llm             # skip the paid step
uv run createto run --seed https://createto.ca/projects/parkdale-hub   # one page + its files
uv run createto reprocess                # re-parse / re-extract what's already downloaded, no crawling
uv run createto export                   # just rewrite data/export/*.jsonl
./scripts/demo_variants.sh               # unchanged / changed source / failed file, against saved copies
uv run pytest
```

(`uv run python -m createto ...` works too. On macOS with Python 3.13, if `createto` isn't found after `uv sync`,
it's the hidden-`.pth` issue: use the `python -m` form or `chflags nohidden .venv/lib/python3.13/site-packages/*.pth`.)

Useful flags: `--since YYYY-MM-DD` (default: 5 years ago), `--max-llm-docs N`, `--model <openrouter id>`,
`--replay <fixture dir>`, `--data-dir`.

Every run prints a summary and writes a full report to `data/runs/run_NNN.json`.

## How it works

```
crawl.py    sitemap + homepage -> follow createto.ca links -> download linked files (any host)
parse.py    read the CMS story embedded in each page -> projects, news, facts      (no LLM)
pdf.py      per-page text out of each PDF
extract.py  PDF pages -> LLM -> facts with page + excerpt, excerpt checked against the page
db.py       sqlite + content-addressed blobs; sync_facts() keeps the history
export.py   JSONL exports + run report
```

The thing that shaped most of this: createto.ca is a Next.js site on the Storyblok CMS, and every page ships its
full CMS record in `<script id="__NEXT_DATA__">`. So project phase, ward, address, lat/lng, site area, councillor,
uses, types, and publish/update timestamps are already structured. I read them directly instead of scraping HTML
or sending pages to a model. The LLM is only used for the PDFs (staff reports, meeting summaries, info
sheets, board minutes), where the information really is unstructured.

**Crawl.** Seeds are the homepage plus `sitemap.xml`. Only createto.ca pages are crawled, but files linked from
those pages are downloaded from wherever they live (Storyblok's CDN, toronto.ca, waterfrontoronto.ca, ...).
1 req/s to createto.ca, ~3 req/s to file CDNs, honest User-Agent, robots.txt checked per host, retries with
backoff on 429/5xx, 100 MB cap per file. Every URL we skip, block or fail on goes into `crawl_log` with a reason.

**Links and attribution.** Links are pulled out of the CMS JSON (multilink fields, asset fields, rich-text links)
and the rendered HTML. Each link gets a context: `content` if it's in the page's own story, `related` if it's in
a teaser/sidebar or an embedded story (an article's `relatedProjects` embeds those projects' files), `html` otherwise.
A file belongs to the project whose own story links it.

**Facts and history.** A fact is `(entity, field, value)` plus its source. Each extraction unit (one CMS story, one
file) owns a set of facts, and `sync_facts` makes the stored set match what was just extracted. New values get
inserted, values that are no longer there get closed (`valid_to`), and if a field changed from one value to
another (a project's phase, ward, ...) the old row points at the new one (`superseded_by`). For PDF facts, where
one file can give ten milestones, there's no way to say which new one replaced which old one, so they're just
closed and added. Nothing is deleted, so you can see what was true at any run. If a page 404s, its facts are
closed and the project is marked `removed_in_run`. Pages from earlier runs are re-checked even if nothing links to
them any more, so removals are noticed. A file that no page links to any more has its facts closed. If a fetch
just fails, the last good data is kept.

**PDF extraction.** Pages are sent as `=== PAGE N ===` blocks with a strict JSON schema. Each fact needs a type
(milestone, decision, housing_units, cost, funding, building_size, site_area, partner, status, other), a page
number and a verbatim excerpt. The excerpt is checked against the page text, exact match first and then fuzzy
(≥90), also trying ±1 page. Facts that can't be found are dropped. In practice ~92% survive, and most drops are
documents that turn out to be older than the cutoff. Responses are cached on `(file sha256, pages, prompt version,
model)`, and the same file uploaded under two URLs only gets one call.

**Re-runs.** Pages and files are re-requested with `If-None-Match` / `If-Modified-Since`. Both createto.ca (Vercel)
and Storyblok answer `304` with an empty body when nothing changed. Unchanged bytes mean no re-extraction and no
LLM call. Raw bodies are stored by sha256 under `data/blobs/`, and every distinct version of every URL is kept in
`document_versions`.

**5-year scope.** Everything is crawled and stored (it's cheap), but news before `--since` is left out of the
exports, and files are only sent to the LLM if their date (from the filename/URL, the linking article, or the
date the model reads off the document) is within range. Skips are logged.

## Output

`data/export/`:

| file | what |
|---|---|
| `projects.jsonl` | one line per project: CMS fields + linked documents (with LLM-read title/type/date) |
| `news.jsonl` | articles in range, including external news cards (links to toronto.ca etc.) |
| `facts.jsonl` | every fact ever extracted, current and superseded, with source, page, excerpt, observed_at / valid_to |
| `files.jsonl` | every linked file: status, error, sha256, pages, which pages link to it |
| `crawl_log.jsonl` | every URL from the latest run and what happened to it: new / changed / unchanged / skipped / blocked / failed, with a reason and the page it was found on |

Also `data/createto.db` (sqlite, all tables incl. `crawl_log`, `requests`, `llm_calls`) and `data/runs/*.json`.

A fact from a PDF:

```json
{
  "entity_id": "project:1869a736-6361-4e82-83d5-c6baac93ca19",
  "entity_name": "Parkdale Hub",
  "field": "building_size",
  "value": {"amount": 30000, "unit": "sq ft", "project": "Parkdale Hub", "verification": "exact",
            "description": "Revitalization and expansion of Masaryk-Cowan Community Recreation Centre with addition of up to 30,000 square feet of space"},
  "source_url": "https://a-us.storyblok.com/f/1017201/x/0a2dd7f9f0/meeting-2021-information-sheet-phase-2.pdf",
  "page": 1,
  "excerpt": "the addition of up to 30,000 square feet of space",
  "method": "llm", "model": "anthropic/claude-haiku-5.5", "prompt_version": "v1",
  "observed_at": "2026-10-21T03:08:13+00:00", "valid_to": null, "current": true
}
```

CMS facts look the same with `method: "cms"` and `locator` naming the story field.
Example output is in [`examples/`](examples/).

## Results

Two live runs against createto.ca on 2026-10-21, from a clean `data/` dir, then again with nothing changed.
Reports: [`examples/full_run/runs/`](examples/full_run/runs/), full exports: [`examples/full_run/export/`](examples/full_run/export/).

| | first run | unchanged repeat |
|---|---|---|
| wall time | 11.6 min | 7.0 min |
| HTTP requests | 736 (730 × 200, 4 × 404, 2 × 403) | 736 (718 × 304, 12 × 200, 4 × 404, 2 × 403) |
| downloaded | 1.21 GB | 118 MB (see waterfrontoronto.ca note below) |
| pages crawled | 251 new, 2 failed | 251 unchanged |
| files | 471 new, 2 failed, 3 blocked by robots.txt | 471 unchanged |
| PDFs text-extracted | 453 (3 have no text layer) | 0 |
| files sent to the LLM | 316 (161 skipped as older than 5 years) | 0 (332 cache hits) |
| paid LLM calls | 319 | 0 |
| tokens in / out | 1.79M / 464k | 0 |
| useful records | 74 projects, 137 news items, 4,171 facts (1,311 CMS + 2,860 from PDFs) | +0 facts added, 0 closed |
| LLM facts dropped (excerpt not found / doc too old) | 245 | – |
| **cost** | **$0.41** | **$0.00** |

The repeat run's time is almost all the politeness delay (1 req/s over ~255 createto.ca URLs). The body work is a
handful of 304s.

**Costs.** LLM cost is the actual per-call `usage.cost` reported by OpenRouter for `anthropic/claude-haiku-5.5`
($0.10 / $0.50 per M tokens), summed in `llm_calls`. Development and testing (model comparison, dev crawl,
variants) used about another $0.50. Hosting: $0, it runs locally. Storage is ~1.2 GB of raw files. A scheduled
version on a small VM or GitHub Actions would cost roughly $0–5/month plus ~$0.40 for each full re-extraction
(estimate). Incremental runs only pay for new or changed files.

### Variants (saved local copies)

`./scripts/demo_variants.sh` runs four times into one db against [`fixtures/`](fixtures/), a single project page
(St. Lawrence Centre for the Arts) and its 2 PDFs served from disk with real ETag / 304 behaviour.
Output: [`examples/variants/`](examples/variants/). The same steps (plus a removed page) run as a test with no
network or LLM in `tests/test_variants.py`.

| run | fixture | what happened |
|---|---|---|
| 1. first | `base` | 1 page + 2 files new, 37 facts, 2 LLM calls ($0.0023) |
| 2. unchanged | `base` | all 304, 0 facts added/closed, 0 LLM calls (2 cache hits), $0 |
| 3. changed source | `changed_source` | page's CMS phase edited + one PDF amended with an extra sentence. Phase fact "Development Concept" closed and `superseded_by` → "Pre-construction". Only the amended PDF goes back to the LLM (1 call, $0.0012), and the new "construction now expected Spring 2028" fact comes back with its page + excerpt |
| 4. failed file | `failed_file` | amended PDF now truncated, the other PDF returns HTTP 500 (retried 4×). Both logged as failed in `crawl_log` (`damaged PDF (needed repair)`, `HTTP 500`), no facts lost: the last good facts stay current |

## Model choice

Same 8 random in-scope documents (40 pages) through three cheap models (`scripts/compare_models.py`):

| model | facts | excerpt verified | cost | p50 latency |
|---|---|---|---|---|
| anthropic/claude-haiku-5.5 | 94 | 99% | $0.0113 | 8.0 s |
| openai/gpt-5.6-luna | 112 | 96% | $0.0197 | 16.1 s |
| google/gemini-3.1-flash-lite | 40 | 100% | $0.0159 | 2.9 s |

Went with Haiku 5.5: close to Luna on recall at ~60% of the cost and half the latency. Flash-Lite was fast but
missed most facts. "Verified" only means the quote is really on that page, not that the interpretation is right.
A small hand-labelled gold set would be the next step to measure precision properly.

## Decisions / things I left out

- **No headless browser.** Pages are server-rendered and the data is in the HTML, so a browser would just be slower.
- **No Scrapy.** At ~250 pages / ~500 files it's more machinery than needed. The interesting part here is the
  incremental / history logic, and I wanted that explicit. At a much bigger scale I'd move to Scrapy or a job queue.
- **Didn't use the Storyblok API.** The "View more" on the news listing calls Storyblok's content API with a token
  from the site's JS. That would give the full news list, but the brief says public pages and linked files, so I
  only use what's on the pages. External news cards come from whatever listing/teaser blocks are embedded in pages
  (56 of them). Internal articles all have their own pages via the sitemap.
- **Scanned PDFs** (3 with no text layer) are stored but not extracted. They'd need OCR or a vision model.
- **Word/Excel files** are downloaded and stored but not parsed (there are only a handful).
- **Fact types are coarse.** `other` and `status` catch a lot. With more time I'd tighten the taxonomy and
  normalise into proper per-project fields (e.g. `affordable_units`, `council_approval_date`) with a resolution
  step when sources disagree.
- **Hosts that ignore conditional requests.** waterfrontoronto.ca always answers `200`, so its 4 PDFs (~118 MB)
  get re-downloaded on every run. They're recognised as unchanged by hash, so nothing is re-processed, but it's
  wasted bandwidth. The fix is a `HEAD` check (Content-Length + Last-Modified) for hosts that have done this before.
- **Damaged PDFs.** MuPDF quietly "repairs" broken files and returns fragments. If a file needed repair I treat
  it as failed and keep the previous good facts. None of the 450 real PDFs needed repair.
