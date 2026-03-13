"""
web_scraper.py — Scrape grant listings from non-RSS web pages.

Implements source-specific scrapers dispatched via the "scraper" key
in each config entry under sources.web_scrapers:

  - jobs_ac_uk:        HTML search results from jobs.ac.uk
  - academic_transfer: Sitemap + JSON-LD from academictransfer.com
  - euraxess:          EURAXESS Drupal search results + detail pages
  - rsa:               RSA news RSS feed (listing page is AJAX-only)
  - ersa:              ERSA vacancies + calls pages (free-form HTML)

Each scraper returns a list of grant dicts in the standard schema.
Scraper failures are logged and recorded in data/run_status.json
but never crash the pipeline — partial results are always returned.
"""

import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.utils import generate_grant_id, load_json, save_json, update_run_status

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "GrantRadar/1.0 (academic grant monitoring)"
# Chrome-like UA for sites that block non-browser agents (EURAXESS)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
POLITE_DELAY = 1.5  # seconds between requests


# ─────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────

def _get(url: str, **kwargs) -> requests.Response | None:
    """HTTP GET with standard headers and error handling."""
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        logger.error(f"Request failed for {url}: {exc}")
        return None


def _get_browser(url: str, **kwargs) -> requests.Response | None:
    """HTTP GET with a browser-like User-Agent (for sites that block bots)."""
    headers = kwargs.pop("headers", {})
    headers["User-Agent"] = BROWSER_UA
    headers.setdefault("Accept", "text/html,application/xhtml+xml")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    return _get(url, headers=headers, **kwargs)


def _update_run_status(source_name: str, success: bool, count: int, error: str = ""):
    """Thin wrapper — delegates to the centralised utils.update_run_status."""
    update_run_status(
        source_name,
        status="success" if success else "error",
        error_msg=error or None,
        grants_found=count,
    )


# ─────────────────────────────────────────────────────────────────────
# Jobs.ac.uk scraper
# ─────────────────────────────────────────────────────────────────────

