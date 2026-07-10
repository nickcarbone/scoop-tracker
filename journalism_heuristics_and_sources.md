# Heuristic Terms — Vetted

## Added ✅

| Term | Category | Rationale |
|---|---|---|
| First reported by | new: attribution phrase | Distinct from "first on" — this credits *another* outlet, which is a miniature version of the Tier 2 downstream-citation signal, detectable even in Tier 1 scope. |
| Obtained by (passive) | exclusivity | Existing regex only caught active voice ("we obtained," "has obtained"). "Documents obtained by [outlet]" is the far more common construction and was silently missed. |
| Newly obtained | exclusivity | Specific enough to be low-noise, genuinely distinct phrasing from "obtained by." |
| Confidential | sourcing_depth | Reasonably specific in a news context, unlike "secret" (see rejected list). |
| Draft report | sourcing_depth | Specific compound phrase — reliably indicates a pre-publication/leaked document, low false-positive risk. |
| Audio obtained / Video obtained | primary_source | Merged into one pattern `(audio|video) obtained` — same logic as documents, distinct enough from generic "obtained." |
| Sources/people familiar with | sourcing_depth (broadened) | **This exposed a real bug**: the existing pattern only matched the exact phrase "people familiar with the matter." "Sources familiar with the discussions," "people familiar with the plans," etc. — much more common in practice — were silently missed. Broadened the regex rather than adding a redundant new one. |
| Review of records | sourcing_depth (broadened) | Same kind of bug: existing pattern required the literal word "a" immediately before "review of," so "ProPublica's review of records" didn't match. Broadened to bare "review of." |
| Federal records | primary_source | Specific, genuine, adds coverage the existing patterns didn't have. |
| Special Report | headline_label | Real editorial convention (used as a formatted prefix, same logic as "Scoop:"). Added to the headline-label tier, not the noisier body-text tier. |
| Visual investigation | headline_label | Distinctive two-word compound (NYT/BBC use this as an actual section label) — low false-positive risk, added as a headline label. |
| Inspector General report | new: institutional_record | Genuine, specific, high-value — government watchdog reports are exactly the kind of institutional grounding worth flagging distinctly. |
| Congressional report | institutional_record | Same logic. |
| SEC filing / DOJ filing | institutional_record | Specific and genuine. Also worth noting: this is the *text-mention* signal, separate from actually cross-checking EDGAR (that's still Tier 3 — see prior discussion). |
| NTSB report / FAA report | institutional_record | Narrow but specific (aviation/transportation safety) — low false-positive risk precisely because they're narrow. |
| FDA documents | institutional_record | Specific, genuine, useful for health/pharma investigative angles. |
| PACER | institutional_record | Specific federal court records system — genuine signal, ties to the Tier 3 discussion. |

**New category added: `institutional_record`**, separate from `primary_source`. These are conceptually primary sources too, but kept distinct so the report's category tags tell you *which kind* of grounding a story has — useful diagnostically, and it's the natural landing spot for future Tier 3 EDGAR/PACER/congress.gov cross-checks.

## Discarded ❌

| Term | Why |
|---|---|
| Learned by | Awkward/uncommon phrasing in practice — "has learned" (already covered) is the actual construction outlets use. Adding this separately would add noise without adding recall. |
| Documents obtained | Already covered by the existing `documents? (obtained\|reviewed) by` pattern (now also fixed for pluralization — see below). |
| Internal memo | Already covered — but this review caught that the existing pattern only matched singular ("email," "memo," "document"), not plurals. **Fixed the existing pattern** rather than adding a new one: `internal (memo\|email\|document)s?`. |
| Secret | Too broad — "secret menu," "secret to success," "Victoria's Secret" are all far more common than any newsworthy use. This would flood the scoring with false positives. If you want this signal, it needs to be a specific phrase ("according to a secret memo"), not the bare word. |
| Investigation / Investigates | Bare form is too broad — "police investigation underway" is routine crime-blotter language, not a signal of resourced journalism. The existing higher-precision version, "an investigation by [X]," is already in the sourcing_depth list and does the actual work here. |
| Analysis | Appears as a routine section label on nearly every outlet for any opinion/analysis piece. Would match a huge volume of content regardless of depth or exclusivity — pure noise. |
| Explainer | This is evergreen reference content — the functional opposite of a must-read scoop. Including it would actively work against the goal. |
| Series | Too common a word outside journalism (TV series, World Series) to use bare. If you want "part of an ongoing investigative series" as a signal, it needs to be a longer, more specific phrase — not worth it for Tier 1. |
| Project | Extremely common word in any policy/construction/tech context. Discarded — no plausible way to make this precise without a much longer phrase. |
| Enterprise | This is journalism-industry jargon *about* a story (used in awards categories, internally) — it essentially never appears as literal text within a published article or headline. Low recall makes it not worth adding as a text-matching signal. |
| Interactive | Same issue as "Enterprise" — a content-type tag, not something that reliably appears as scannable text in the RSS title/body. |
| Timeline | Too generic — most articles include boilerplate "timeline of events" structure regardless of whether the piece is investigative. Discarded. |
| Exclusive interview | Redundant — already caught by the existing bare `\bexclusive\b` pattern. Adding a longer, more specific variant on top gives no incremental recall. |

---

# Sources — Vetted

Tested every candidate against a live fetch (not memory) before deciding. Outcomes fell into four real
buckets, and they get different treatment:

- **Confirmed working** — 200 response, real XML/RSS content. Added.
- **Sandbox-blocked, likely fine elsewhere** — this container blocks the hostname outright (`x-block-reason:
  hostname_blocked`), same issue as NYT/Politico/etc. from the last round. Added anyway, same as before,
  since it's an environment limitation here, not a dead URL.
- **Dead/wrong URL** — 404, or a 200 that's actually an HTML page, not a feed. Not added. Some of these
  (OCCRP, Marshall Project, Kyiv Independent, Nikkei Asia) are genuinely valuable outlets — flagged for a
  follow-up pass to find the correct current path rather than fully discarded as a concept.
- **Site's own bot-block** — a 403 that is *not* the sandbox's block (different response size/body), meaning
  the outlet itself is blocking scraped requests. This would also hit you outside the sandbox; it's the same
  category as AP/The Information from the last round and needs a different fetch strategy, not a different URL.

## Added ✅

**Confirmed working (200, real feed content):** Wall Street Journal, ABC News, CBS News, NBC News, CNN,
MIT Technology Review, Platformer, Rest of World, Bellingcat, ICIJ, The Markup, Documented, Texas Tribune,
Mississippi Today, Wisconsin Watch, Defense One, Just Security, The War Zone, The Cipher Brief, Fortune,
Inside Climate News, Carbon Brief, Grist, Nature News, Science, STAT, Courthouse News, SCOTUSblog,
Krebs on Security, Recorded Future News, Dark Reading, Al Jazeera English, South China Morning Post,
CBC News, Seattle Times.

**Sandbox-blocked here, added anyway (untested in this environment, standard URLs, likely fine on your
machine):** Financial Times, Los Angeles Times, USA Today, Ars Technica, New Scientist, Reveal, MarketWatch,
Der Spiegel International, El Pais English, ABC Australia, Miami Herald, Chicago Tribune, Houston Chronicle,
San Francisco Chronicle, Sacramento Bee, Barron's, Sueddeutsche Zeitung, Intelligence Online.

## Discarded ❌ (this round)

| Source | Why |
|---|---|
| OCCRP | 404 on the URL tested — real, valuable investigative consortium, but needs the correct current RSS path. Worth a follow-up, not a real "no." |
| Marshall Project | Same — 404, needs correct path. Genuinely useful (criminal justice investigative reporting), revisit. |
| Kyiv Independent | 404, needs correct path. |
| Nikkei Asia | 404, needs correct path. |
| The Globe and Mail | 404, needs correct path. |
| Heatmap | 404, needs correct path (relatively new outlet, RSS conventions may differ from what was tried). |
| Meduza (English) | Effectively empty response — needs correct path, or may not have a stable public English RSS. |
| Haaretz | Returned 200 but the body was a full HTML page, not RSS — wrong URL. |
| Times of Israel | 403, but not the sandbox's block signature — this is the outlet's own bot protection. Same category as AP/The Information: needs browser automation or an official feed, not just a different URL. |
| Breaking Defense | 403, outlet's own block (small response body, not the sandbox's). |
| Lawfare | 403, outlet's own block. |
| Boston Globe, Atlanta Journal-Constitution, Philadelphia Inquirer | 404 on the paths tried. Also worth being honest about scope here: even if fixed, adding every regional metro daily risks diluting a scoops/exclusives tracker with routine local news that rarely produces cross-topic must-reads. Texas Tribune, Miami Herald, and Seattle Times already give reasonable regional-investigative coverage — recommend not chasing all nine metro dailies down, only adding more if a specific one has a track record you actually want (e.g. Tribune's investigative unit specifically). |
| Le Monde (English), Süddeutsche Zeitung (German) | **Flagging a real design issue, not just a fetch issue**: even where these are reachable, adding non-English sources right now provides no value — every Tier 1 heuristic (score.py) is an English-language regex. A German or French article will simply never match anything and will sit in the ingested pile scoring zero, cluttering the pipeline without being wrong, exactly. If international coverage matters to you, the heuristics need translated marker sets per language before non-English sources are worth adding — not just a working feed URL. Süddeutsche in particular has no English edition at all, which makes this issue unavoidable rather than a translation-of-convenience question. |
| Law360 | No public RSS found — subscription-gated legal trade press, consistent with its business model (this is the same category as The Information's and AP's bot-blocking, but here it looks like there's genuinely no public feed rather than a scraping obstacle). |

## Note on duplication

"Recorded Future News" and "The Record" in the original list are the same outlet — Recorded Future's news
arm was renamed and consolidated under therecord.media. Added once, under its current name.


