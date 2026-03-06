"""
utils.py — Shared utility functions for Grant Radar.

Provides the following public helpers:

  1. deduplicate(new, existing)   — URL + title-similarity dedup.
  2. load_grants(data_dir)        — Read data/grants.json safely.
  3. save_grants(grants, data_dir) — Write grants sorted by tier & date.
  4. update_run_status(...)        — Per-source result in run_status.json.
  5. parse_date(date_string)       — Multi-format → ISO 8601.
  6. generate_grant_id(url, title) — Stable MD5-based hash ID.

Lower-level helpers (load_json / save_json) are also exported for use
by individual fetchers that manage their own data files.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"


# ───────────────────────────────────────────────────────────────────
# Low-level JSON helpers (used by fetchers and internally)
# ───────────────────────────────────────────────────────────────────

def load_json(filename: str, data_dir: Path | str | None = None) -> list | dict:
    """Load a JSON file from the data/ directory.

    Args:
        filename: Name of the file inside data/ (e.g. "grants.json").
        data_dir: Override for the data directory path.

    Returns:
        Parsed JSON content (list or dict).  Returns ``[]`` if the
        file does not exist or cannot be decoded.
    """
    dirpath = Path(data_dir) if data_dir else DATA_DIR
    filepath = dirpath / filename
    if not filepath.exists():
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", filepath, exc)
        return []


def save_json(
    filename: str,
    data: list | dict,
    data_dir: Path | str | None = None,
) -> None:
    """Save data to a JSON file in the data/ directory.

    Args:
        filename: Name of the file inside data/.
        data: The data to serialise as JSON.
        data_dir: Override for the data directory path.
    """
    dirpath = Path(data_dir) if data_dir else DATA_DIR
    dirpath.mkdir(parents=True, exist_ok=True)
    filepath = dirpath / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Saved %s", filepath)


# ───────────────────────────────────────────────────────────────────
# 6. generate_grant_id
# ───────────────────────────────────────────────────────────────────

def generate_grant_id(url: str, title: str | None = None) -> str:
    """Create a stable hash ID from URL (primary) or title (fallback).

    Uses MD5 for simplicity — this is a fingerprint, not a security
    hash.  The first 12 hex characters are returned.

    Args:
        url:   The grant's source URL (primary key material).
        title: Fallback text used when *url* is empty/None.

    Returns:
        A 12-character hex digest string.
    """
    key = url if url else (title or "")
    return hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]


# ───────────────────────────────────────────────────────────────────
# 5. parse_date
# ───────────────────────────────────────────────────────────────────

# strptime patterns tried in order — most specific first
_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",       # ISO 8601 with timezone
    "%Y-%m-%dT%H:%M:%S",         # ISO 8601 without tz
    "%Y-%m-%d",                   # 2026-03-15
    "%d %B %Y",                   # 15 March 2026
    "%d %b %Y",                   # 15 Mar 2026
    "%B %d, %Y",                  # March 15, 2026
    "%b %d, %Y",                  # Mar 15, 2026
    "%d/%m/%Y",                   # 15/03/2026
    "%m/%d/%Y",                   # 03/15/2026
    "%d-%b-%Y",                   # 15-Mar-2026
    "%d %b",                      # 15 Mar (no year — jobs.ac.uk)
]


def parse_date(date_string: str | None) -> str | None:
    """Parse a date string into ISO 8601 (YYYY-MM-DD).

    Handles:
      - RFC 822 / RSS date formats  ("Mon, 01 Mar 2026 00:00:00 GMT")
      - ISO 8601 variants           ("2026-03-15", "2026-03-15T12:00:00")
      - Human-readable formats      ("15 March 2026", "March 15, 2026")
      - Abbreviated month formats   ("15 Mar 2026", "Mar 15, 2026")
      - Partial dates               ("15 Mar" — assumes current year)

    Args:
        date_string: A date string in an unknown format.

    Returns:
        ISO date string (YYYY-MM-DD) or None if unparseable.
    """
    if not date_string or not isinstance(date_string, str):
        return None

    text = date_string.strip()
    if not text:
        return None

    # 1. Try RFC 822 (RSS feeds: "Mon, 01 Mar 2026 00:00:00 GMT")
    try:
        dt = parsedate_to_datetime(text)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    # 2. Try each strptime pattern
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            # For patterns without a year, default to current year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    logger.warning("Could not parse date: %r", text)
    return None


# ───────────────────────────────────────────────────────────────────
# 1. deduplicate
# ───────────────────────────────────────────────────────────────────

def _normalise_title(title: str) -> str:
    """Lowercase, strip whitespace, remove non-alphanumeric chars."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower().strip())


