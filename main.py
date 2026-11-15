"""Piece 5+6: Orchestration + Output — now DB-backed for scheduled/repeated runs.

Key change from the one-off version: articles already seen in a prior run are
never re-fetched or re-scored. Only genuinely new URLs go through enrich+score.
This is what makes a scheduled job's footprint sane instead of growing linearly
with total historical volume, and it's what accumulates the history Tier 2 needs.
"""
import json
import re
import html as html_lib
import argparse
from datetime import datetime, timezone
from ingest import ingest_all
from enrich import enrich_all
from score import score_all
import db

# (bucket_key, max_age_hours, display_label) — checked in order, first match wins.
# Bucketed by the article's own recency, not by when our pipeline first saw it,
# so a high-scoring but two-day-old story can no longer sit at the top of a
# report generated an hour ago.
RECENCY_BUCKETS = [
    ("last_6h", 6, "Last 6 Hours"),
    ("6_24h", 24, "6–24 Hours Ago"),
    ("24_72h", 72, "24–72 Hours Ago"),
]

def _effective_timestamp(article):
    """Best available recency signal for an article: the feed's own
    published/updated time when the outlet supplied one and feedparser could
    parse it, falling back to first_seen_at (when our pipeline first ingested
    the URL) when it didn't. The fallback is a real limitation worth naming,
    not a nicety — some feeds are inconsistent or silent on dates, and
    first_seen_at can lag true publish time by up to one 4-hour run cycle.
    Returns None only if both fields are somehow missing/malformed."""
    for key in ("published_parsed", "first_seen_at"):
        raw = article.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
    return None

# --- Story-level deduplication --------------------------------------------
# Problem: per_source_cap prevents one prolific *source* from crowding out a
# bucket, but does nothing when N different sources all cover the same
# underlying event (e.g. a single filed lawsuit picked up by wire services) —
# each write-up scores independently, so identical non-exclusive coverage can
# fill 8+ consecutive rows. Clustering groups those together before bucketing/
# capping, so only one representative shows per distinct story, with the rest
# listed as "also covered by."
#
# Similarity is deliberately loose (paraphrase-tolerant) token-overlap on
# normalized titles, not embeddings — good enough at rolling-window scale and
# doesn't pull in an ML dependency for a personal project. "Loose" is a
# deliberate choice here (per discussion) and carries a real risk: it can
# merge two distinct-but-related stories that happen to share a lot of
# vocabulary (e.g. two separate Apple/OpenAI stories in the same week).
# Worth tightening _STORY_CLUSTER_THRESHOLD if that starts happening in
# practice — this hasn't been validated against a real false-merge yet.
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are",
    "was", "were", "by", "with", "at", "from", "as", "its", "it", "this",
    "that", "after", "over", "amid", "says", "say", "said", "into", "new",
    "up", "out", "than", "will", "be", "has", "have", "had", "not", "but",
}
_STORY_CLUSTER_THRESHOLD = 0.35  # unchanged number -- see _overlap_coefficient
                                   # docstring for why the METRIC changed instead.

_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _cluster_tokens(article):
    """Tokens used for story-clustering similarity. Combines title with the RSS
    summary -- title alone was missing genuine duplicates.

    Diagnosed 2026-07-16 against two real same-story misses in production: NBC
    vs. The Verge on the same Tesla/NTSB crash report, and Axios vs. ABC on the
    same White House teleprompter/Kalshi story. In both cases the outlets led
    with different facts about the identical event (one names the investigating
    agency, the other the crash mechanism; one names the prediction-market
    platform "Kalshi," the other describes the same fact as generic "betting"),
    so title-only token overlap landed well under threshold despite being
    genuine duplicates.

    Uses `summary`, not the enriched `article_body_snippet`, because summary is
    populated for every ingested article regardless of enrichment outcome --
    the body snippet only exists for the ~half of articles where JSON-LD
    extraction succeeds (see README known-limitations #3), so keying clustering
    on it would fix some sources' duplicates and not others', inconsistently."""
    text = _HTML_TAG_RE.sub(" ", f"{article.get('title', '')} {article.get('summary', '')}")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {t for t in cleaned.split() if t not in _STOPWORDS and len(t) > 2}

