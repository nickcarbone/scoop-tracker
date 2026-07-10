# Scoop Tracker — Tier 1 MVP

Run: `python3 main.py` (needs `feedparser requests beautifulsoup4 lxml`, installed via
`pip install feedparser requests beautifulsoup4 lxml --break-system-packages`)

Outputs `scoop_report.html` (ranked, human-readable) and `scoop_tracker_output.json` (raw, for
piping into whatever comes next).

## What's actually in this MVP (Tier 1 only)

- **feeds.json** — 24 RSS sources; 12 currently resolve (see feed status table at the
  bottom of the HTML report for which and why).
- **ingest.py** — pulls all feeds, normalizes to a flat list.
- **enrich.py** — fetches each article's page, pulls byline count + author names from
  schema.org NewsArticle JSON-LD when present. Handles `@graph`-nested JSON-LD and
  `@id`-referenced author nodes (both are common WordPress/Yoast patterns). Fails
  gracefully to RSS-provided author data, then to a default of 1 — a fetch failure
  never kills the pipeline.
- **score.py** — regex-based scoring across three categories: exclusivity language
  ("exclusive," "scoop," weight 2), sourcing-depth language ("people familiar with
  the matter," "documents obtained by," weight 3–4), and primary-source language
  ("FOIA," "leaked memo," "whistleblower," weight 3–4). Byline count adds a capped
  bonus (2 pts/extra author, max +6). Sourcing-depth and primary-source language are
  weighted higher than bare "exclusive" labels deliberately — self-declared
  exclusivity is the easiest signal to game.
- **main.py** — orchestrates the above, dedupes by URL, writes both outputs.

## Known limitations (be honest with yourself about these before trusting the output)

1. **The original 12 "FAILED (parse error)" feeds were three different problems
   wearing one misleading error message**, now diagnosed and labeled distinctly:
   - **Sandbox-only hostname block** (NYT, NYT Politics, Politico, The Atlantic,
     Guardian, Vox, Business Insider, The Verge, Wired, AP News — 10 sources):
     this container's egress proxy blocks these hostnames outright. Untested and
     unverifiable from inside this sandbox, but the URLs are standard/current —
     **run this on your own machine and these will very likely just work.**
   - **Dead/moved URL** (Semafor, ProPublica): genuinely wrong paths. Fixed —
     Semafor is now `semafor.com/rss.xml` (228 items), ProPublica is now
     `propublica.org/feeds/propublica/main` (20 items), both verified working.
   - **Site's own bot-check, not a sandbox issue** (AP intermittently, The
     Information): Cloudflare "Just a moment" challenge page. This one *would*
     also hit you outside the sandbox — needs a real browser-automation fetch
     (e.g. Playwright) or an official API, not a plain HTTP GET, regardless of
     where you run it.
   - `ingest.py` now checks the response before handing it to feedparser, so
     future failures get labeled `ENV-BLOCKED`, `BOT-CHALLENGED`, or a real HTTP
     code instead of one generic, uninformative "syntax error."
2. **Adding a browser User-Agent changed some outcomes** — NPR and The Information
   flipped from working to blocked/challenged between runs after `ingest.py` started
   sending an explicit Chrome UA instead of feedparser's default one. Sites clearly
   treat these differently; worth testing a rotation of UAs (or feedparser's
   default) if a previously-working feed suddenly stops.
3. **Byline/JSON-LD extraction works well on about half of resolvable feeds**
   (TechCrunch, NPR, CNBC, 404 Media hit ~100% when reachable; Washington Post,
   Axios, The Hill, BBC, Reuters, Bloomberg return 0% — likely bot-detection on
   full-page fetches). Fix path: fall back to `<meta name="author">` /
   `<meta property="article:author">` tags when JSON-LD is absent.
4. **Headline-position exclusivity labels are now scored separately from loose
   body-text mentions** — a title starting with "Scoop:" or "Exclusive:" (Axios's
   actual convention) scores higher than the same word appearing mid-sentence
   elsewhere, since the two aren't equally meaningful signals. This also fixed the
   issue where Axios's 100-item feed was mechanically dominating purely on volume:
   the report view is now capped at 8 slots per source (`per_source_cap` in
   `main.py`) so a high-volume outlet can flag more candidates without crowding out
   smaller/slower feeds in what you actually see. The full uncapped ranking is
   still in the JSON if you want it.