def deduplicate(
    new_grants: list[dict],
    existing_grants: list[dict],
) -> list[dict]:
    """Return grants from *new_grants* not already in *existing_grants*.

    Matching criteria (either triggers duplicate removal):
      - **URL exact match**.
      - **Normalised title similarity > 90 %** (difflib SequenceMatcher).

    Args:
        new_grants:      Freshly fetched grant dicts.
        existing_grants: Previously stored grant dicts (from grants.json).

    Returns:
        A list containing only the non-duplicate grants.
    """
    existing_urls: set[str] = {g.get("url", "") for g in existing_grants}
    existing_titles: list[str] = [
        _normalise_title(g.get("title", "")) for g in existing_grants
    ]
    # Pre-filter empty titles out of the comparison list
    existing_titles = [t for t in existing_titles if t]

    unique: list[dict] = []
    skipped = 0

    for grant in new_grants:
        url = grant.get("url", "")

        # --- URL exact match ------------------------------------------
        if url and url in existing_urls:
            logger.debug("Duplicate (URL match): %s", url)
            skipped += 1
            continue

        # --- Title similarity -----------------------------------------
        norm_title = _normalise_title(grant.get("title", ""))
        is_dup = False
        if norm_title:
            for et in existing_titles:
                ratio = SequenceMatcher(None, norm_title, et).ratio()
                if ratio > 0.90:
                    logger.debug(
                        "Duplicate (title ≈%.0f%%): %s",
                        ratio * 100,
                        norm_title[:60],
                    )
                    is_dup = True
                    break

        if is_dup:
            skipped += 1
            continue

        unique.append(grant)

    logger.info(
        "Deduplication: %d incoming, %d new, %d duplicates.",
        len(new_grants),
        len(unique),
        skipped,
    )
    return unique


# Keep old name as alias for backward compatibility
deduplicate_grants = deduplicate



# ───────────────────────────────────────────────────────────────────
# 2. load_grants
# ───────────────────────────────────────────────────────────────────

def load_grants(data_dir: Path | str | None = None) -> list[dict]:
    """Load ``data/grants.json``.

    Returns an empty list if the file does not exist or contains
    corrupted / non-list JSON.

    Args:
        data_dir: Override for the data directory (default: ``data/``).
    """
    dirpath = Path(data_dir) if data_dir else DATA_DIR
    filepath = dirpath / "grants.json"

    if not filepath.exists():
        logger.info("%s not found — starting with empty grant list.", filepath)
        return []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        logger.error("Corrupted %s: %s — returning empty list.", filepath, exc)
        return []
    except OSError as exc:
        logger.error("Cannot read %s: %s — returning empty list.", filepath, exc)
        return []

    if not isinstance(data, list):
        logger.warning("%s is not a list — returning empty list.", filepath)
        return []

    logger.info("Loaded %d grants from %s", len(data), filepath)
    return data


# ───────────────────────────────────────────────────────────────────
# 3. save_grants
# ───────────────────────────────────────────────────────────────────

def save_grants(
    grants: list[dict],
    data_dir: Path | str | None = None,
) -> None:
    """Save grants to ``data/grants.json`` with pretty formatting.

    The list is sorted by **tier** (ascending — Tier 1 first) then by
    **date_found** (descending — newest first within each tier).

    Args:
        grants:   List of grant dicts to persist.
        data_dir: Override for the data directory (default: ``data/``).
    """
    dirpath = Path(data_dir) if data_dir else DATA_DIR
    dirpath.mkdir(parents=True, exist_ok=True)
    filepath = dirpath / "grants.json"

    # Two-pass stable sort: date_found DESC first, then tier ASC
    # Python's sort is stable, so the secondary key is preserved.
    sorted_grants = sorted(
        grants,
        key=lambda g: g.get("date_found") or "0000-00-00",
        reverse=True,
    )
    sorted_grants = sorted(
        sorted_grants,
        key=lambda g: g.get("tier", 999),
    )

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(sorted_grants, f, indent=2, default=str)

    logger.info("Saved %d grants to %s", len(sorted_grants), filepath)


# ───────────────────────────────────────────────────────────────────
# 4. update_run_status
# ───────────────────────────────────────────────────────────────────

def update_run_status(
    source_name: str,
    status: str,
    error_msg: str | None = None,
    data_dir: Path | str | None = None,
    *,
    grants_found: int = 0,
) -> None:
    """Record a per-source fetch result in ``data/run_status.json``.

    Each entry in the JSON array has the shape::

        {
            "source_name":  str,
            "status":       "success" | "error",
            "last_checked": "2026-03-15T12:00:00+00:00",
            "error":        str | null,
            "grants_found": int
        }

    If an entry for *source_name* already exists it is **replaced**;
    otherwise a new entry is appended.

    Args:
        source_name:  Human-readable source label (e.g. "EURAXESS").
        status:       ``"success"`` or ``"error"``.
        error_msg:    Error description (``None`` on success).
        data_dir:     Override for the data directory.
        grants_found: Number of grants returned by this source.
    """
    dirpath = Path(data_dir) if data_dir else DATA_DIR

    try:
        status_list = load_json("run_status.json", data_dir=dirpath)
        if not isinstance(status_list, list):
            status_list = []

        entry = {
            "source_name": source_name,
            "status": status,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "error": error_msg,
            "grants_found": grants_found,
        }

        # Replace existing entry for this source, or append
        status_list = [
            s for s in status_list
            if s.get("source_name") != source_name
            # Also match the old field name used before this refactor
            and s.get("source") != source_name
        ]
        status_list.append(entry)

        save_json("run_status.json", status_list, data_dir=dirpath)
    except Exception as exc:
        logger.warning("Failed to update run_status.json: %s", exc)
