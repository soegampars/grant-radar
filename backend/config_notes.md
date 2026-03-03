# config.json — Field Reference

Since JSON does not support comments, this file documents non-obvious
fields and design decisions in `config.json`.

---

## researcher.academic_status.urgency

PNRR funding ends October 2026 with no extension. This field tells the
analyser to weight opportunities starting Q4 2026–Q2 2027 more highly,
but **never** to exclude later ones.

## researcher.education[1].note (MSc Groningen)

A Dutch MSc satisfies grant requirements phrased as "European
postgraduate degree" or "EEA degree", but does **not** satisfy
"British degree" or "UK university graduate" requirements. The analyser
uses this to flag eligibility accurately.

## researcher.eligibility.eu_residency

Italian residence permit is tied to the PhD enrolment and is
time-limited. Positions requiring "EU citizen" status are **not**
automatically satisfied — these should be flagged as `CHECK` rather
than marked eligible or ineligible.

## researcher.eligibility.disqualifying_conditions

These are conditions that, if a grant requires them, make the
researcher **ineligible**. The analyser should flag these clearly but
still include the grant in the output (per `design_principles`).

## researcher.research_identity_note

Skills listed in the profile are current capabilities, not hard
filters. A postdoc in "urban economics" or "development policy" that
doesn't require spatial econometrics is still highly relevant. The
analyser should match on broad theme ("economic geography for policy")
first, and treat specific method overlap as a bonus.

## researcher.languages.Italian

Marked explicitly as NOT professional-level. Positions requiring
Italian-language teaching or professional Italian communication should
be flagged as a potential barrier, not silently excluded.

---

## tiers

Priority tiers control notification urgency and dashboard sort order.

| Tier | Name | Notification | Rationale |
|------|------|-------------|-----------|
| 1 | Cardiff + Welsh | Immediate email | Existing host connection (Prof Huggins) makes these high-probability |
| 2 | Strong Alternatives | Weekly digest (prominent) | Known institutions with existing academic ties |
| 3 | Good European | Weekly digest | Broad net across European economic geography |
| 4 | Indonesia: BRIN/UGM | Weekly digest | Home country fallback; UGM alumnus + IRSA network |

### tiers.1.portable_fellowships

Tier 1 includes not just Cardiff-advertised positions but also
fellowships from any funder that can be **taken to** Cardiff (e.g.,
British Academy, Leverhulme, Marie Curie). The analyser should
identify portable fellowships and flag them for Tier 1 if Cardiff is a
plausible host.

### tiers.2.subcategories.b_netherlands

The Groningen MSc creates a Dutch academic connection. NWO (Dutch
Research Council) grants sometimes require a degree from a Dutch
institution — this may make the researcher eligible.

### tiers.2.subcategories.c_polimi

"Assegno di ricerca" is the Italian postdoctoral research fellowship
format. Search should include this Italian term alongside English
equivalents.

---

## timeline.note

**Critical design rule**: The system must NEVER exclude or hide a grant
because its start date falls outside the ideal window. Timeline
mismatches are communicated through scoring penalties and visual flags
on the dashboard, not by omission.

---

## sources — Architecture (updated)

The original RSS feeds (EconJobMarket XML, jobs.ac.uk RSS, Academic
Transfer RSS) all stopped working. Sources are now split into three
categories:

### sources.rss_feeds

Standard RSS/Atom feeds parsed by `rss_fetcher.py`. Currently only:
- **arXiv Economics** (`https://rss.arxiv.org/rss/econ`) — valid RSS
  2.0, ~47 entries/day. Contains research papers, not job ads, but
  catches working papers that may reference postdoc openings.

### sources.api_endpoints

Custom JSON/XML APIs with dedicated parsers:
- **EconJobMarket** — their XML endpoint is deprecated; the only
  supported format is JSON at
  `https://backend.econjobmarket.org/data/zz_public/json/Ads`.
  Returns a bare JSON array of ad objects with fields: `adtitle`,
  `adtext` (HTML), `startdate`, `enddate`, `department`, `name`
  (institution), `url`, `position_types`, `categories`.
  Parsed by `ejm_fetcher.py`.

### sources.web_scrapers

HTML scraping for sites without feeds or APIs. Each entry has a
`"scraper"` key that selects the parsing function in `web_scraper.py`.

| scraper ID | Site | Strategy |
|-----------|------|----------|
| `jobs_ac_uk` | jobs.ac.uk | HTML scrape of search results. RSS was removed. Job links match `a[href^="/job/"]`. Site uses Vue.js but SSR'd HTML contains listings. |
| `academic_transfer` | Academic Transfer (NL) | Sitemap + JSON-LD. RSS returns 404. Site is a Nuxt 3 SPA. We fetch `sitemap-vacancies.xml`, take the 30 most recent EN job URLs, and extract `schema.org/JobPosting` JSON-LD from each page. |
| `euraxess` | EURAXESS | HTML scrape of search results + detail pages. Drupal 10 site requires browser User-Agent (403 otherwise). Fetches 2 pages of results (20 jobs), then each detail page for full description, requirements, and metadata. |
| `rsa` | Regional Studies Association | WordPress RSS feed at `/feed/?post_type=news&paged=N`. Listing page is AJAX-only (unusable with requests). Fetches 2 pages (~40 articles) via feedparser, extracts full content from `<content:encoded>`. |
| `ersa` | ERSA | HTML scrape of 3 WordPress pages: `/vacancies/`, `/calls/calls-for-publications/`, `/calls/calls-for-event-submissions/`. Free-form HTML parsed using heading delimiters (`<strong>`, `<h3>`, `<h4>`). Deadlines extracted via regex. |

### sources.web_scrapers[].params

Query parameters appended to the base URL when scraping. For EURAXESS,
this pre-filters to economics + experienced researcher career stage to
reduce noise.

---

## models

| Key | Model | Used for |
|-----|-------|----------|
| daily_analysis | claude-sonnet-4-6 | Scoring each grant against the profile (fast, cost-efficient) |
| weekly_discovery | claude-opus-4-6 | Broad web search for opportunities not in RSS/scrape sources |
| weekly_reeval | claude-opus-4-6 | Re-scoring the full grant database with deeper analysis |

---

## design_principles

These are injected into every Claude API prompt as system-level
instructions. They override any tendency the model might have to
"helpfully" filter out seemingly irrelevant results.

- **never_exclude**: Collect everything in the broad domain.
- **only_hard_exclusion**: Only skip clearly non-academic or
  non-economics postings.
- **eligibility_uncertain**: When in doubt, flag — don't filter.
