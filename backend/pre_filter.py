"""
pre_filter.py -- Lightweight pre-analysis filter to reduce Sonnet API costs.

Two layers:
  1. keyword_filter()  -- free, instant, config-driven title/description check
  2. haiku_triage()     -- ~$0.003/day, single Haiku batch call
  3. pre_filter()       -- orchestrator that runs both layers

Inserted between deduplication and Sonnet analysis in the pipeline.
"""

import json
import logging
from datetime import datetime, timezone

from anthropic import Anthropic, APIError

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Layer 1: Keyword-based filter (free, instant)
# -------------------------------------------------------------------

def keyword_filter(
    grants: list[dict], config: dict
) -> tuple[list[dict], list[dict]]:
    """Filter grants using config-driven keyword lists.

    Three rules (all must pass for a grant to survive):
      1. Title must NOT contain any exclude keyword.
      2. Title must contain at least one require keyword (title-only check).
      3. Title must NOT match any exclude field pattern (irrelevant academic fields).

    Returns:
        (keep, filtered) — two lists.
    """
    pf = config.get("pre_filter", {})
    exclude_kws = [kw.lower() for kw in pf.get("exclude_title_keywords", [])]
    require_kws = [kw.lower() for kw in pf.get("require_any_keywords", [])]
    exclude_fields = [kw.lower() for kw in pf.get("exclude_title_fields", [])]

    keep: list[dict] = []
    filtered: list[dict] = []

    for g in grants:
        title_lower = (g.get("title") or "").lower()

        # Rule 1: exclude keywords in title
        excluded_by = None
        for kw in exclude_kws:
            if kw in title_lower:
                excluded_by = kw
                break

        if excluded_by:
            g["filter_reason"] = f"keyword_exclude: {excluded_by}"
            filtered.append(g)
            continue

        # Rule 2: require at least one domain keyword in TITLE ONLY
        # (previously checked title + description, which was too permissive)
        if require_kws:
            found = any(kw in title_lower for kw in require_kws)
            if not found:
                g["filter_reason"] = "no_domain_keyword_in_title"
                filtered.append(g)
                continue

        # Rule 3: exclude irrelevant academic fields mentioned in title
        field_excluded_by = None
        for kw in exclude_fields:
            if kw in title_lower:
                field_excluded_by = kw
                break

        if field_excluded_by:
            g["filter_reason"] = f"irrelevant_field: {field_excluded_by}"
            filtered.append(g)
            continue

        keep.append(g)

    logger.info(
        "Keyword filter: %d in, %d excluded, %d remain.",
        len(grants), len(filtered), len(keep),
    )
    return keep, filtered


# -------------------------------------------------------------------
# Layer 2: Haiku batch triage (~$0.003/day)
# -------------------------------------------------------------------

_TRIAGE_SYSTEM = """You are a pre-filter for an academic grant monitoring system.

The researcher is a PhD candidate (final year, expecting defence Q1 2027) in Regional Economics / Economic Geography at Politecnico di Milano (Indonesian nationality, Groningen MSc). He is looking for POSTDOCTORAL positions, fellowships, research associate roles, and academic grants in economics, geography, regional science, spatial economics, urban studies, policy, planning, or development.

TARGET GEOGRAPHY: Europe (especially UK, Netherlands, Italy) and Indonesia ONLY.

Given a numbered list of job/grant listing titles with institutions, EXCLUDE the following:

1. PhD / doctoral / pre-doctoral positions (researcher needs POSTDOC level).
2. Positions clearly outside Europe and Indonesia (e.g., US, Canada, Australia, Asia except Indonesia, Latin America, Middle East, Africa).
3. News articles, blog posts, announcements, award ceremonies, book launches, conference highlights — anything that is NOT a job or grant listing.
4. Pure finance, accounting, or marketing faculty positions with no regional/spatial/policy dimension.
5. Positions in clearly irrelevant fields that somehow passed keyword filtering (natural sciences, engineering, medicine, law, computer science, etc.).

KEEP when in doubt. If geography or field is ambiguous, keep it.
KEEP all portable fellowships (MSCA, ERC, Humboldt, etc.) regardless of host institution geography.
KEEP positions that mention economics, geography, regional, spatial, urban, policy, planning, development, inequality, labour, trade, innovation, or similar even if the subfield seems distant.

Return ONLY a JSON object: {"exclude": [list of numbers to exclude], "reasons": {"1": "brief reason", ...}}
Only include excluded items in the reasons dict."""