def _overlap_coefficient(a, b):
    """Shared tokens over the size of the SMALLER set, not the union (i.e. not
    Jaccard). This was a deliberate correction, not the original plan: adding
    summary text to plain Jaccard was tested first and made both known misses
    WORSE (Tesla/NTSB: 0.188 -> 0.123; Kalshi: 0.286 -> 0.260), because a
    longer, more detailed summary on one side (extra names, dollar figures,
    agency specifics) inflates the union faster than genuinely shared
    vocabulary grows the intersection -- Jaccard punishes exactly the kind of
    one-sided detail real summaries have. Overlap coefficient isn't diluted by
    that, and empirically: it fixed the Kalshi miss (0.481, clears threshold)
    with no observed new false-merge risk against a real non-duplicate control
    pair (two distinct AI-safety-policy stories from different companies,
    0.211 -- well under threshold).

    The Tesla/NTSB pair was tested against a reconstructed, unverified Verge
    title/summary (theverge.com blocked direct fetch and didn't surface exact
    text via search) and scored 0.292 -- still just under threshold. Not
    shipped as a confirmed fix for that specific pair, only as a strict
    improvement with no downside shown. Worth re-checking against the real
    Verge text once available, and worth revisiting the 0.35 threshold itself
    only once there's real (not reconstructed) data on where the margin
    against false merges actually sits."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))

def cluster_articles(articles, threshold=_STORY_CLUSTER_THRESHOLD):
    """Union-find clustering on title+summary token overlap coefficient. O(n^2)
    comparisons — fine at rolling-window scale (low hundreds of articles).
    Revisit with blocking (e.g. by day, or by shared proper-noun entities) if
    the window ever grows large enough for the O(n^2) pass to become a real
    cost — untested at that scale, not a hypothetical problem to pre-solve now."""
    n = len(articles)
    tokens = [_cluster_tokens(a) for a in articles]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if _overlap_coefficient(tokens[i], tokens[j]) >= threshold:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(articles[i])
    return list(groups.values())

def _timestamp_sort_key(article):
    """Earliest-first ordering for representative selection. An article with
    no usable timestamp sorts last — missing data shouldn't win 'who had it
    first' by default."""
    ts = _effective_timestamp(article)
    return ts if ts is not None else datetime.max.replace(tzinfo=timezone.utc)

def select_representative(cluster):
    """Per working-style discussion: an outlet with an actual headline label
    (a real 'Scoop:'/'First on'/'Exclusive:' claim, not just body-text
    mentioning the same lawsuit everyone has) wins regardless of timestamp —
    that's an editorial claim, not wire-pickup. Among those (or if none
    exist), earliest timestamp wins, as the closest available proxy for
    who actually had it first — acknowledged as only as good as each feed's
    own published/updated timestamp, which can be a same-hour wire-pickup
    race rather than a meaningful gap.

    Known limitation (flagged 2026-07-29, Suri Cruise/Page Six case): this
    logic can only compare outlets that are actually in feeds.json. If the
    true first-reporter isn't a tracked source, this will confidently pick
    the earliest *tracked* outlet and present it as if it were the origin —
    it has no way to know a non-monitored outlet broke the story earlier.
    Not fixable in this function; the fix is expanding source coverage, or
    (longer-term) Tier 2 GDELT downstream-citation checking, which could
    eventually flag "this outlet cites another outlet" even when the
    original isn't tracked."""
    labeled = [a for a in cluster if "headline_label" in a.get("matched_categories", [])]
    pool = labeled if labeled else cluster
    return min(pool, key=_timestamp_sort_key)

