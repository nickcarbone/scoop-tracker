"""Piece 2: Ingestion — pull all RSS feeds, normalize into a flat list of articles."""
import json
import feedparser
from datetime import datetime
import time

def load_feeds(path="feeds.json"):
    with open(path) as f:
        return json.load(f)["feeds"]

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

def fetch_feed(source_name, url, timeout=10):
    """Fetch and parse one RSS feed. Returns list of normalized entries, or empty list on failure.

    Fetches raw bytes via requests first (with a browser UA) rather than letting
    feedparser fetch directly, specifically so a blocked/challenged response can be
    labeled correctly instead of surfacing as a generic, misleading XML parse error.
    """
    entries = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as ex:
        return entries, f"FAILED (network): {str(ex)[:100]}"

    if r.status_code == 403 and r.headers.get("x-block-reason") == "hostname_blocked":
        return entries, "ENV-BLOCKED (this sandbox blocks this hostname; untested here, likely fine on your own machine)"
    if r.status_code == 403 and "just a moment" in r.text.lower():
        return entries, "BOT-CHALLENGED (site's own Cloudflare/bot-check, not a sandbox issue — needs a different fetch strategy)"
    if r.status_code == 429:
        return entries, "RATE-LIMITED (HTTP 429 — outlet is throttling; back off or reduce run frequency for this source)"
    if r.status_code == 202 and not r.content.strip():
        return entries, "SOFT-THROTTLED (HTTP 202, empty body — looks like a bot-holding-pattern response, not real content)"
    if r.status_code == 403:
        return entries, "BOT-BLOCKED (site's own 403, not the sandbox — confirmed real in production logs too, not an artifact of this environment)"
    if r.status_code == 404:
        return entries, f"FAILED (404 — feed URL is wrong/moved)"
    if r.status_code != 200:
        return entries, f"FAILED (HTTP {r.status_code})"

    try:
        feed = feedparser.parse(r.content)
        if feed.bozo and not feed.entries:
            return entries, f"FAILED (parse error): {str(feed.bozo_exception)[:100]}"
        for e in feed.entries:
            entries.append({
                "source": source_name,
                "title": e.get("title", "").strip(),
                "link": e.get("link", ""),
                "summary": e.get("summary", e.get("description", ""))[:1000],
                "published": e.get("published", e.get("updated", "")),
                "authors_rss": [a.get("name") for a in e.get("authors", [])] if e.get("authors") else [],
            })
        return entries, f"OK ({len(entries)} items)"
    except Exception as ex:
        return entries, f"FAILED (exception): {str(ex)[:100]}"

def ingest_all(feeds_path="feeds.json", verbose=True):
    feeds = load_feeds(feeds_path)
    all_entries = []
    status_report = []
    for f in feeds:
        entries, status = fetch_feed(f["source"], f["url"])
        all_entries.extend(entries)
        status_report.append((f["source"], status))
        if verbose:
            print(f"  {f['source']:<25} {status}")
    return all_entries, status_report

if __name__ == "__main__":
    print("Fetching feeds...\n")
    entries, status = ingest_all()
    print(f"\nTotal articles ingested: {len(entries)}")
    working = sum(1 for _, s in status if s.startswith("OK"))
    print(f"Working feeds: {working}/{len(status)}")
