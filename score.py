"""Piece 4: Scoring — Tier 1 heuristics only (self-declared markers + structural signals).
No downstream-citation or institutional-response checks yet — those need GDELT/EDGAR
integration, which is Tier 2/3 per the plan. This is deliberately a candidate-surfacing
score, not a verified-scoop score: a high score means "worth a human's attention,"
not "confirmed important." Self-declared exclusivity language is a weak, gameable
signal on its own (see: Brill's Content's 1998 Drudge audit), so it's weighted lower
than sourcing-depth language, which is harder to fake convincingly.
"""
import re

# Each phrase maps to a category and weight. Weights are a starting point —
# expect to retune these once you see how they perform against real judgment calls.
# A headline-position label ("Scoop: Senator to resign") is a much higher-confidence
# signal than the same word appearing mid-body ("an inside scoop on the bake sale").
# Axios formats every real scoop this way; treating that the same as a loose body-text
# hit was inflating precision-free matches. These patterns only match at the START
# of the title.
HEADLINE_LABEL_MARKERS = {
    r"^scoop:": 3,
    r"^exclusive:": 3,
    r"^first on\b": 3,
    r"^special report:": 3,
    r"^visual investigation:": 3,
}

EXCLUSIVITY_MARKERS = {
    r"\bexclusive\b": 1,
    r"\bscoop\b": 1,
    r"\bfirst on\b": 1,
    r"\bcan reveal\b": 2,
    r"\bhas learned\b": 2,
    r"\bhas obtained\b": 2,
    r"\bwe obtained\b": 2,
    r"\bobtained by\b": 2,          # passive form, e.g. "documents obtained by [outlet]"
    r"\bnewly obtained\b": 2,
}

SOURCING_DEPTH_MARKERS = {
    # broadened from "people familiar with the matter" (exact phrase only) to catch
    # the much more common "sources/people familiar with [X]" family of phrasing
    r"\b(people|sources) familiar with\b": 4,
    r"\baccording to (three|four|five|several|multiple) people\b": 4,
    r"\bspoke on condition of anonymity\b": 3,
    r"\bnot authorized to (speak|discuss)\b": 3,
    r"\baccording to documents\b": 4,
    r"\baccording to internal\b": 4,
    r"\bdocuments? (obtained|reviewed) by\b": 4,
    r"\breview of\b": 2,             # broadened from "a review of" — catches "ProPublica's review of records"
    r"\ban investigation by\b": 3,
    r"\bdeclined to comment\b": 2,
    r"\bdid not respond to (a )?request for comment\b": 1,
    r"\bconfidential\b": 2,
    r"\bdraft report\b": 3,
}

PRIMARY_SOURCE_MARKERS = {
    r"\bcourt filing[s]?\b": 3,
    r"\bcourt document[s]?\b": 3,
    r"\blawsuit\b": 2,
    r"\bfoia\b": 3,
    r"\bfreedom of information\b": 3,
    r"\bleaked\b": 3,
    r"\bleaked (recording|memo|email|audio|video)\b": 4,
    r"\b(audio|video) obtained\b": 4,
    r"\binternal (memo|email|document)s?\b": 4,   # fixed: now catches plurals too
    r"\bregulatory filing[s]?\b": 3,
    r"\bwhistleblower\b": 4,
    r"\bfederal records\b": 3,
}

# New category: named institutional/regulatory artifacts. These are conceptually
# primary sources too, but kept distinct so the report tags tell you *which kind*
# of grounding a story has — useful since these map directly to what Tier 3
# (EDGAR/congress.gov cross-checks) will eventually verify independently.
INSTITUTIONAL_RECORD_MARKERS = {
    r"\binspector general report\b": 4,
    r"\bcongressional report\b": 3,
    r"\bsec filing\b": 4,
    r"\bdoj filing\b": 4,
    r"\bntsb report\b": 4,
    r"\bfaa report\b": 4,
    r"\bfda documents?\b": 4,
    r"\bpacer\b": 3,
}

ALL_CATEGORIES = {
    "exclusivity": EXCLUSIVITY_MARKERS,
    "sourcing_depth": SOURCING_DEPTH_MARKERS,
    "primary_source": PRIMARY_SOURCE_MARKERS,
    "institutional_record": INSTITUTIONAL_RECORD_MARKERS,
}

def score_text(text):
    """Scan a block of text against all body-level marker categories. Returns (score, hits)."""
    text_lower = text.lower()
    total = 0
    hits = []
    for category, markers in ALL_CATEGORIES.items():
        for pattern, weight in markers.items():
            if re.search(pattern, text_lower):
                total += weight
                hits.append({"category": category, "pattern": pattern, "weight": weight})
    return total, hits

def score_headline_label(title):
    """Check the title specifically for a formatted exclusivity label at the start,
    e.g. Axios's 'Scoop:' convention. Kept separate from score_text because position
    in the headline is itself the signal — the same word buried mid-sentence doesn't
    carry the same editorial weight."""
    title_lower = title.lower().strip()
    total = 0
    hits = []
    for pattern, weight in HEADLINE_LABEL_MARKERS.items():
        if re.search(pattern, title_lower):
            total += weight
            hits.append({"category": "headline_label", "pattern": pattern, "weight": weight})
    return total, hits

def score_article(article):
    """Score one enriched article. Combines keyword score (title+summary+body snippet)
    with a byline-count bonus. Returns the article dict with score fields added."""
    title = article.get("title", "")
    text_blob = " ".join([title, article.get("summary", ""), article.get("article_body_snippet", "")])
    body_score, body_hits = score_text(text_blob)
    label_score, label_hits = score_headline_label(title)
    keyword_score = body_score + label_score
    hits = body_hits + label_hits

    byline_count = article.get("byline_count", 1)
    # Byline bonus: 2 points per additional author beyond the first, capped —
    # this rewards visibly resourced investigations without letting a 6-byline
    # wire roundup dominate the rankings.
    byline_bonus = min((byline_count - 1) * 2, 6)

    result = dict(article)
    result["keyword_score"] = keyword_score
    result["byline_bonus"] = byline_bonus
    result["total_score"] = keyword_score + byline_bonus
    result["score_hits"] = hits
    return result

def score_all(articles):
    scored = [score_article(a) for a in articles]
    scored.sort(key=lambda a: a["total_score"], reverse=True)
    return scored

if __name__ == "__main__":
    from ingest import ingest_all
    from enrich import enrich_all
    entries, _ = ingest_all(verbose=False)
    enriched = enrich_all(entries, verbose=False)
    scored = score_all(enriched)
    print(f"Scored {len(scored)} articles. Top 15 by score:\n")
    for a in scored[:15]:
        cats = sorted(set(h["category"] for h in a["score_hits"]))
        print(f"[{a['total_score']:>2}] ({a['source']}, {a['byline_count']} byline{'s' if a['byline_count']>1 else ''}) {a['title'][:75]}")
        if cats:
            print(f"      matched: {', '.join(cats)}")
