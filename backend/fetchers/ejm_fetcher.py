"""
ejm_fetcher.py — Fetch job ads from the EconJobMarket public JSON API.

EconJobMarket's XML endpoint is deprecated; the only supported format
is JSON at:
    https://backend.econjobmarket.org/data/zz_public/json/Ads

Each ad contains: adtitle, adtext (HTML), startdate, enddate,
department, name (institution), url, position_types, categories,
latitude/longitude, and logo info.

Returns standardised grant dicts matching the schema in __init__.py.
"""

import json
import logging
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.utils import generate_grant_id, update_run_status

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90
USER_AGENT = "GrantRadar/1.0 (academic grant monitoring)"
DEFAULT_URL = "https://backend.econjobmarket.org/data/zz_public/json/Ads"


def fetch_ejm_ads(config: dict) -> list[dict]:
    """Fetch all current ads from EconJobMarket.

    Args:
        config: The full config dict (uses sources.api_endpoints
                to find the EJM entry).

    Returns:
        List of standardised grant dicts.
    """
    # Find URL from config, fall back to default
    url = DEFAULT_URL
    for ep in config.get("sources", {}).get("api_endpoints", []):
        if ep.get("name") == "EconJobMarket" and ep.get("enabled", True):
            url = ep.get("url", DEFAULT_URL)
            break
    else:
        # No enabled EJM entry in config
        logger.info("EconJobMarket not enabled in config, skipping")
        update_run_status("EconJobMarket", status="error", error_msg="Not enabled in config")
        return []

    logger.info(f"Fetching EconJobMarket ads from {url}")

    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error(f"EconJobMarket fetch failed: {exc}")
        update_run_status("EconJobMarket", status="error", error_msg=str(exc))
        return []

    try:
        ads = resp.json()
    except ValueError as exc:
        logger.error(f"EconJobMarket JSON parse failed: {exc}")
        update_run_status("EconJobMarket", status="error", error_msg=f"JSON parse: {exc}")
        return []

    if not isinstance(ads, list):
        logger.error(
            f"EconJobMarket: expected JSON array, got {type(ads).__name__}"
        )
        update_run_status("EconJobMarket", status="error",
                          error=f"Expected array, got {type(ads).__name__}")
        return []

    grants = []
    for ad in ads:
        ad_url = ad.get("url", "")
        if not ad_url:
            continue

        position_types = [p.get("name", "") for p in ad.get("position_types", [])]
        categories = [c.get("name", "") for c in ad.get("categories", [])]

        raw_parts = [
            f"Title: {ad.get('adtitle', '')}",
            f"Institution: {ad.get('name', '')}",
            f"Department: {ad.get('department', '')}",
            f"Position types: {', '.join(position_types)}",
            f"Categories: {', '.join(categories)}",
            f"Deadline: {ad.get('enddate', '')}",
            f"Description: {ad.get('adtext', '')}",
        ]

        grant = {
            "id": generate_grant_id(ad_url),
            "title": ad.get("adtitle", "(no title)"),
            "url": ad_url,
            "date_posted": ad.get("startdate"),
            "deadline": ad.get("enddate"),
            "description": ad.get("adtext", ""),
            "source_name": "EconJobMarket",
            "source_type": "api",
            "institution": ad.get("name", ""),
            "department": ad.get("department", ""),
            "position_types": position_types,
            "categories": categories,
            "raw_content": "\n".join(raw_parts),
        }
        grants.append(grant)

    logger.info(f"EconJobMarket: {len(grants)} ads fetched")
    update_run_status("EconJobMarket", status="success", grants_found=len(grants))
    return grants


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent.parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    grants = fetch_ejm_ads(config)

    print(f"\n{'='*50}")
    print(f"EconJobMarket: {len(grants)} ads")
    print(f"{'='*50}")

    for g in grants[:5]:
        types = ", ".join(g.get("position_types", []))
        print(f"  [{types}] {g['title']}")
        print(f"    {g['institution']} — deadline: {g.get('deadline', 'N/A')}")
        print(f"    {g['url']}")
        print()