def collapse_to_stories(articles):
    """Cluster all flagged articles across the *whole* window, not per-bucket
    — a story can straddle a bucket boundary (e.g. one outlet's write-up
    timestamped at 5h59m, another at 6h01m), and per-bucket clustering would
    silently miss that split. Returns one representative per cluster, each
    carrying _cluster_others/_cluster_size for display, sorted by
    total_score desc so downstream bucket_by_recency + select_for_bucket
    behave exactly as they did on the uncollapsed list."""
    clusters = cluster_articles(articles)
    reps = []
    for cluster in clusters:
        rep = dict(select_representative(cluster))
        others = [a for a in cluster if a["link"] != rep["link"]]
        rep["_cluster_others"] = others
        rep["_cluster_size"] = len(cluster)
        reps.append(rep)
    reps.sort(key=lambda a: a["total_score"], reverse=True)
    return reps

def bucket_by_recency(articles, now):
    """Split articles into RECENCY_BUCKETS by age. Articles with no usable
    timestamp at all (both published_parsed and first_seen_at missing/bad)
    are dropped into the oldest bucket rather than silently discarded —
    better to surface them somewhere than to lose them."""
    buckets = {key: [] for key, _, _ in RECENCY_BUCKETS}
    for a in articles:
        ts = _effective_timestamp(a)
        if ts is None:
            buckets[RECENCY_BUCKETS[-1][0]].append(a)
            continue
        age_hours = max((now - ts).total_seconds() / 3600, 0)  # clamp negative (clock skew / future-dated) to 0
        for key, max_age, _ in RECENCY_BUCKETS:
            if age_hours <= max_age:
                buckets[key].append(a)
                break
        else:
            buckets[RECENCY_BUCKETS[-1][0]].append(a)
    return buckets

def select_for_bucket(articles, per_source_cap, top_n):
    """Same per-source-cap logic as before, applied fresh within one bucket —
    articles are already sorted by total_score DESC coming in from the DB query."""
    selected = []
    source_counts = {}
    for a in articles:
        c = source_counts.get(a["source"], 0)
        if c < per_source_cap:
            selected.append(a)
            source_counts[a["source"]] = c + 1
        if len(selected) >= top_n:
            break
    return selected

def dedupe(articles):
    seen = set()
    out = []
    for a in articles:
        key = a["link"]
        if key and key not in seen:
            seen.add(key)
            out.append(a)
    return out

def run_pipeline(db_path="scoop_tracker.db", report_window_hours=72, per_source_cap=8, top_n_per_bucket=20):
    print("=" * 60)
    print("SCOOP TRACKER — scheduled run")
    print("=" * 60)
    conn = db.connect(db_path)

    print("\n[1/5] Ingesting feeds...")
    entries, feed_status = ingest_all(verbose=True)
    entries = dedupe(entries)
    print(f"\n  -> {len(entries)} unique articles this run")
    db.record_feed_runs(conn, feed_status)

    print("\n[2/5] Filtering to new URLs (skip anything already persisted)...")
    known = db.known_urls(conn)
    new_entries = [a for a in entries if a["link"] not in known]
    print(f"  -> {len(new_entries)} new / {len(entries) - len(new_entries)} already known and skipped")

    print("\n[3/5] Enriching new articles only...")
    enriched = enrich_all(new_entries, verbose=True) if new_entries else []
    jsonld_hits = sum(1 for e in enriched if e["found_jsonld"])
    if enriched:
        print(f"  -> byline data found on {jsonld_hits}/{len(enriched)} new articles")

    print("\n[4/5] Scoring and persisting new articles...")
    scored_new = score_all(enriched) if enriched else []
    db.upsert_articles(conn, scored_new)
    print(f"  -> {len(scored_new)} new articles scored and saved")

    # This is the one authoritative definition of "new" for the report: a URL
    # that was actually enriched+scored+inserted *in this execution*. Deliberately
    # not inferred later from first_seen_at proximity to "now" — that would also
    # light up anything from the last few hours even if it was really caught by
    # a prior run, which defeats the point (a reader who already saw last run's
    # report needs "new since I looked," not "new-ish").
    new_urls = {a["link"] for a in scored_new}

    print("\n[5/5] Generating report from accumulated history...")
    write_html(conn, feed_status, path="scoop_report.html",
               window_hours=report_window_hours, per_source_cap=per_source_cap,
               top_n_per_bucket=top_n_per_bucket, new_urls=new_urls)
    write_json(conn, window_hours=report_window_hours, new_urls=new_urls)
    print("  -> wrote scoop_tracker_output.json and scoop_report.html")
    conn.close()

