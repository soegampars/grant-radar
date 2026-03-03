"""
fetchers — Grant data collection modules.

This package provides three fetching strategies:
  - rss_fetcher:  Parses RSS/Atom feeds (arXiv Economics).
  - ejm_fetcher:  Fetches ads from the EconJobMarket JSON API.
  - web_scraper:  Scrapes non-RSS web sources (jobs.ac.uk,
                  Academic Transfer, EURAXESS, etc.)

All return a list of grant dicts with a consistent core schema:
    {
        "id": str,            # SHA-256[:12] hash of URL
        "title": str,
        "url": str,
        "date_posted": str,   # ISO 8601 date or None
        "deadline": str,      # ISO 8601 date, free text, or None
        "description": str,
        "source_name": str,
        "source_type": str,   # "rss", "api", or "web_scrape"
        "raw_content": str,   # combined text for Claude analysis
    }
"""
