"""Persistence layer. Two tables:

- articles: one row per unique URL, permanent record. This is what makes Tier 2
  (reporter-authority scoring, publishing-pattern baselines) possible later — none
  of that works from a single run, it needs accumulated history.
- feed_runs: one row per feed per pipeline run, tracking health over time. Lets you
  eventually see "this feed has been silently failing for 3 days" instead of just
  the current run's status.
"""
import sqlite3
import json
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    summary TEXT,
    published TEXT,
    first_seen_at TEXT,
    byline_count INTEGER,
    author_names TEXT,
    date_published TEXT,
    word_count INTEGER,
    found_jsonld INTEGER,
    keyword_score INTEGER,
    byline_bonus INTEGER,
    total_score INTEGER,
    matched_categories TEXT
);

CREATE TABLE IF NOT EXISTS feed_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT,
    source TEXT,
    status TEXT,
    item_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_articles_first_seen ON articles(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(total_score);
CREATE INDEX IF NOT EXISTS idx_feed_runs_source ON feed_runs(source);
"""

def connect(db_path="scoop_tracker.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn

def known_urls(conn):
    """Return the set of URLs already persisted — used to skip re-enriching
    articles we've already fetched and scored in a prior run."""
    return set(row[0] for row in conn.execute("SELECT url FROM articles"))

def get_cached(conn, url):
    """Return the persisted enrichment/score fields for a known URL, or None."""
    row = conn.execute(
        "SELECT byline_count, author_names, date_published, word_count, found_jsonld "
        "FROM articles WHERE url = ?", (url,)
    ).fetchone()
    if not row:
        return None
    return {
        "byline_count": row[0],
        "author_names": json.loads(row[1]) if row[1] else [],
        "date_published": row[2],
        "word_count": row[3],
        "found_jsonld": bool(row[4]),
    }

def upsert_articles(conn, scored_articles):
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for a in scored_articles:
        cats = sorted(set(h["category"] for h in a.get("score_hits", [])))
        rows.append((
            a["link"], a["source"], a["title"], a.get("summary", ""),
            a.get("published", ""), now, a.get("byline_count", 1),
            json.dumps(a.get("author_names", [])), a.get("date_published", ""),
            a.get("word_count"), int(a.get("found_jsonld", False)),
            a["keyword_score"], a["byline_bonus"], a["total_score"],
            json.dumps(cats),
        ))
    conn.executemany(
        """INSERT OR IGNORE INTO articles
           (url, source, title, summary, published, first_seen_at, byline_count,
            author_names, date_published, word_count, found_jsonld, keyword_score,
            byline_bonus, total_score, matched_categories)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()

def record_feed_runs(conn, feed_status):
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO feed_runs (run_at, source, status, item_count) VALUES (?,?,?,?)",
        [(now, source, status, _extract_count(status)) for source, status in feed_status],
    )
    conn.commit()

def _extract_count(status):
    if status.startswith("OK"):
        try:
            return int(status.split("(")[1].split()[0])
        except (IndexError, ValueError):
            return 0
    return 0

def recent_articles(conn, hours=72, limit=1000):
    """Pull articles first seen within the last N hours, for report generation.
    Keeps the report focused on what's current while the DB retains everything."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    rows = conn.execute(
        "SELECT url, source, title, byline_count, author_names, total_score, "
        "matched_categories, first_seen_at FROM articles "
        "ORDER BY total_score DESC LIMIT ?", (limit * 3,)  # overfetch, filter by time below
    ).fetchall()
    out = []
    for r in rows:
        try:
            seen_ts = datetime.fromisoformat(r[7]).timestamp()
        except (ValueError, TypeError):
            continue
        if seen_ts >= cutoff:
            out.append({
                "link": r[0], "source": r[1], "title": r[2], "byline_count": r[3],
                "author_names": json.loads(r[4]) if r[4] else [],
                "total_score": r[5], "matched_categories": json.loads(r[6]) if r[6] else [],
                "first_seen_at": r[7],
            })
    return out[:limit]

def feed_health_summary(conn, hours=24):
    """Per-source status from the most recent run in the window — used to flag
    feeds that have been silently dead across several scheduled runs, not just
    whether the current run happened to fail."""
    cutoff_iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc
    ).isoformat()
    rows = conn.execute(
        "SELECT source, status, COUNT(*) FROM feed_runs WHERE run_at >= ? "
        "GROUP BY source, status", (cutoff_iso,)
    ).fetchall()
    return rows