def esc(s):
    return html_lib.escape(s or "")

def write_json(conn, path="scoop_tracker_output.json", window_hours=72, new_urls=frozenset()):
    # The JSON is meant to be the full, uncollapsed window (see write_html), and a
    # 72h window holds ~3,000 articles, so the old limit=2000 truncated it.
    articles = db.recent_articles(conn, hours=window_hours, limit=100000)
    for a in articles:
        a["is_new"] = a["link"] in new_urls
    with open(path, "w") as f:
        json.dump(articles, f, indent=2)

def _plain_summary(value, limit=900):
    """Turn feed-provided HTML into a short, safe plain-text excerpt."""
    text = _HTML_TAG_RE.sub(" ", str(value or ""))
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]

def _safe_http_url(value):
    """Only render navigable http(s) links from feed data."""
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else ""

def _display_time(article):
    ts = _effective_timestamp(article)
    if ts is None:
        return "Time not supplied"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%b %d · %H:%M UTC")

def _story_is_new(article, new_urls):
    return article.get("link") in new_urls or any(
        other.get("link") in new_urls for other in article.get("_cluster_others", [])
    )

_SIGNAL_EXPLANATIONS = {
    "headline_label": (
        "A headline-position label such as “Scoop:”, “Exclusive:” or “First on” "
        "appears at the start of the headline. This is the outlet's own claim, "
        "not independent confirmation."
    ),
    "exclusivity": (
        "Exclusivity language appears in the headline, RSS summary or extracted "
        "article text—for example “exclusive”, “can reveal” or “has obtained”. "
        "The wording can be self-asserted."
    ),
    "sourcing_depth": (
        "The text contains language associated with deeper sourcing, such as "
        "people familiar with a matter, anonymous sourcing, documents obtained "
        "or reviewed, or a declined request for comment. The match does not "
        "establish the source's quality."
    ),
    "primary_source": (
        "The text references documentary or first-hand material such as court "
        "filings, FOIA, leaked material, whistleblowers or regulatory filings. "
        "A mention does not mean the underlying record was independently checked."
    ),
    "institutional_record": (
        "The text references a named institutional record or report, such as "
        "SEC, DOJ, NTSB, FDA, PACER or an inspector-general report. The match "
        "does not verify the record."
    ),
}

