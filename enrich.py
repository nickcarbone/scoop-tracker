"""Piece 3: Enrichment — fetch each article, extract byline count + metadata from
schema.org NewsArticle JSON-LD when available. Falls back to RSS-provided authors,
then to a default of 1, if the page can't be fetched or parsed (paywalls, blocks, etc.
are expected and handled, not treated as fatal)."""
import json
import re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

def _flatten_graph(data):
    """JSON-LD sometimes nests entities under an @graph array (common WordPress/Yoast
    pattern), and refers to authors by @id pointer to a separate Person node rather
    than inlining the name. Flatten to a single list and build an @id -> node lookup."""
    if isinstance(data, dict) and "@graph" in data and isinstance(data["@graph"], list):
        nodes = data["@graph"]
    elif isinstance(data, list):
        nodes = data
    else:
        nodes = [data]
    by_id = {n["@id"]: n for n in nodes if isinstance(n, dict) and "@id" in n}
    return nodes, by_id

def extract_jsonld_article(html):
    """Look for schema.org NewsArticle/Article JSON-LD block and pull byline + metadata."""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            continue
        nodes, by_id = _flatten_graph(data)
        for item in nodes:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            type_str = " ".join(item_type) if isinstance(item_type, list) else str(item_type)
            if "Article" in type_str or "NewsArticle" in type_str:
                author = item.get("author", [])
                if isinstance(author, dict):
                    author = [author]
                author_names = []
                for a in author:
                    if not isinstance(a, dict):
                        continue
                    if "name" in a:
                        author_names.append(a["name"])
                    elif "@id" in a and a["@id"] in by_id:
                        resolved = by_id[a["@id"]]
                        if "name" in resolved:
                            author_names.append(resolved["name"])
                return {
                    "byline_count": max(len(author_names), 1),
                    "author_names": author_names,
                    "date_published": item.get("datePublished", ""),
                    "word_count": item.get("wordCount"),
                    "article_body_snippet": (item.get("articleBody") or "")[:2000],
                    "found_jsonld": True,
                }
    return None

def enrich_one(article, timeout=5):
    """Fetch one article's page and enrich it. Never raises — always returns a dict."""
    result = dict(article)
    result["byline_count"] = max(len(article.get("authors_rss", [])), 1)
    result["found_jsonld"] = False
    result["article_body_snippet"] = ""
    try:
        r = requests.get(article["link"], headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            jsonld = extract_jsonld_article(r.text)
            if jsonld:
                result.update(jsonld)
    except Exception:
        pass  # graceful degradation — RSS-derived fields remain as fallback
    return result

def enrich_all(articles, max_workers=30, verbose=True):
    enriched = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(enrich_one, a): a for a in articles}
        done = 0
        for fut in as_completed(futures):
            enriched.append(fut.result())
            done += 1
            if verbose and done % 25 == 0:
                print(f"  enriched {done}/{len(articles)}")
    return enriched

if __name__ == "__main__":
    from ingest import ingest_all
    entries, _ = ingest_all(verbose=False)
    print(f"Enriching {len(entries)} articles (fetching pages for byline data)...")
    enriched = enrich_all(entries[:30])  # small sample for a quick test
    jsonld_hits = sum(1 for e in enriched if e["found_jsonld"])
    print(f"JSON-LD byline data found on {jsonld_hits}/{len(enriched)} sampled articles")
    multi_byline = [e for e in enriched if e["byline_count"] > 1]
    print(f"Multi-byline articles in sample: {len(multi_byline)}")
    for e in multi_byline[:5]:
        print(f"  [{e['byline_count']} authors] {e['title'][:70]}")