def _scrape_jobs_ac_uk(source_config: dict) -> list[dict]:
    """Scrape job listings from jobs.ac.uk search results.

    jobs.ac.uk removed their RSS feeds. The search results page
    returns server-rendered HTML with job cards inside
    div.j-search-result__text containers. Each card has:
      - Job title link: a[href^="/job/"]
      - Employer: .j-search-result__employer > b
      - Department: .j-search-result__department
      - Salary: .j-search-result__info (contains "Salary: ...")
      - Date placed: <strong>Date Placed: </strong>DD Mon
      - Location: plain div with "Location: ..." text

    Note: Closing dates only appear on individual job pages, not in
    search results. We leave deadline as None here — Claude can
    extract it from the job page URL if needed.
    """
    url = source_config["url"]
    name = source_config["name"]
    logger.info(f"Scraping {name}: {url}")

    resp = _get(url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    grants = []

    # Job title links follow the pattern /job/{ID}/{slug}
    job_links = soup.select('a[href^="/job/"]')
    seen_urls = set()

    for link in job_links:
        href = link.get("href", "")
        full_url = urljoin("https://www.jobs.ac.uk", href)

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        title = link.get_text(strip=True)
        if not title:
            continue

        # The card container is div.j-search-result__text
        card = link.find_parent("div", class_=re.compile(r"j-search-result"))
        if not card:
            card = link.find_parent("div") or link.parent
        card_text = card.get_text(" ", strip=True) if card else ""

        # Extract employer from .j-search-result__employer
        employer = ""
        department = ""
        if card and hasattr(card, "select_one"):
            emp_el = card.select_one(".j-search-result__employer")
            if emp_el:
                employer = emp_el.get_text(strip=True)
            dept_el = card.select_one(".j-search-result__department")
            if dept_el:
                department = dept_el.get_text(strip=True)

        # Extract "Date Placed: DD Mon" from card text
        date_posted = None
        date_match = re.search(
            r"Date\s+Placed\s*:?\s*(\d{1,2}\s+\w{3,9}(?:\s+\d{4})?)",
            card_text,
        )
        if date_match:
            date_posted = date_match.group(1)

        # Closing date is NOT in search results (only on detail pages)
        deadline = None

        # Extract salary from .j-search-result__info
        salary = ""
        if card and hasattr(card, "select_one"):
            salary_el = card.select_one(".j-search-result__info")
            if salary_el:
                salary_text = salary_el.get_text(strip=True)
                salary_text = re.sub(r"^Salary\s*:\s*", "", salary_text)
                salary = salary_text

        # Extract location from card text
        location = ""
        loc_match = re.search(r"Location\s*:\s*(.+?)(?=\s*Salary|\s*Date|$)", card_text)
        if loc_match:
            location = loc_match.group(1).strip()

        raw_parts = [
            f"Title: {title}",
            f"Employer: {employer}",
            f"Department: {department}",
            f"Location: {location}",
            f"Salary: {salary or 'Not specified'}",
            f"Date placed: {date_posted or 'N/A'}",
            f"URL: {full_url}",
            f"Context: {card_text[:1500]}",
        ]

        grant = {
            "id": generate_grant_id(full_url),
            "title": title,
            "url": full_url,
            "date_posted": date_posted,
            "deadline": deadline,
            "description": card_text[:500] if card_text else "",
            "source_name": name,
            "source_type": "web_scrape",
            "raw_content": "\n".join(raw_parts),
        }
        grants.append(grant)

    logger.info(f"  {name}: {len(grants)} listings scraped")
    return grants


# ─────────────────────────────────────────────────────────────────────
# Academic Transfer scraper (sitemap + JSON-LD)
# ─────────────────────────────────────────────────────────────────────

ACADEMIC_TRANSFER_SITEMAP = (
    "https://www.academictransfer.com/sitemap-vacancies.xml"
)
ACADEMIC_TRANSFER_MAX_PAGES = 30  # Limit individual fetches per run


def _scrape_academic_transfer(source_config: dict) -> list[dict]:
    """Scrape job listings from Academic Transfer via sitemap + JSON-LD.

    Academic Transfer (Netherlands) removed their RSS feed. The site
    is a Nuxt 3 SPA, but individual job pages embed schema.org
    JobPosting JSON-LD which is reliable and structured.

    Steps:
    1. Fetch the vacancies sitemap XML.
    2. Filter for English job URLs, sorted by lastmod (most recent first).
    3. Fetch each job page and extract the JSON-LD block.
    4. Convert to standardised grant dicts.
    """
    name = source_config["name"]
    logger.info(f"Scraping {name} via sitemap")

    # Step 1: Fetch sitemap
    resp = _get(ACADEMIC_TRANSFER_SITEMAP)
    if not resp:
        return []

    # Step 2: Parse sitemap XML
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.error(f"  Sitemap XML parse failed: {exc}")
        return []

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for url_el in root.findall("sm:url", ns):
        loc = url_el.findtext("sm:loc", default="", namespaces=ns)
        lastmod = url_el.findtext("sm:lastmod", default="", namespaces=ns)
        # Only English job pages (URL pattern: /en/jobs/{id}/{slug}/)
        if "/en/jobs/" in loc and loc.rstrip("/").count("/") >= 5:
            urls.append((loc, lastmod))

    # Sort by lastmod descending (most recent first)
    urls.sort(key=lambda x: x[1], reverse=True)
    urls = urls[:ACADEMIC_TRANSFER_MAX_PAGES]

    logger.info(
        f"  Sitemap: {len(urls)} recent EN job URLs to fetch"
    )

    # Step 3: Fetch each page and extract JSON-LD
    grants = []
    for i, (job_url, lastmod) in enumerate(urls):
        if i > 0:
            time.sleep(POLITE_DELAY)

        resp = _get(job_url)
        if not resp:
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        ld_script = soup.find("script", type="application/ld+json")
        if not ld_script or not ld_script.string:
            logger.warning(f"  No JSON-LD on {job_url}")
            continue

        try:
            ld_data = json.loads(ld_script.string)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(f"  JSON-LD parse failed on {job_url}: {exc}")
            continue

        # Handle @graph wrapper
        if isinstance(ld_data, dict) and "@graph" in ld_data:
            postings = [
                item for item in ld_data["@graph"]
                if item.get("@type") == "JobPosting"
            ]
            ld_data = postings[0] if postings else ld_data

        if not isinstance(ld_data, dict):
            continue
        if ld_data.get("@type") != "JobPosting":
            continue

        title = ld_data.get("title", "(no title)")

        org = ld_data.get("hiringOrganization", {})
        org_name = org.get("name", "") if isinstance(org, dict) else str(org)

        location_data = ld_data.get("jobLocation", {})
        city = ""
        if isinstance(location_data, dict):
            address = location_data.get("address", {})
            if isinstance(address, dict):
                city = address.get("addressLocality", "")

        deadline = ld_data.get("validThrough", "")
        if deadline:
            deadline = deadline[:10]  # Normalize to YYYY-MM-DD

        salary = ld_data.get("baseSalary", {})
        salary_str = ""
        if isinstance(salary, dict):
            min_s = salary.get("minValue", "")
            max_s = salary.get("maxValue", "")
            currency = salary.get("currency", "EUR")
            if min_s and max_s:
                salary_str = f"{currency} {min_s}-{max_s}"

        description = ld_data.get("description", "")

        raw_parts = [
            f"Title: {title}",
            f"Institution: {org_name}",
            f"Location: {city}",
            f"Deadline: {deadline or 'N/A'}",
            f"Salary: {salary_str or 'N/A'}",
            f"Employment type: {ld_data.get('employmentType', 'N/A')}",
            f"Description: {description}",
        ]

        grant = {
            "id": generate_grant_id(job_url),
            "title": title,
            "url": job_url,
            "date_posted": lastmod[:10] if lastmod else None,
            "deadline": deadline or None,
            "description": description,
            "source_name": name,
            "source_type": "web_scrape",
            "institution": org_name,
            "raw_content": "\n".join(raw_parts),
        }
        grants.append(grant)

    logger.info(f"  {name}: {len(grants)} listings scraped")
    return grants


# ─────────────────────────────────────────────────────────────────────
# EURAXESS scraper (search results + detail pages)
# ─────────────────────────────────────────────────────────────────────

EURAXESS_BASE = "https://euraxess.ec.europa.eu"
EURAXESS_SEARCH_PAGES = 2  # Fetch first N pages (10 results each)


def _scrape_euraxess(source_config: dict) -> list[dict]:
    """Scrape job listings from EURAXESS search results.

    EURAXESS is a Drupal 10 site using the EU ECL design system.
    Search results are fully server-rendered HTML. Each card has
    title, institution, country, deadline, and a link to a detail
    page with the full description.

    IMPORTANT: Requires a browser-like User-Agent header — the site
    returns HTTP 403 to plain bot user agents.

    Steps:
    1. Fetch search result pages (filtered by economics + experienced).
    2. Parse job cards from the HTML.
    3. For each job, fetch the detail page for the full description.
    """
    name = source_config["name"]
    base_url = source_config.get("url", f"{EURAXESS_BASE}/jobs/search")
    params_str = source_config.get("params", "")
    logger.info(f"Scraping {name}")

    grants = []

    for page_num in range(EURAXESS_SEARCH_PAGES):
        search_url = f"{base_url}?{params_str}&page={page_num}"
        logger.info(f"  Fetching search page {page_num}: {search_url}")

        if page_num > 0:
            time.sleep(POLITE_DELAY)

        resp = _get_browser(search_url)
        if not resp:
            logger.error(f"  Failed to fetch search page {page_num}")
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # Job cards are <li> inside ul[aria-label="Search results items"]
        results_ul = soup.select_one(
            'ul.unformatted-list[aria-label="Search results items"]'
        )
        if not results_ul:
            logger.warning(f"  No results container on page {page_num}")
            break

        cards = results_ul.find_all("li", recursive=False)
        if not cards:
            logger.info(f"  No more results on page {page_num}")
            break

        logger.info(f"  Page {page_num}: {len(cards)} cards found")

        for card in cards:
            grant = _parse_euraxess_card(card, name)
            if grant:
                grants.append(grant)

    # Fetch detail pages for full descriptions
    logger.info(f"  Fetching {len(grants)} detail pages...")
    for i, grant in enumerate(grants):
        if i > 0:
            time.sleep(POLITE_DELAY)

        detail_url = grant["url"]
        resp = _get_browser(detail_url)
        if not resp:
            continue

        detail_soup = BeautifulSoup(resp.text, "lxml")
        full_desc = _parse_euraxess_detail(detail_soup)
        if full_desc:
            grant["description"] = full_desc
            grant["raw_content"] += f"\nFull description: {full_desc}"

    logger.info(f"  {name}: {len(grants)} listings scraped")
    return grants


def _parse_euraxess_card(card, source_name: str) -> dict | None:
    """Parse a single EURAXESS search result card into a grant dict."""
    # Title and URL
    title_el = card.select_one("h3.ecl-content-block__title a")
    if not title_el:
        return None

    title_span = title_el.select_one("span")
    title = title_span.get_text(strip=True) if title_span else title_el.get_text(strip=True)
    rel_url = title_el.get("href", "")
    full_url = urljoin(EURAXESS_BASE, rel_url)

    # Country
    country_el = card.select_one("span.ecl-label.ecl-label--highlight")
    country = country_el.get_text(strip=True) if country_el else ""

    # Offer type (JOB, FELLOWSHIP, etc.)
    type_el = card.select_one("span.ecl-label.ecl-label--low")
    offer_type = type_el.get_text(strip=True) if type_el else ""

    # Institution
    institution = ""
    meta_items = card.select("ul.ecl-content-block__primary-meta-container li")
    if meta_items:
        inst_link = meta_items[0].select_one("a")
        if inst_link:
            institution = inst_link.get_text(strip=True)

    # Posted date
    date_posted = None
    if len(meta_items) >= 2:
        date_text = meta_items[1].get_text(strip=True)
        date_match = re.search(r"Posted on:\s*(.+)", date_text)
        if date_match:
            date_posted = date_match.group(1).strip()

    # Application deadline (from <time> element with ISO datetime attr)
    deadline = None
    deadline_el = card.select_one("div.id-Application-Deadline time")
    if deadline_el:
        deadline = deadline_el.get("datetime", "")[:10]  # YYYY-MM-DD

    # Description snippet
    desc_el = card.select_one("div.ecl-content-block__description")
    snippet = desc_el.get_text(strip=True) if desc_el else ""

    # Research fields
    research_fields = []
    field_links = card.select("div.id-Research-Field a")
    for fl in field_links:
        research_fields.append(fl.get_text(strip=True))

    raw_parts = [
        f"Title: {title}",
        f"Type: {offer_type}",
        f"Institution: {institution}",
        f"Country: {country}",
        f"Posted: {date_posted or 'N/A'}",
        f"Deadline: {deadline or 'N/A'}",
        f"Research fields: {', '.join(research_fields) or 'N/A'}",
        f"URL: {full_url}",
        f"Snippet: {snippet}",
    ]

    return {
        "id": generate_grant_id(full_url),
        "title": title,
        "url": full_url,
        "date_posted": date_posted,
        "deadline": deadline,
        "description": snippet,
        "source_name": source_name,
        "source_type": "web_scrape",
        "institution": institution,
        "country": country,
        "raw_content": "\n".join(raw_parts),
    }


def _parse_euraxess_detail(soup) -> str:
    """Extract the full job description from a EURAXESS detail page.

    The description lives under h2#offer-description in a div.ecl
    container. We also grab the Job Information metadata from the
    <dl> under h2#job-information.
    """
    parts = []

    # Job Information metadata (structured <dl>)
    job_info_h2 = soup.find("h2", id="job-information")
    if job_info_h2:
        dl = job_info_h2.find_next("dl")
        if dl:
            terms = dl.find_all("dt")
            defs = dl.find_all("dd")
            for dt, dd in zip(terms, defs):
                key = dt.get_text(strip=True)
                val = dd.get_text(strip=True)
                parts.append(f"{key}: {val}")

    # Offer Description (free text)
    desc_h2 = soup.find("h2", id="offer-description")
    if desc_h2:
        desc_div = desc_h2.find_next("div", class_="ecl")
        if desc_div:
            parts.append(f"Description: {desc_div.get_text(strip=True)}")

    # Requirements
    req_h2 = soup.find("h2", id="requirements")
    if req_h2:
        req_container = req_h2.find_next_sibling("div")
        if req_container:
            parts.append(f"Requirements: {req_container.get_text(strip=True)}")

    # Additional Information (benefits, salary as free text)
    add_h2 = soup.find("h2", id="additional-information")
    if add_h2:
        add_container = add_h2.find_next_sibling("div")
        if add_container:
            parts.append(f"Additional info: {add_container.get_text(strip=True)}")

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# RSA scraper (via RSS feed — listing page is AJAX-only)
# ─────────────────────────────────────────────────────────────────────

RSA_FEED_URL = "https://www.regionalstudies.org/feed/?post_type=news"
RSA_FEED_PAGES = 2  # Fetch first N pages of the RSS feed


# RSA news detection — filters out non-job/grant content from the RSS feed
_RSA_NEWS_MARKERS = [
    "announcing", "highlights from", "in memoriam", "welcome to the new",
    "celebrating", "introducing the", "book launch", "book series",
    "blog award", "best blog", "honorary membership", "award ceremony",
    "conference highlights", "conference faqs", "conference tours",
    "submitted sessions", "open sessions", "closed sessions",
    "plenary speakers", "anniversary workshop", "workshop:",
    "journal of regional", "knowbot", "call for papers",
    "call for proposals", "call for rsrs", "call for expressions",
    "policy impact", "rsa session at", "latin america",
    "women's network", "phd student representative",
    "most read", "around the world", "rsa opportunities",
]

# If the title contains ANY job/grant indicator, keep it regardless of news markers
_RSA_JOB_INDICATORS = [
    "position", "postdoc", "fellowship", "grant scheme", "grant holder",
    "lecturer in", "lecturer /", "vacancy", "funding", "award scheme",
    "research associate", "research fellow", "call for applications",
    "assistant professor", "associate professor", "full professor",
    "tenure", "research grant",
]


def _is_rsa_news(title: str) -> bool:
    """Return True if an RSA RSS entry is a news article (not a job/grant listing)."""
    t = title.lower()
    # If it clearly looks like a job/grant listing, always keep it
    if any(ind in t for ind in _RSA_JOB_INDICATORS):
        return False
    # If it matches any news marker pattern, skip it
    if any(marker in t for marker in _RSA_NEWS_MARKERS):
        return True
    return False


def _scrape_rsa(source_config: dict) -> list[dict]:
    """Scrape news/announcements from the Regional Studies Association.

    The RSA news listing page loads articles via AJAX (requires a
    session nonce), so it cannot be scraped with requests+BeautifulSoup.
    However, a fully functional WordPress RSS feed exists at:
        /feed/?post_type=news&paged=N

    Each RSS item includes the full article HTML in <content:encoded>,
    so we don't need to fetch individual pages. We use feedparser
    (already a project dependency) to parse it.
    """
    name = source_config["name"]
    logger.info(f"Scraping {name} via RSS feed")

    grants = []
    for page_num in range(1, RSA_FEED_PAGES + 1):
        feed_url = f"{RSA_FEED_URL}&paged={page_num}"
        logger.info(f"  Fetching RSS page {page_num}: {feed_url}")

        if page_num > 1:
            time.sleep(POLITE_DELAY)

        try:
            resp = requests.get(
                feed_url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            logger.error(f"  RSS fetch failed for page {page_num}: {exc}")
            break

        if resp.status_code == 404:
            logger.info(f"  No more RSS pages (404 on page {page_num})")
            break

        if resp.status_code != 200:
            logger.error(f"  RSS returned HTTP {resp.status_code}")
            break

        feed = feedparser.parse(resp.text)
        if not feed.entries:
            logger.info(f"  No entries on page {page_num}")
            break

        logger.info(f"  Page {page_num}: {len(feed.entries)} entries")

        for entry in feed.entries:
            entry_url = getattr(entry, "link", "")
            if not entry_url:
                continue

            title = getattr(entry, "title", "(no title)")

            # Filter out news articles, announcements, and non-job content.
            # RSA RSS feed mixes job/grant listings with news posts.
            if _is_rsa_news(title):
                logger.debug(f"  Skipping RSA news: {title[:60]}")
                continue

            # Extract date from published_parsed
            date_posted = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    date_posted = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            # Full content from content:encoded, fall back to summary
            description = ""
            if hasattr(entry, "content") and entry.content:
                description = entry.content[0].get("value", "")
            elif hasattr(entry, "summary"):
                description = entry.summary or ""

            # Strip HTML for raw_content (Claude gets the plain text)
            plain_text = BeautifulSoup(description, "lxml").get_text(
                " ", strip=True
            ) if description else ""

            raw_parts = [
                f"Title: {title}",
                f"Date: {date_posted or 'N/A'}",
                f"URL: {entry_url}",
                f"Content: {plain_text}",
            ]

            grant = {
                "id": generate_grant_id(entry_url),
                "title": title,
                "url": entry_url,
                "date_posted": date_posted,
                "deadline": None,  # News posts don't have deadlines
                "description": plain_text[:500],
                "source_name": name,
                "source_type": "web_scrape",
                "raw_content": "\n".join(raw_parts),
            }
            grants.append(grant)

    logger.info(f"  {name}: {len(grants)} articles scraped")
    return grants


# ─────────────────────────────────────────────────────────────────────
# ERSA scraper (vacancies + calls pages)
# ─────────────────────────────────────────────────────────────────────

ERSA_PAGES = [
    ("https://ersa.org/vacancies/", "vacancies"),
    ("https://ersa.org/calls/calls-for-publications/", "calls_publications"),
    ("https://ersa.org/calls/calls-for-event-submissions/", "calls_events"),
]


def _scrape_ersa(source_config: dict) -> list[dict]:
    """Scrape job postings and calls from the ERSA website.

    ERSA is a standard WordPress site with fully server-rendered HTML.
    Content is free-form — no semantic markup around individual items.
    We scrape three pages:
      - /vacancies/ — job postings (heading-delimited)
      - /calls/calls-for-publications/ — CFPs (h3-delimited)
      - /calls/calls-for-event-submissions/ — event CFPs (h3/h4/h5)

    All pages are single-page (no pagination).
    """
    name = source_config["name"]
    logger.info(f"Scraping {name}")

    grants = []
    for page_url, page_type in ERSA_PAGES:
        time.sleep(POLITE_DELAY)
        logger.info(f"  Fetching {page_type}: {page_url}")

        resp = _get(page_url)
        if not resp:
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        if page_type == "vacancies":
            page_grants = _parse_ersa_vacancies(soup, page_url, name)
        else:
            page_grants = _parse_ersa_calls(soup, page_url, name, page_type)

        grants.extend(page_grants)
        logger.info(f"  {page_type}: {len(page_grants)} items")

    logger.info(f"  {name}: {len(grants)} total items scraped")
    return grants


def _parse_ersa_vacancies(soup, page_url: str, source_name: str) -> list[dict]:
    """Parse ERSA /vacancies/ page — free-form paragraphs with <strong> titles.

    Strategy: find the content area, then iterate through child elements.
    A <strong> tag inside a <p> likely starts a new vacancy. Collect all
    following elements until the next <strong> as the vacancy body.
    """
    content = (
        soup.select_one(".entry-content")
        or soup.select_one("#content")
        or soup.select_one("article")
        or soup.select_one("main")
    )
    if not content:
        logger.warning("  Could not find content container on vacancies page")
        return []

    grants = []
    current_title = None
    current_body_parts = []
    current_links = []

    for el in content.children:
        if not hasattr(el, "name") or not el.name:
            continue

        # Detect new vacancy: <p> with <strong>, or <h2>/<h3>/<h4>
        is_heading = el.name in ("h2", "h3", "h4", "h5")
        has_strong = el.name == "p" and el.find("strong")

        if is_heading or has_strong:
            # Save previous vacancy
            if current_title:
                grants.append(_build_ersa_grant(
                    current_title, current_body_parts, current_links,
                    page_url, source_name,
                ))

            if is_heading:
                current_title = el.get_text(strip=True)
            else:
                strong_el = el.find("strong")
                current_title = strong_el.get_text(strip=True)
            current_body_parts = [el.get_text(strip=True)]
            current_links = [a["href"] for a in el.find_all("a", href=True)]
        elif current_title:
            current_body_parts.append(el.get_text(strip=True))
            if hasattr(el, "find_all"):
                current_links.extend(a["href"] for a in el.find_all("a", href=True))

    # Don't forget the last item
    if current_title:
        grants.append(_build_ersa_grant(
            current_title, current_body_parts, current_links,
            page_url, source_name,
        ))

    return grants


def _parse_ersa_calls(
    soup, page_url: str, source_name: str, page_type: str
) -> list[dict]:
    """Parse ERSA calls pages — h3-delimited items with linked titles."""
    content = (
        soup.select_one(".entry-content")
        or soup.select_one("#content")
        or soup.select_one("article")
        or soup.select_one("main")
    )
    if not content:
        logger.warning(f"  Could not find content container on {page_type} page")
        return []

    grants = []
    # Find all h3/h4 elements as call delimiters
    headings = content.find_all(["h3", "h4"])
    if not headings:
        # Fall back to vacancies-style parsing
        return _parse_ersa_vacancies(soup, page_url, source_name)

    for i, heading in enumerate(headings):
        title = heading.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # Get the URL from the heading link if present
        heading_link = heading.find("a", href=True)
        item_url = heading_link["href"] if heading_link else page_url

        # Collect body: all siblings between this heading and the next
        body_parts = []
        links = [heading_link["href"]] if heading_link else []
        sibling = heading.find_next_sibling()
        next_heading = headings[i + 1] if i + 1 < len(headings) else None

        while sibling and sibling != next_heading:
            if hasattr(sibling, "get_text"):
                text = sibling.get_text(strip=True)
                if text:
                    body_parts.append(text)
                links.extend(
                    a["href"] for a in sibling.find_all("a", href=True)
                )
            sibling = sibling.find_next_sibling()

        body_text = " ".join(body_parts)

        # Try to extract deadline from body text
        deadline = None
        dl_match = re.search(
            r"[Dd]eadline[:\s]*(\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
            body_text,
        )
        if dl_match:
            deadline = dl_match.group(1)

        # Try to extract date posted
        date_posted = None
        posted_match = re.search(
            r"[Pp]osted\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", body_text
        )
        if posted_match:
            date_posted = posted_match.group(1)

        raw_parts = [
            f"Title: {title}",
            f"Type: {page_type}",
            f"Date: {date_posted or 'N/A'}",
            f"Deadline: {deadline or 'N/A'}",
            f"URL: {item_url}",
            f"Content: {body_text[:2000]}",
        ]

        grant = {
            "id": generate_grant_id(item_url, title),
            "title": title,
            "url": item_url,
            "date_posted": date_posted,
            "deadline": deadline,
            "description": body_text[:500],
            "source_name": source_name,
            "source_type": "web_scrape",
            "raw_content": "\n".join(raw_parts),
        }
        grants.append(grant)

    return grants


def _build_ersa_grant(
    title: str,
    body_parts: list[str],
    links: list[str],
    page_url: str,
    source_name: str,
) -> dict:
    """Build a grant dict from parsed ERSA vacancy components."""
    body_text = " ".join(body_parts)

    # Use first external link as the grant URL, fall back to page URL
    item_url = page_url
    for link in links:
        if link.startswith("http") and "ersa.org" not in link:
            item_url = link
            break

    # Try to extract deadline
    deadline = None
    dl_match = re.search(
        r"[Dd]eadline[:\s]*(\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
        body_text,
    )
    if dl_match:
        deadline = dl_match.group(1)

    raw_parts = [
        f"Title: {title}",
        f"Deadline: {deadline or 'N/A'}",
        f"URL: {item_url}",
        f"Content: {body_text[:2000]}",
    ]

    return {
        "id": generate_grant_id(item_url, title),
        "title": title,
        "url": item_url,
        "date_posted": None,
        "deadline": deadline,
        "description": body_text[:500],
        "source_name": source_name,
        "source_type": "web_scrape",
        "raw_content": "\n".join(raw_parts),
    }


# ─────────────────────────────────────────────────────────────────────
# BRIN Talent Management Portal (manajementalenta.brin.go.id)
# ─────────────────────────────────────────────────────────────────────

BRIN_BASE = "https://manajementalenta.brin.go.id"


def _scrape_brin(source_config: dict) -> list[dict]:
    """Scrape postdoc and fellowship programmes from BRIN Talent Management.

    The portal lists programme cards on the homepage, each linking to
    /program/{id} with details (deadline, eligibility, description).
    Programmes are batch-based (e.g. "Postdoctoral 2026 Batch 1") and
    change infrequently — typically a few new batches per year.
    """
    name = source_config["name"]
    logger.info(f"Scraping {name}")

    # 1. Fetch homepage to discover programme links
    resp = _get_browser(BRIN_BASE + "/")
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    # Find links matching /program/{id}
    prog_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.match(r"^/program/\d+$", href):
            prog_links.add(href)
        elif re.match(r"^https?://manajementalenta\.brin\.go\.id/program/\d+$", href):
            prog_links.add(href.replace(BRIN_BASE, ""))

    logger.info(f"  Found {len(prog_links)} programme links")

    grants = []
    for path in sorted(prog_links):
        time.sleep(POLITE_DELAY)
        prog_url = BRIN_BASE + path
        logger.info(f"  Fetching {prog_url}")

        detail = _get_browser(prog_url)
        if not detail:
            continue

        grant = _parse_brin_programme(detail.text, prog_url, name)
        if grant:
            grants.append(grant)

    logger.info(f"  {name}: {len(grants)} programmes scraped")
    return grants


def _parse_brin_programme(html: str, url: str, source_name: str) -> dict | None:
    """Parse a single BRIN programme detail page."""
    soup = BeautifulSoup(html, "lxml")

    # Title: typically the main heading or page title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
    # Clean up title
    title = re.sub(r"\s*[-|].*Manajemen Talenta.*$", "", title).strip()

    if not title:
        return None

    # Extract deadline from text (look for "pendaftaran ... - DD Mon YYYY" pattern)
    text = soup.get_text(" ", strip=True)
    deadline = None
    # Pattern: "31 Mar 2026", "31 Mei 2025", etc.
    dl_match = re.search(
        r"(?:pendaftaran|deadline|batas\s+waktu)[^0-9]*"
        r"\d{1,2}\s+\w+\s+\d{4}\s*[-–]\s*(\d{1,2}\s+\w+\s+\d{4})",
        text, re.IGNORECASE,
    )
    if dl_match:
        deadline = _parse_indo_date(dl_match.group(1))
    else:
        # Try simpler pattern: just find a closing date
        dl_match2 = re.search(
            r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des|"
            r"January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\w*\s+\d{4})",
            text, re.IGNORECASE,
        )
        if dl_match2:
            deadline = _parse_indo_date(dl_match2.group(1))

    # Description: collect text from the main content area
    main = soup.find("main") or soup.find("div", class_=re.compile(r"content|detail"))
    desc_text = main.get_text(" ", strip=True) if main else text
    # Truncate for display
    description = desc_text[:500] + "..." if len(desc_text) > 500 else desc_text

    return {
        "id": generate_grant_id(url),
        "title": title,
        "url": url,
        "date_posted": None,
        "deadline": deadline,
        "description": description,
        "source_name": source_name,
        "source_type": "web_scrape",
        "institution": "BRIN (Badan Riset dan Inovasi Nasional)",
        "country": "Indonesia",
        "raw_content": desc_text[:12000],
    }


def _parse_indo_date(date_str: str) -> str | None:
    """Parse Indonesian/English date string to ISO format."""
    # Indonesian month mapping
    indo_months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "mei": "05", "may": "05", "jun": "06", "jul": "07",
        "agu": "08", "aug": "08", "sep": "09", "okt": "10", "oct": "10",
        "nov": "11", "des": "12", "dec": "12",
    }
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", date_str.strip())
    if not m:
        return None
    day, month_str, year = m.groups()
    month_key = month_str[:3].lower()
    month = indo_months.get(month_key)
    if not month:
        return None
    return f"{year}-{month}-{int(day):02d}"


# ─────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────

_SCRAPER_REGISTRY: dict[str, callable] = {
    "jobs_ac_uk": _scrape_jobs_ac_uk,
    "academic_transfer": _scrape_academic_transfer,
    "euraxess": _scrape_euraxess,
    "rsa": _scrape_rsa,
    "ersa": _scrape_ersa,
    "brin": _scrape_brin,
}


def get_all_scraped_grants(config: dict) -> list[dict]:
    """Scrape grants from all configured and enabled web sources.

    Iterates over config.sources.web_scrapers, dispatches each to
    the appropriate scraper function, and returns combined results.
    Updates data/run_status.json with per-source success/failure.

    Args:
        config: The full config dict from config.json.

    Returns:
        Combined list of grant dicts from all enabled web scrapers.
    """
    source_configs = config.get("sources", {}).get("web_scrapers", [])
    all_grants = []

    for source_cfg in source_configs:
        if not source_cfg.get("enabled", True):
            continue

        name = source_cfg.get("name", "unknown")
        scraper_id = source_cfg.get("scraper")

        if not scraper_id:
            logger.warning(f"Skipping '{name}': no 'scraper' key in config")
            _update_run_status(name, False, 0, "no scraper key in config")
            continue

        scraper_fn = _SCRAPER_REGISTRY.get(scraper_id)
        if not scraper_fn:
            logger.warning(
                f"Skipping '{name}': scraper '{scraper_id}' not implemented"
            )
            _update_run_status(name, False, 0, f"scraper '{scraper_id}' not implemented")
            continue

        try:
            grants = scraper_fn(source_cfg)
            all_grants.extend(grants)
            _update_run_status(name, True, len(grants))
        except Exception as exc:
            logger.error(f"Scraper '{name}' crashed: {exc}", exc_info=True)
            _update_run_status(name, False, 0, str(exc))

        time.sleep(POLITE_DELAY)

    logger.info(f"Web scraper complete: {len(all_grants)} total listings")
    return all_grants


# Keep the old name as an alias for backwards compatibility
scrape_web_sources = get_all_scraped_grants


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent.parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    grants = get_all_scraped_grants(config)

    source_counts: dict[str, int] = {}
    for g in grants:
        source_counts[g["source_name"]] = source_counts.get(g["source_name"], 0) + 1

    print(f"\n{'='*60}")
    print("Web Scraper Results")
    print(f"{'='*60}")
    for source, count in source_counts.items():
        print(f"  {source}: {count} listings")
    if not source_counts:
        print("  (no results from any scraper)")
    print(f"{'='*60}")
    print(f"  TOTAL: {len(grants)} listings")

    if grants:
        print(f"\nSample entry (first result):")
        sample = grants[0].copy()
        for key in ("description", "raw_content"):
            if len(sample.get(key, "")) > 200:
                sample[key] = sample[key][:200] + "..."
        print(json.dumps(sample, indent=2, default=str))