def render_rows(selected, new_urls=frozenset()):
    """Render one article card per clustered story, with transparent context and local actions."""
    rows = []
    for i, article in enumerate(selected, 1):
        others = article.get("_cluster_others", [])
        cluster = [article, *others]
        categories = sorted(set(article.get("matched_categories", [])))
        tags = " ".join(categories)
        tag_html = "".join(
            f'<span class="tag tag-{esc(category)}">{esc(category.replace("_", " "))}</span>'
            for category in categories
        )

        is_new = _story_is_new(article, new_urls)
        new_badge = '<span class="badge-new">NEW</span>' if is_new else ""
        byline_count = max(int(article.get("byline_count") or 1), 1)
        byline_bonus = min((byline_count - 1) * 2, 6)
        byline_label = f"{byline_count} bylines" if byline_count > 1 else "1 byline"
        authors = article.get("author_names") or []
        author_label = ", ".join(str(name) for name in authors[:3] if name)

        signal_items = "".join(
            f'<li><strong>{esc(category.replace("_", " ").title())}.</strong> '
            f'{esc(_SIGNAL_EXPLANATIONS.get(category, "A scoring marker in this category matched."))}</li>'
            for category in categories
        )
        if byline_bonus:
            signal_items += (
                f'<li><strong>Byline credit: +{byline_bonus}.</strong> '
                f'{byline_label}; the bonus is two points per additional byline, capped at six.</li>'
            )
        elif not categories:
            signal_items = '<li>No phrase category was recorded; the score reflects the available byline credit.</li>'
        else:
            signal_items += '<li><strong>Byline credit: +0.</strong> No additional-byline bonus was applied.</li>'

        summary = _plain_summary(article.get("summary", ""))
        summary_html = (
            f'<p class="summary-text">{esc(summary)}</p>'
            if summary else '<p class="summary-empty">No RSS summary was supplied by this outlet.</p>'
        )

        story_urls = sorted({
            url for item in cluster
            if (url := _safe_http_url(item.get("link")))
        })
        sources = sorted({
            str(item.get("source") or "").strip()
            for item in cluster if str(item.get("source") or "").strip()
        })
        source_json = esc(json.dumps(sources, ensure_ascii=False))
        urls_json = esc(json.dumps(story_urls, ensure_ascii=False))
        source_names = " · ".join(sources)
        coverage_items = []
        for item in cluster:
            url = _safe_http_url(item.get("link"))
            name = esc(item.get("source", "Unknown outlet"))
            title = esc(item.get("title", "Untitled coverage"))
            if url:
                coverage_items.append(
                    f'<li><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{name}</a>'
                    f'<span>{title}</span></li>'
                )
            else:
                coverage_items.append(f'<li><strong>{name}</strong><span>{title}</span></li>')
        coverage_html = ""
        if len(cluster) > 1:
            coverage_html = f"""
          <details class="coverage-details">
            <summary>Coverage cluster · {len(cluster)} tracked write-ups</summary>
            <p class="cluster-caveat">Grouped automatically by similarity between headlines and RSS summaries. This is a research aid, not confirmation that these outlets independently verified the story.</p>
            <ul class="coverage-list">{''.join(coverage_items)}</ul>
          </details>"""

        url = _safe_http_url(article.get("link"))
        title = esc(article.get("title", "Untitled story"))
        title_html = (
            f'<a class="story-title" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{title}</a>'
            if url else f'<span class="story-title">{title}</span>'
        )
        score = int(article.get("total_score") or 0)
        time_epoch = 0
        ts = _effective_timestamp(article)
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            time_epoch = int(ts.timestamp())
        source = str(article.get("source") or "")
        meta_parts = [source, _display_time(article), byline_label]
        if author_label:
            meta_parts.append(author_label)
        meta = " · ".join(part for part in meta_parts if part)

        rows.append(f"""
        <article class="story-card{' is-new' if is_new else ''}" data-story-card
          data-source="{esc(source)}" data-sources="{source_json}"
          data-tags="{esc(tags)}" data-new="{str(is_new).lower()}"
          data-score="{score}" data-time="{time_epoch}" data-story-urls="{urls_json}">
          <div class="story-topline">
            <div class="story-rankline">
              <span class="rank">#{i:02d}</span>
              <span class="score-badge">SCORE {score}</span>
              {new_badge}
              <span class="cluster-count">{len(cluster)} outlet{'s' if len(cluster) != 1 else ''}</span>
            </div>
            <div class="story-actions">
              <button type="button" class="action-button save-button" data-action="save" aria-pressed="false">Save</button>
              <button type="button" class="action-button dismiss-button" data-action="dismiss">Dismiss</button>
            </div>
          </div>
          {title_html}
          <div class="meta">{esc(meta)}</div>
          <div class="tags">{tag_html}</div>
          <details class="story-context">
            <summary>Why it surfaced <span>View matched signals and RSS context</span></summary>
            <div class="context-body">
              <div class="context-section">
                <h3>Matched scoring signals</h3>
                <ul class="signal-explanations">{signal_items}</ul>
                <p class="score-caution">The displayed score is the sum of matched keyword markers and any byline bonus. Individual marker weights are not stored in the report database, so this explains the category-level matches rather than claiming an exact phrase-by-phrase breakdown.</p>
              </div>
              <div class="context-section">
                <h3>RSS summary</h3>
                {summary_html}
              </div>
            </div>
          </details>
          {coverage_html}
        </article>""")
    return "".join(rows)

