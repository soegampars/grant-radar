"""
rss_fetcher.py — Fetch and parse grant listings from RSS/Atom feeds.

Uses `requests` to download feed content (so we can inspect status
codes and content-type) and `feedparser` to parse the XML. Each entry
is converted to a standardised grant dict for downstream analysis.

Handles:
  - Multiple feed formats (RSS 2.0, Atom, RSS 1.0)
  - HTML responses / 404s (detected before parsing, logged clearly)
  - Malformed XML with control characters (sanitised before parsing)
  - Graceful failure on unreachable feeds (logs error, continues)
  - Date normalisation to ISO 8601 via feedparser's parsed dates
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.utils import generate_grant_id, update_run_status

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
USER_AGENT = "GrantRadar/1.0 (academic grant monitoring)"
POLITE_DELAY = 1  # seconds between feed fetches


def _sanitise_xml(text: str) -> str:
    """Remove characters that are illegal in XML 1.0.

    Strips control chars (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F) that cause
    feedparser/expat to choke on otherwise valid feeds.
    """
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _extract_date(entry: feedparser.FeedParserDict) -> str | None:
    """Extract the best available date from a feed entry.

    feedparser normalises dates into *_parsed time.struct_time tuples.
    We check published_parsed, then updated_parsed, then created_parsed.
    """
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                continue
    return None


def _extract_description(entry: feedparser.FeedParserDict) -> str:
    """Extract the fullest text available from a feed entry.

    Prefers content[0].value (often the full HTML body), then falls
    back to summary, then description, then empty string.
    """
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    return getattr(entry, "summary", "") or getattr(entry, "description", "") or ""


def _build_raw_content(entry: feedparser.FeedParserDict) -> str:
    """Combine all textual fields into a single string for Claude analysis."""
    parts = []
    title = getattr(entry, "title", "")
    if title:
        parts.append(f"Title: {title}")
    description = _extract_description(entry)
    if description:
        parts.append(f"Description: {description}")
    for tag in getattr(entry, "tags", []):
        term = tag.get("term", "")
        if term:
            parts.append(f"Tag: {term}")
    return "\n".join(parts)


def _is_html_response(text: str) -> bool:
    """Detect if the response is an HTML page rather than an RSS/Atom feed."""
    start = text.strip()[:500].lower()
    return start.startswith("<!doctype html") or start.startswith("<html")


def fetch_single_feed(feed_config: dict) -> list[dict]:
    """Fetch and parse a single RSS/Atom feed.

    Args:
        feed_config: Dict with keys: name, url, enabled.

    Returns:
        List of standardised grant dicts from this feed.
    """
    name = feed_config["name"]
    url = feed_config["url"]
    logger.info(f"Fetching feed: {name}")

    # Step 1: Download with requests for status/content-type inspection
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
    except requests.RequestException as exc:
        logger.error(f"  Network error for '{name}': {exc}")
        update_run_status(name, status="error", error_msg=str(exc))
        return []

    if resp.status_code != 200:
        logger.error(f"  '{name}' returned HTTP {resp.status_code}")
        update_run_status(name, status="error", error_msg=f"HTTP {resp.status_code}")
        return []

    if _is_html_response(resp.text):
        logger.error(
            f"  '{name}' returned HTML instead of RSS/XML. "
            f"The feed URL may have changed — check {url}"
        )
        update_run_status(name, status="error", error_msg="Response was HTML, not RSS/XML")
        return []

    # Step 2: Sanitise and parse
    clean_text = _sanitise_xml(resp.text)
    feed = feedparser.parse(clean_text)

    if feed.bozo and not feed.entries:
        logger.error(f"  '{name}' XML parse failed: {feed.bozo_exception}")
        update_run_status(name, status="error", error_msg=f"XML parse: {feed.bozo_exception}")
        return []
    elif feed.bozo:
        logger.warning(f"  '{name}' parsed with warnings: {feed.bozo_exception}")

    # Step 3: Convert entries to grant dicts
    grants = []
    for entry in feed.entries:
        entry_url = getattr(entry, "link", "") or getattr(entry, "id", "")
        if not entry_url:
            continue

        grant = {
            "id": generate_grant_id(entry_url),
            "title": getattr(entry, "title", "(no title)"),
            "url": entry_url,
            "date_posted": _extract_date(entry),
            "description": _extract_description(entry),
            "source_name": name,
            "source_type": "rss",
            "raw_content": _build_raw_content(entry),
        }
        grants.append(grant)

    logger.info(f"  '{name}': {len(grants)} entries parsed")
    update_run_status(name, status="success", grants_found=len(grants))
    return grants


def get_all_rss_grants(config: dict) -> list[dict]:
    """Fetch grants from all enabled RSS feeds in config.

    Args:
        config: The full config dict (loaded from config.json).

    Returns:
        Combined list of grant dicts from all enabled feeds.
    """
    feed_configs = config.get("sources", {}).get("rss_feeds", [])
    enabled = [f for f in feed_configs if f.get("enabled", True)]
    logger.info(f"RSS fetcher: {len(enabled)} enabled feeds")

    all_grants = []
    for i, feed_cfg in enumerate(enabled):
        grants = fetch_single_feed(feed_cfg)
        all_grants.extend(grants)
        if i < len(enabled) - 1:
            time.sleep(POLITE_DELAY)

    logger.info(f"RSS fetcher complete: {len(all_grants)} total entries")
    return all_grants


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent.parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    grants = get_all_rss_grants(config)

    # Per-source summary
    source_counts: dict[str, int] = {}
    for g in grants:
        source_counts[g["source_name"]] = source_counts.get(g["source_name"], 0) + 1

    print(f"\n{'='*50}")
    print("RSS Fetch Results")
    print(f"{'='*50}")
    for source, count in source_counts.items():
        print(f"  {source}: {count} entries")
    if not source_counts:
        print("  (no entries from any feed)")
    print(f"{'='*50}")
    print(f"  TOTAL: {len(grants)} entries")

    if grants:
        print(f"\nSample entry (first result):")
        sample = grants[0].copy()
        for key in ("description", "raw_content"):
            if len(sample.get(key, "")) > 200:
                sample[key] = sample[key][:200] + "..."
        print(json.dumps(sample, indent=2))
