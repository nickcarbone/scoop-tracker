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
_STORY_CLUSTER_THRESHOLD = 0.35  # loose, per working-style discussion

def _title_tokens(title):
    cleaned = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    return {t for t in cleaned.split() if t not in _STOPWORDS and len(t) > 2}

def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def cluster_articles(articles, threshold=_STORY_CLUSTER_THRESHOLD):
    """Union-find clustering on title token overlap. O(n^2) comparisons —
    fine at rolling-window scale (low hundreds of articles). Revisit with
    blocking (e.g. by day, or by shared proper-noun entities) if the window
    ever grows large enough for the O(n^2) pass to become a real cost —
    untested at that scale, not a hypothetical problem to pre-solve now."""
    n = len(articles)
    tokens = [_title_tokens(a["title"]) for a in articles]
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
            if _jaccard(tokens[i], tokens[j]) >= threshold:
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
    race rather than a meaningful gap."""
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
    articles = db.recent_articles(conn, hours=window_hours, limit=2000)
    for a in articles:
        a["is_new"] = a["link"] in new_urls
    with open(path, "w") as f:
        json.dump(articles, f, indent=2)

def render_rows(selected, new_urls=frozenset()):
    rows = []
    for i, a in enumerate(selected, 1):
        cats = a.get("matched_categories", [])
        cat_tags = "".join(f'<span class="tag tag-{c}">{c.replace("_"," ")}</span>' for c in cats)
        byline_note = f'{a["byline_count"]} bylines' if a["byline_count"] > 1 else "1 byline"
        others = a.get("_cluster_others", [])
        # "New" if the representative OR any collapsed duplicate was newly
        # scored this run — a story is new-to-you even if the specific
        # outlet showing as the representative happened to be seen earlier.
        is_new = a["link"] in new_urls or any(o["link"] in new_urls for o in others)
        new_badge = '<span class="badge-new">NEW</span> ' if is_new else ''

        also_covered_html = ""
        if others:
            other_links = ", ".join(
                f'<a href="{esc(o["link"])}" target="_blank">{esc(o["source"])}</a>'
                for o in others[:8]
            )
            overflow = f" +{len(others) - 8} more" if len(others) > 8 else ""
            also_covered_html = f'<div class="also-covered">Also covered by: {other_links}{overflow}</div>'

        rows.append(f"""
        <tr class="{'is-new' if is_new else ''}">
          <td class="rank">{i:02d}</td>
          <td class="score">{a['total_score']}</td>
          <td class="story">
            {new_badge}<a href="{esc(a['link'])}" target="_blank">{esc(a['title'])}</a>
            <div class="meta">{esc(a['source'])} &middot; {byline_note}{(' &middot; ' + esc(', '.join(a['author_names'][:3]))) if a.get('author_names') else ''}</div>
            <div class="tags">{cat_tags}</div>
            {also_covered_html}
          </td>
        </tr>""")
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
        total_new_shown += sum(1 for a in selected if a["link"] in new_urls)
        rows_html = render_rows(selected, new_urls)
        bucket_sections.append(f"""
  <section class="bucket">
    <h2 class="bucket-title">{esc(label)} <span class="bucket-count">({len(selected)} of {len(bucket_articles)} flagged)</span></h2>
    <table><tbody>{rows_html if rows_html else '<tr><td class="empty" colspan="3">Nothing flagged in this window.</td></tr>'}</tbody></table>
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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SCOOP TRACKER — Tier 1 Wire</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Courier+Prime:wght@400;700&display=swap');
  :root {{
    --bg: #0f0e0d; --panel: #171513; --line: #332e29; --paper: #e8e2d4;
    --ink: #cfc8b8; --red: #b6432c; --dim: #7a7368; --gold: #d4a13d;
  }}
  * {{ box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--paper); font-family: 'Courier Prime', monospace; margin: 0; padding: 24px 16px 60px; }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  header {{ border-bottom: 3px double var(--red); padding-bottom: 14px; margin-bottom: 18px; }}
  h1 {{ font-family: 'Bebas Neue', sans-serif; font-size: 44px; letter-spacing: 2px; color: var(--paper); margin: 0; line-height: 0.95; }}
  h1 span {{ color: var(--red); }}
  .subhead {{ font-size: 12px; color: var(--dim); letter-spacing: 1px; text-transform: uppercase; margin-top: 6px; }}
  .stats {{ display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: var(--dim); margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }}
  .stats b {{ color: var(--paper); }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 10px 8px; vertical-align: top; border-bottom: 1px solid var(--line); }}
  .rank {{ font-family: 'Bebas Neue', sans-serif; font-size: 20px; color: var(--dim); width: 34px; }}
  .score {{ font-family: 'Bebas Neue', sans-serif; font-size: 26px; color: var(--red); width: 44px; }}
  .story a {{ color: var(--paper); text-decoration: none; font-weight: 700; font-size: 15px; }}
  .story a:hover {{ color: var(--red); text-decoration: underline; }}
  .meta {{ color: var(--dim); font-size: 11px; margin-top: 4px; }}
  .tags {{ margin-top: 6px; }}
  .tag {{ display: inline-block; font-size: 10px; letter-spacing: 0.5px; text-transform: uppercase; padding: 2px 7px; border-radius: 2px; margin-right: 5px; border: 1px solid var(--line); color: var(--ink); }}
  .tag-exclusivity {{ border-color: var(--red); color: var(--red); }}
  .also-covered {{ color: var(--dim); font-size: 11px; margin-top: 6px; }}
  .also-covered a {{ color: var(--ink); }}
  tr.is-new {{ background: rgba(212, 161, 61, 0.07); }}
  .badge-new {{ display: inline-block; background: var(--gold); color: #171513; font-family: 'Bebas Neue', sans-serif; font-size: 11px; letter-spacing: 1px; padding: 2px 6px; border-radius: 2px; vertical-align: middle; margin-right: 2px; }}
  .bucket {{ margin-top: 26px; }}
  .bucket-title {{ font-family: 'Bebas Neue', sans-serif; font-size: 22px; letter-spacing: 1px; color: var(--paper); border-bottom: 1px solid var(--line); padding-bottom: 6px; margin: 0 0 4px; }}
  .bucket-count {{ font-family: 'Courier Prime', monospace; font-size: 11px; color: var(--dim); letter-spacing: 0; text-transform: none; }}
  .empty {{ color: var(--dim); font-size: 12px; padding: 12px 8px; border-bottom: none; }}
  details {{ margin-top: 30px; border-top: 1px solid var(--line); padding-top: 14px; }}
  summary {{ cursor: pointer; color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  .fail {{ color: #6b5a52; }}
  .ok {{ color: #7a9a7e; }}
  footer {{ margin-top: 30px; color: var(--dim); font-size: 11px; border-top: 1px solid var(--line); padding-top: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>SCOOP <span>TRACKER</span></h1>
    <div class="subhead">Tier 1 Wire &middot; DB-backed, {window_hours}h rolling window &middot; No convergence/citation checks yet</div>
    <div class="stats">
      <div>Generated <b>{now}</b></div>
      <div><b>{total_in_window}</b> articles in window</div>
      <div><b>{total_flagged}</b> flagged (score &gt; 0)</div>
      <div><b>{total_stories}</b> distinct stories ({total_collapsed} duplicate write-ups collapsed)</div>
      <div><b>{total_shown}</b> shown (capped at {per_source_cap}/source per time window)</div>
      <div><b>{total_new_shown}</b> new this run</div>
    </div>
  </header>
  {''.join(bucket_sections)}
  <details>
    <summary>Feed status, this run ({sum(1 for _,s in feed_status if s.startswith('OK'))}/{len(feed_status)} working)</summary>
    <table style="margin-top:10px;">{feed_rows}</table>
  </details>
  <footer>
    Tier 1 heuristics only. Rolling {window_hours}-hour window shown; full history accumulates in scoop_tracker.db
    for future reporter-authority scoring and publishing-pattern baselines. High score = worth a human's attention,
    not a verified scoop.
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