def write_html(conn, feed_status, path="scoop_report.html", window_hours=72,
                top_n_per_bucket=20, per_source_cap=8, new_urls=frozenset()):
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    all_recent = [a for a in db.recent_articles(conn, hours=window_hours, limit=5000) if a["total_score"] > 0]
    total_flagged = len(all_recent)

    # Collapse same-story duplicates across sources before bucketing — see
    # collapse_to_stories() for why this has to happen across the whole
    # window rather than inside bucket_by_recency. Deliberately NOT applied
    # to write_json below: the JSON output stays the full, uncollapsed list
    # since downstream uses (Tier 2 GDELT convergence, retuning) need
    # per-article granularity, not the report's display-level collapsing.
    story_representatives = collapse_to_stories(all_recent)
    total_stories = len(story_representatives)
    total_collapsed = total_flagged - total_stories

    # Bucket first, then apply the per-source cap fresh within each bucket —
    # a prolific source can take up to per_source_cap slots in EACH time
    # window, not just once across the whole report. That's a deliberate
    # choice (a source shouldn't be penalized for being active in more than
    # one window) but it does mean total items shown can exceed what the old
    # single-list report showed for the same per_source_cap value.
    buckets = bucket_by_recency(story_representatives, now_dt)
    bucket_sections = []
    total_shown = 0
    total_new_shown = 0
    for key, _, label in RECENCY_BUCKETS:
        bucket_articles = buckets[key]
        selected = select_for_bucket(bucket_articles, per_source_cap, top_n_per_bucket)
        total_shown += len(selected)
        total_new_shown += sum(1 for a in selected if _story_is_new(a, new_urls))
        rows_html = render_rows(selected, new_urls)
        bucket_sections.append(f"""
  <section class="bucket" data-bucket>
    <div class="bucket-heading">
      <h2 class="bucket-title">{esc(label)}</h2>
      <span class="bucket-count" data-bucket-count>{len(selected)} shown · {len(bucket_articles)} available</span>
    </div>
    <div class="story-list">{rows_html if rows_html else '<p class="empty">Nothing flagged in this window.</p>'}</div>
  </section>""")

    total_in_window = len(db.recent_articles(conn, hours=window_hours, limit=100000))

    feed_rows = "".join(
        f'<tr><td>{esc(s)}</td><td class="{"ok" if status.startswith("OK") else "fail"}">{esc(status)}</td></tr>'
        for s, status in feed_status
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#12110f">
<title>Scoop Tracker — Investigative Lead Queue</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Courier+Prime:wght@400;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="assets/report.css">
<script src="assets/report.js" defer></script>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <div class="masthead-top">
      <span class="eyebrow">Editorial intelligence · Tier 1</span>
      <span class="generated">Generated {now}</span>
    </div>
    <h1>SCOOP <span>TRACKER</span></h1>
    <p class="dek">A rolling queue of reporting leads, documentary signals and emerging stories. Built to help you decide what deserves a closer look.</p>
    <div class="stats">
      <div class="stat"><b>{total_in_window}</b><span>articles scanned</span></div>
      <div class="stat"><b>{total_flagged}</b><span>scoring matches</span></div>
      <div class="stat"><b>{total_stories}</b><span>story clusters</span></div>
      <div class="stat"><b>{total_collapsed}</b><span>related write-ups grouped</span></div>
      <div class="stat"><b>{total_shown}</b><span>leads displayed</span></div>
      <div class="stat"><b>{total_new_shown}</b><span>new this run</span></div>
    </div>
    <div class="score-legend">
      <strong>How to read this queue:</strong> scores combine keyword-based structural signals and a capped byline bonus.
      A high score means “worth a human's attention,” not “verified scoop.” Story clusters use headline and RSS-summary
      similarity; they are not confirmation of independent reporting. Use the matched-signal notes on each lead to inspect
      why it appeared.
    </div>
  </header>

  <section class="triage" aria-label="Story triage controls">
    <div class="triage-primary">
      <label class="search-box" for="story-search">
        <span class="search-icon" aria-hidden="true">⌕</span>
        <input id="story-search" type="search" placeholder="Search headlines, outlets, authors and summaries…" autocomplete="off">
        <kbd>/</kbd>
      </label>
      <div class="view-switch" role="group" aria-label="Story view">
        <button type="button" class="view-button is-active" data-view="all" aria-pressed="true">All <span data-view-count="all">{total_shown}</span></button>
        <button type="button" class="view-button" data-view="new" aria-pressed="false">New <span data-view-count="new">{total_new_shown}</span></button>
        <button type="button" class="view-button" data-view="saved" aria-pressed="false">Saved <span data-view-count="saved">0</span></button>
        <button type="button" class="view-button" data-view="dismissed" aria-pressed="false">Dismissed <span data-view-count="dismissed">0</span></button>
      </div>
    </div>
    <div class="triage-secondary">
      <label class="select-label">Representative outlet
        <select id="source-filter"><option value="">All outlets</option></select>
      </label>
      <label class="select-label">Order
        <select id="sort-by">
          <option value="score">Highest score</option>
          <option value="newest">Newest first</option>
          <option value="source">Outlet A–Z</option>
        </select>
      </label>
      <button type="button" class="reset-button" id="reset-filters">Reset filters</button>
    </div>
    <div class="signal-filter-row">
      <span class="filter-label">Signal</span>
      <div class="signal-filters" id="signal-filters" role="group" aria-label="Filter by signal">
        <button type="button" class="signal-button is-active" data-signal="all" aria-pressed="true">All signals</button>
        <button type="button" class="signal-button" data-signal="headline_label" aria-pressed="false">Headline label</button>
        <button type="button" class="signal-button" data-signal="exclusivity" aria-pressed="false">Exclusivity</button>
        <button type="button" class="signal-button" data-signal="sourcing_depth" aria-pressed="false">Sourcing depth</button>
        <button type="button" class="signal-button" data-signal="primary_source" aria-pressed="false">Primary source</button>
        <button type="button" class="signal-button" data-signal="institutional_record" aria-pressed="false">Institutional record</button>
      </div>
    </div>
    <div class="results-line">
      <span id="results-summary" role="status" aria-live="polite">Showing {total_shown} of {total_shown} leads</span>
      <span class="local-note">Saved and dismissed choices stay in this browser only.</span>
    </div>
  </section>

  <main id="story-results">
    {''.join(bucket_sections)}
  </main>

  <details class="feed-status">
    <summary>Feed status this run · {sum(1 for _,s in feed_status if s.startswith('OK'))}/{len(feed_status)} working</summary>
    <table><tbody>{feed_rows}</tbody></table>
  </details>

  <footer>
    <div><strong>Scoop Tracker · Tier 1 heuristics</strong></div>
    <p>Rolling {window_hours}-hour window. The full article-level history remains in the database for future analysis.
    The ranking and similarity groups are navigation aids, not editorial conclusions or verification.</p>
  </footer>
</div>
</body>
</html>"""
    with open(path, "w") as f:
        f.write(doc)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scoop_tracker.db")
    parser.add_argument("--window-hours", type=int, default=72)
    parser.add_argument("--per-source-cap", type=int, default=8)
    parser.add_argument("--top-n-per-bucket", type=int, default=20,
                         help="Max stories shown per recency section (Last 6h / 6-24h / 24-72h), after the per-source cap.")
    args = parser.parse_args()
    run_pipeline(db_path=args.db, report_window_hours=args.window_hours,
                 per_source_cap=args.per_source_cap, top_n_per_bucket=args.top_n_per_bucket)