def haiku_triage(
    grants: list[dict], config: dict
) -> tuple[list[dict], list[dict]]:
    """Send all titles to Haiku in a single batch call for triage.

    Fail-open: if the API call fails, all grants pass through.

    Returns:
        (keep, filtered) — two lists.
    """
    if not grants:
        return [], []

    pf = config.get("pre_filter", {})
    model = pf.get("haiku_model", "claude-haiku-4-5-20241022")

    # Build numbered list
    lines = []
    for i, g in enumerate(grants, 1):
        title = g.get("title", "(no title)")
        inst = g.get("institution") or g.get("source_name", "")
        lines.append(f"{i}. {title} — {inst}")

    user_msg = "\n".join(lines)

    try:
        client = Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # Parse JSON response
        result = _parse_triage_response(text)
        if result is None:
            logger.warning("Haiku triage: could not parse response. Keeping all grants.")
            return grants, []

        exclude_indices = set(result.get("exclude", []))
        reasons = result.get("reasons", {})

        keep: list[dict] = []
        filtered: list[dict] = []

        for i, g in enumerate(grants, 1):
            if i in exclude_indices:
                reason = reasons.get(str(i), "haiku determined irrelevant")
                g["filter_reason"] = f"haiku_triage: {reason}"
                filtered.append(g)
            else:
                keep.append(g)

        logger.info(
            "Haiku triage: %d in, %d excluded, %d remain.",
            len(grants), len(filtered), len(keep),
        )
        return keep, filtered

    except (APIError, Exception) as exc:
        logger.warning("Haiku triage failed (fail-open, keeping all): %s", exc)
        return grants, []


def _parse_triage_response(text: str) -> dict | None:
    """Extract JSON from Haiku's response."""
    stripped = text.strip()

    # Strip markdown fences if present
    if stripped.startswith("```"):
        first_nl = stripped.index("\n")
        stripped = stripped[first_nl + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()

    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Fallback: find first { … } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


# -------------------------------------------------------------------
# Orchestrator
# -------------------------------------------------------------------

def pre_filter(
    grants: list[dict], config: dict, *, dry_run: bool = False
) -> tuple[list[dict], list[dict]]:
    """Run both filter layers sequentially.

    Args:
        grants:  New grants after deduplication.
        config:  Full config dict.
        dry_run: If True, skip Haiku call (keyword filter only).

    Returns:
        (keep, all_filtered) — grants to analyse and rejected grants.
    """
    pf = config.get("pre_filter", {})
    all_filtered: list[dict] = []

    # Layer 1: keyword filter (always runs)
    keep, kw_filtered = keyword_filter(grants, config)
    all_filtered.extend(kw_filtered)

    # Layer 2: Haiku triage (skip in dry-run or if disabled)
    haiku_enabled = pf.get("haiku_enabled", True)
    if haiku_enabled and not dry_run and keep:
        keep, haiku_filtered = haiku_triage(keep, config)
        all_filtered.extend(haiku_filtered)
    elif dry_run:
        logger.info("[DRY RUN] Skipping Haiku triage.")

    logger.info(
        "Pre-filter total: %d in, %d filtered out, %d remain for Sonnet.",
        len(grants), len(all_filtered), len(keep),
    )

    # Stamp metadata on filtered grants
    now = datetime.now(timezone.utc).isoformat()
    for g in all_filtered:
        g["filtered_at"] = now

    return keep, all_filtered