5. **This is a candidate-surfacing score, not a verified-scoop score.** A high score
   means "a human should look at this," not "this is confirmed important." The
   Brill's Content problem — self-declared "exclusive" labels are only sometimes
   actually exclusive — is reduced but not solved by the label/body split above.
   That's what Tier 2 (downstream citation/convergence via GDELT) is for.
6. **No publishing-pattern-anomaly detection** — deliberately cut from tonight's
   scope since it needs a historical per-outlet baseline this system hasn't
   accumulated yet.
7. **No persistence/scheduling** — this runs once, in the foreground, on demand.
   Turning it into an actual running service needs a cron job or scheduler and a
   real datastore (SQLite at minimum) instead of overwriting a JSON file each run.
8. **Scraping full outlet pages at real scale has ToS implications** worth getting
   ahead of before this becomes anything beyond a personal research tool — RSS
   consumption for personal reading is uncontroversial, but repeatedly crawling
   full article pages from major outlets to extract metadata, at volume, is the
   kind of thing some publishers' terms of service explicitly address. Worth a
   look before this scales past "just for me."

## Deploying to GitHub (scheduled runs)

1. Push this directory to a new repo.
2. No secrets needed — the workflow only needs the default `GITHUB_TOKEN` (already
   available) to commit results back, granted via `permissions: contents: write`
   in the workflow file.
3. Runs every 4 hours via `.github/workflows/scoop-tracker.yml`, or trigger
   manually from the Actions tab (`workflow_dispatch`).
4. Each run: ingests all feeds, skips enrichment for any URL already in
   `scoop_tracker.db` (see below), scores only genuinely new articles, regenerates
   `scoop_report.html` from a rolling 72-hour window of the accumulated DB, and
   commits the updated db + report + JSON back to the repo.
5. **Optional — view the report as a webpage:** enable GitHub Pages on the repo
   (Settings -> Pages -> Deploy from branch -> main / root). The workflow already
   copies `scoop_report.html` to `index.html` each run, so Pages picks it up with
   no extra config.

### Why DB-backed instead of the one-off JSON from the first version

The original MVP re-fetched and re-scored everything on every run, which is fine
once, but doesn't work as a schedule — every run's cost would grow with total
historical volume instead of just the volume of new articles, and it hits the same
outlets' servers for pages you've already fetched, which is the exact scraping-load
concern flagged earlier. `db.py` fixes both: `known_urls()` is checked before
enrichment, so a URL already seen is never re-fetched, and the two SQLite tables
(`articles`, `feed_runs`) are what will make Tier 2 possible later — reporter-
authority scoring and publishing-pattern-anomaly detection both need accumulated
history to compare against, which a single run can never provide regardless of how
good the heuristics are.

### Known limitations of the scheduled version

- **The SQLite file is committed to git on every run.** That's fine at this scale
  (~1.2MB for ~1,360 articles) but git isn't really a database — if this grows into
  tens of thousands of articles, committing the whole file on every run will make
  the repo's history bloat fast. Worth revisiting (e.g. a proper hosted DB, or only
  committing a pruned/summarized export) well before that becomes painful, not
  after.
- **A scheduled job hitting ~75 outlets' full article pages every 4 hours is a
  bigger footprint than the one-off runs from before.** Caching keeps the marginal
  cost of a re-run low, but the underlying ToS question from the first round
  (README, point 8 above) applies more, not less, to something running unattended
  and indefinitely. Worth a look before this runs for months without you checking
  in on it.
- **No alerting if a feed silently breaks.** `feed_runs` records every run's status
  per source, so the data to build "this feed has failed 6 runs in a row" exists,
  but nothing currently reads it and tells you. Worth adding once you've deployed
  this and want to stop manually re-checking the feed-status table in the report.

## Next steps, in order

1. ~~Fix dead feed URLs, add meta-tag fallback for byline extraction.~~ Feed URLs
   fixed (see journalism_heuristics_and_sources.md for the full vetting pass).
   Meta-tag fallback for byline extraction still open.
2. ~~Add scheduling + persistence.~~ Done — see "Deploying to GitHub" above.
3. Push to GitHub, let it run unattended for at least a week before touching
   anything else — the retuning in step 4 and all of Tier 2 need real accumulated
   output to work from, not a guess.
4. Retune category weights once you've eyeballed a week's worth of output against
   your own judgment of what was actually worth surfacing.
5. Add GDELT integration for downstream-citation/convergence scoring (Tier 2) —
   deliberately sequenced after deployment, since GDELT's own convergence
   detection and any publishing-pattern baseline need history to compare against,
   which only a running system accumulates.
