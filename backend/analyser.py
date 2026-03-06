"""
analyser.py — Grant relevance analysis via Claude API.

Core intelligence layer of Grant Radar.  Each scraped grant is sent to
Claude Sonnet (daily) for structured analysis against the researcher
profile.  Claude returns a rich JSON object with eligibility verdicts,
tier assignments, relevance scores, pros/cons, and next steps.

Public API
----------
analyse_grant(grant_dict, config)   -> dict   (single grant)
analyse_grants(grants_list, config) -> list    (batch, sequential)
"""

import json
import logging
import time
from datetime import datetime, timezone

from anthropic import Anthropic, APIError, RateLimitError

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# System prompt template
# -------------------------------------------------------------------
#
# Placeholders filled at runtime:
#   {researcher_json}   — full researcher block from config.json
#   {tiers_json}        — tier definitions
#   {timeline_json}     — timeline block
#   {principles_json}   — design principles
#   {today}             — ISO date of the run

SYSTEM_PROMPT_TEMPLATE = r"""You are the analysis engine of Grant Radar, an academic grant and postdoc monitoring system.  Your job is to evaluate a single grant / fellowship / position listing against the researcher profile below and return a structured JSON assessment.

===================================================================
RESEARCHER PROFILE
===================================================================
{researcher_json}

===================================================================
TIER SYSTEM
===================================================================
{tiers_json}

Tier assignment rules:
- Tier 1: Cardiff School of Geography and Planning positions, OR any portable fellowship that could be hosted at Cardiff for economic geography research.  Prof Robert Huggins is the established contact.
- Tier 2: Strong alternatives — (a) other Cardiff departments, (b) any Dutch university (researcher has Groningen MSc connection), (c) Politecnico di Milano postdoc / assegno di ricerca / portable fellowships.
- Tier 3: Any economic geography, regional economics, regional science, spatial economics, or urban economics postdoc or fellowship anywhere in Europe.
- Tier 4: BRIN or UGM (Indonesia) positions.  Researcher is a UGM alumnus with IRSA network connections.
- Tier 0: Does not fit any of the above tiers but is still within the broad academic domain.  NEVER exclude these — still analyse and score them.

===================================================================
TIMELINE
===================================================================
{timeline_json}

Timeline evaluation rules:
- "ideal": Start date falls in Q4 2026 – Q2 2027.
- "acceptable": Start date falls in Q3 2027 – Q4 2027.
- "early": Start date is before October 2026 (before PhD submission). Flag but do NOT exclude.
- "gap": Start date is 2028 or later (funding gap after PNRR October 2026 expiry).
- "expired": Deadline has already passed (before {today}).  Still include and flag.
- If start date is unknown, set timeline_fit to "ideal" and add a note.
CRITICAL: NEVER exclude a grant because of timeline.  Timeline mismatches are communicated through the timeline_fit field and timeline_note, never by omission.

===================================================================
DESIGN PRINCIPLES (mandatory)
===================================================================
{principles_json}

These principles override any inclination to be "helpful" by filtering out results.  Every grant that is even tangentially related to economics, geography, regional science, urban planning, or policy must be scored — not discarded.

===================================================================
ELIGIBILITY RULES
===================================================================
Evaluate eligibility carefully using the researcher profile's eligibility block.

HARD FACTS — always apply these:
* The researcher is Indonesian.  He does NOT hold EU/EEA citizenship.
* He holds an Italian student residence permit tied to his PhD (time-limited).  This does NOT equal "EU residency" for grants that require EU citizenship.
* He has a Dutch MSc (Groningen).  This satisfies "European postgraduate degree" but NOT "British degree" or "UK university graduate".
* He does NOT have a British degree or UK nationality/settled status.
* Italian is NOT a proficient working language — only basic daily use.  Positions requiring professional Italian should be flagged as a barrier.
* PhD is not yet complete.  Positions requiring a completed PhD at time of application should be checked against the timeline (submission October 2026, defence Q1 2027).
* The researcher is NOT looking for another PhD / doctoral position.  If a listing is clearly a PhD studentship, doctoral position, or pre-doctoral fellowship (not a postdoc), set relevance_score to 0, tier to 0, and set career_stage to "phd" so the dashboard can filter it.

Eligibility verdicts:
- "eligible": Researcher clearly meets all stated requirements.
- "not_eligible": A hard disqualifying condition applies (e.g. "UK nationality required").
- "check": Requirements are ambiguous or may depend on timing (e.g. "PhD required" but defence is imminent).  Always prefer "check" over "not_eligible" when uncertain.

===================================================================
PORTABLE FELLOWSHIP KNOWLEDGE BASE
===================================================================
When you encounter a fellowship, check whether it is portable (can be taken to a host institution of the applicant's choice).  This affects tier assignment — a portable fellowship hostable at Cardiff = Tier 1.

Known fellowships:
* MSCA Postdoctoral Fellowships (Marie Curie):  PORTABLE within EU/associated countries.  Applicant chooses host.  Open to all nationalities.  Highly competitive.  If hostable at Cardiff -> Tier 1.
* Leverhulme Early Career Fellowships:  Requires a UK host institution (Cardiff qualifies).  UK degree is NOT required, but check latest terms.  3-year fellowship.  If Cardiff hosts -> Tier 1.
* ESRC (UKRI) Postdoctoral Fellowships:  Usually requires a UK institution as host.  Check nationality requirements — some schemes require UK/EU settled status.  If eligible and Cardiff hosts -> Tier 1.
* ERC Starting Grants:  PORTABLE within EU/associated countries.  Applicant chooses host.  Open to all nationalities.  2–7 years post-PhD.  If hostable at Cardiff -> Tier 1.
* NWO Veni (Netherlands):  Requires a Dutch host institution.  Check nationality/residency rules — traditionally open to all but verify.  If eligible -> Tier 2 (Netherlands connection via Groningen).
* British Academy Postdoctoral Fellowships:  Usually requires UK host.  Check nationality requirements carefully — has historically been restricted.  If eligible and Cardiff hosts -> Tier 1.
* Newton International Fellowships:  For non-UK researchers to work in the UK.  Indonesian nationality is eligible.  If Cardiff hosts -> Tier 1.
* Humboldt Research Fellowships:  Germany-based, open to all nationalities.  -> Tier 3.

If you recognise a fellowship not in this list, still evaluate portability from the listing text and assign the appropriate tier.

===================================================================
OUTPUT FORMAT
===================================================================
Return ONLY a single JSON object (no markdown fences, no commentary).
Every field must be present.  Use null for unknown values.

{
  "title": "cleaned/standardised title",
  "institution": "extracted institution name",
  "country": "country",
  "city": "city if available",
  "url": "original URL (pass through unchanged)",
  "date_posted": "ISO date or null",
  "deadline": "ISO date (YYYY-MM-DD) or null",
  "deadline_display": "human-readable deadline string, e.g. '15 March 2026'",
  "salary": "original currency amount string or null",
  "salary_eur": "approximate EUR equivalent or null",
  "duration": "e.g. '2 years' or null",
  "start_date": "ISO date or null",
  "full_time": true | false | null,
  "funding_covers": "what the funding covers beyond salary, or null",
  "career_stage": "requirement description, e.g. 'PhD + 0-4 years'",
  "nationality_requirement": "extracted requirement or 'none stated'",
  "degree_requirement": "extracted requirement or 'none stated'",
  "language_requirement": "extracted requirement or 'none stated'",
  "eligibility_verdict": "eligible" | "not_eligible" | "check",
  "eligibility_reason": "one-paragraph explanation of verdict",
  "eligibility_timeline_note": "note if eligibility depends on PhD completion timing, or null",
  "research_theme": "required/preferred research theme of the position",
  "theme_flexibility": "locked" | "flexible" | "open",
  "methods_relevance": "how the researcher's methods (spatial econometrics, microsimulation, etc.) match",
  "policy_relevant": true | false | null,
  "funding_portable": true | false | null,
  "portability_note": "explanation if portable (e.g. 'MSCA-PF can be hosted at Cardiff'), or null",
  "tier": 1 | 2 | 3 | 4 | 0,
  "tier_reason": "concise explanation of tier assignment",
  "relevance_score": 0-100,
  "timeline_fit": "ideal" | "acceptable" | "early" | "gap" | "expired",
  "timeline_note": "explanation of timeline assessment",
  "pros": ["list", "of", "pros", "for this researcher"],
  "cons": ["list", "of", "cons", "for this researcher"],
  "next_steps": ["suggested", "actions"],
  "competition_level": "high" | "medium" | "low" | "unknown",
  "one_line_summary": "one sentence summary suitable for a dashboard card"
}

Scoring guidance for relevance_score (0-100):
- 90-100: Perfect match — correct field, eligible, good timeline, strong institutional connection.
- 70-89:  Strong match — relevant field, likely eligible, minor concerns.
- 50-69:  Moderate — broadly relevant field, some eligibility or fit concerns.
- 30-49:  Weak — tangentially related, significant barriers.
- 10-29:  Poor match — outside core field but still within economics/geography broadly.
- 0-9:    Not relevant but collected anyway (e.g. pure mathematics paper from arXiv).

METHODS SCORING PENALTY:
The researcher's methods are QUANTITATIVE: spatial econometrics, microsimulation, GIS, agent-based modelling.
- Positions with a strong QUALITATIVE methods focus (ethnography, interviews, discourse analysis, participatory research, case studies as primary method) should receive a scoring penalty of -15 to -25 points.
- Set methods_relevance to describe the mismatch clearly (e.g. "Primarily qualitative — ethnographic fieldwork, not aligned with researcher's quantitative toolkit").
- Mixed-methods positions that also value quantitative skills should NOT be penalised.
- Positions that are methods-agnostic or policy-focused should NOT be penalised.
"""


# -------------------------------------------------------------------
# Expected output fields (for validation / fallback construction)
# -------------------------------------------------------------------

_EXPECTED_FIELDS: list[str] = [
    "title", "institution", "country", "city", "url",
    "date_posted", "deadline", "deadline_display",
    "salary", "salary_eur", "duration", "start_date", "full_time",
    "funding_covers", "career_stage",
    "nationality_requirement", "degree_requirement", "language_requirement",
    "eligibility_verdict", "eligibility_reason", "eligibility_timeline_note",
    "research_theme", "theme_flexibility", "methods_relevance",
    "policy_relevant", "funding_portable", "portability_note",
    "tier", "tier_reason", "relevance_score",
    "timeline_fit", "timeline_note",
    "pros", "cons", "next_steps",
    "competition_level", "one_line_summary",
]


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

def _build_system_prompt(config: dict) -> str:
    """Render the system prompt with researcher profile data.

    Uses manual replacement instead of str.format() because the
    template contains literal JSON braces in the output-format example.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    researcher_json = json.dumps(config.get("researcher", {}), indent=2)
    tiers_json = json.dumps(config.get("tiers", {}), indent=2)
    timeline_json = json.dumps(config.get("timeline", {}), indent=2)
    principles_json = json.dumps(config.get("design_principles", {}), indent=2)

    prompt = SYSTEM_PROMPT_TEMPLATE
    prompt = prompt.replace("{researcher_json}", researcher_json)
    prompt = prompt.replace("{tiers_json}", tiers_json)
    prompt = prompt.replace("{timeline_json}", timeline_json)
    prompt = prompt.replace("{principles_json}", principles_json)
    prompt = prompt.replace("{today}", today)
    return prompt


def _build_user_message(grant: dict) -> str:
    """Build the user-message payload for a single grant."""
    parts = [
        f"SOURCE: {grant.get('source_name', 'unknown')} ({grant.get('source_type', 'unknown')})",
        f"TITLE:  {grant.get('title', '(no title)')}",
        f"URL:    {grant.get('url', '')}",
    ]

    if grant.get("date_posted"):
        parts.append(f"DATE POSTED: {grant['date_posted']}")
    if grant.get("deadline"):
        parts.append(f"DEADLINE:    {grant['deadline']}")
    if grant.get("institution"):
        parts.append(f"INSTITUTION: {grant['institution']}")
    if grant.get("country"):
        parts.append(f"COUNTRY:     {grant['country']}")

    parts.append("")
    parts.append("--- RAW CONTENT ---")
    raw = grant.get("raw_content", grant.get("description", ""))
    # Truncate to ~12 000 chars to stay within token budget
    if len(raw) > 12_000:
        raw = raw[:12_000] + "\n[... truncated ...]"
    parts.append(raw)

    return "\n".join(parts)


def _parse_json_response(text: str) -> dict | None:
    """Try to extract a JSON object from the model's response text."""
    stripped = text.strip()

    # Strip markdown code fences if present
    if stripped.startswith("```"):
        # Remove opening fence (```json or ```)
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

    # Second attempt: find the first { … } block
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


def _make_fallback(grant: dict, error: str) -> dict:
    """Construct a minimal analysed grant when API or parsing fails."""
    result = {field: None for field in _EXPECTED_FIELDS}
    # Preserve original grant fields
    result["title"] = grant.get("title", "(analysis failed)")
    result["url"] = grant.get("url", "")
    result["date_posted"] = grant.get("date_posted")
    result["deadline"] = grant.get("deadline")
    result["institution"] = grant.get("institution")
    result["country"] = grant.get("country")
    # Mark as unanalysed
    result["tier"] = 0
    result["relevance_score"] = 0
    result["eligibility_verdict"] = "check"
    result["eligibility_reason"] = f"Automated analysis failed: {error}"
    result["timeline_fit"] = "ideal"
    result["timeline_note"] = "Not assessed (analysis error)"
    result["pros"] = []
    result["cons"] = ["Automated analysis failed — manual review needed"]
    result["next_steps"] = ["Review this listing manually"]
    result["competition_level"] = "unknown"
    result["one_line_summary"] = f"[Analysis failed] {grant.get('title', '')}"
    result["analysis_error"] = True
    return result


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------

# Retry / back-off settings
_MAX_RETRIES = 5
_BASE_DELAY = 2.0          # seconds
_INTER_CALL_DELAY = 1.0    # seconds between sequential calls


def analyse_grant(grant: dict, config: dict) -> dict:
    """Analyse a single grant against the researcher profile.

    Sends the grant to the Anthropic API with the full researcher
    profile as system context.  Returns the grant dict augmented with
    all analysis fields.

    Args:
        grant:  A grant dict from any fetcher.
        config: The full config dict from config.json.

    Returns:
        A new dict merging the original grant data with Claude's
        analysis fields plus ``analysed_at`` timestamp.  If analysis
        fails, the dict will have ``analysis_error: true`` and
        fallback values.
    """
    model = config.get("models", {}).get("daily_analysis", "claude-sonnet-4-20250514")
    system_prompt = _build_system_prompt(config)
    user_message = _build_user_message(grant)

    client = Anthropic()  # reads ANTHROPIC_API_KEY from environment

    # Exponential back-off for rate limits
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            # Extract text from the response
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text

            parsed = _parse_json_response(text)
            if parsed is None:
                logger.error(
                    "JSON parse failed for '%s'. Raw response:\n%s",
                    grant.get("title", "?"),
                    text[:2000],
                )
                result = _make_fallback(grant, "JSON parse failure")
            else:
                # Ensure all expected fields are present (fill missing with None)
                result = {field: None for field in _EXPECTED_FIELDS}
                result.update(parsed)

            # Merge original grant identifiers that must not be overwritten
            result["id"] = grant.get("id", "")
            result["source_name"] = grant.get("source_name", "")
            result["source_type"] = grant.get("source_type", "")
            result["raw_content"] = grant.get("raw_content", "")
            result["analysed_at"] = datetime.now(timezone.utc).isoformat()
            result["date_found"] = (
                grant.get("date_found")
                or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )

            logger.info(
                "Analysed: '%s' -> tier=%s  score=%s  verdict=%s",
                result.get("title", "?"),
                result.get("tier"),
                result.get("relevance_score"),
                result.get("eligibility_verdict"),
            )
            return result

        except RateLimitError as exc:
            delay = _BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Rate limited (attempt %d/%d). Retrying in %.1fs: %s",
                attempt, _MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)

        except APIError as exc:
            if exc.status_code and exc.status_code >= 500:
                delay = _BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "API server error %s (attempt %d/%d). Retrying in %.1fs",
                    exc.status_code, attempt, _MAX_RETRIES, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Anthropic API error for '%s': %s",
                    grant.get("title", "?"), exc,
                )
                result = _make_fallback(grant, f"API error: {exc}")
                result["id"] = grant.get("id", "")
                result["source_name"] = grant.get("source_name", "")
                result["source_type"] = grant.get("source_type", "")
                result["raw_content"] = grant.get("raw_content", "")
                result["analysed_at"] = datetime.now(timezone.utc).isoformat()
                result["date_found"] = (
                    grant.get("date_found")
                    or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                )
                return result

        except Exception as exc:
            logger.error(
                "Unexpected error analysing '%s': %s",
                grant.get("title", "?"), exc,
                exc_info=True,
            )
            result = _make_fallback(grant, str(exc))
            result["id"] = grant.get("id", "")
            result["source_name"] = grant.get("source_name", "")
            result["source_type"] = grant.get("source_type", "")
            result["raw_content"] = grant.get("raw_content", "")
            result["analysed_at"] = datetime.now(timezone.utc).isoformat()
            result["date_found"] = (
                grant.get("date_found")
                or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )
            return result

    # All retries exhausted
    logger.error("All %d retries exhausted for '%s'.", _MAX_RETRIES, grant.get("title", "?"))
    result = _make_fallback(grant, f"All {_MAX_RETRIES} retries exhausted (rate limited)")
    result["id"] = grant.get("id", "")
    result["source_name"] = grant.get("source_name", "")
    result["source_type"] = grant.get("source_type", "")
    result["raw_content"] = grant.get("raw_content", "")
    result["analysed_at"] = datetime.now(timezone.utc).isoformat()
    result["date_found"] = (
        grant.get("date_found")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    return result


def analyse_grants(grants: list[dict], config: dict) -> list[dict]:
    """Analyse a batch of grants sequentially.

    Processes each grant through :func:`analyse_grant` with a small
    inter-call delay to stay within Anthropic rate limits.

    Args:
        grants: List of grant dicts from any fetcher.
        config: The full config dict from config.json.

    Returns:
        List of analysed grant dicts (same order as input).
    """
    total = len(grants)
    logger.info("Starting batch analysis of %d grants.", total)
    results: list[dict] = []

    for i, grant in enumerate(grants, 1):
        logger.info("Analysing grant %d/%d: '%s'", i, total, grant.get("title", "?"))
        analysed = analyse_grant(grant, config)
        results.append(analysed)

        # Small delay between calls to stay within rate limits
        if i < total:
            time.sleep(_INTER_CALL_DELAY)

    succeeded = sum(1 for r in results if not r.get("analysis_error"))
    failed = total - succeeded
    logger.info(
        "Batch analysis complete: %d/%d succeeded, %d failed.",
        succeeded, total, failed,
    )
    return results


# -------------------------------------------------------------------
# CLI testing
# -------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    # Quick test: create a synthetic grant and analyse it
    test_grant = {
        "id": "test_001",
        "title": "Postdoctoral Research Associate in Economic Geography",
        "url": "https://example.com/jobs/123",
        "date_posted": "2026-02-15",
        "deadline": "2026-04-30",
        "description": (
            "Cardiff University School of Geography and Planning seeks "
            "a Postdoctoral Research Associate in Economic Geography. "
            "The successful candidate will work with Prof Robert Huggins "
            "on a project examining regional economic resilience. "
            "Requirements: PhD in geography, economics, or related field. "
            "Salary: Grade 6, £35,326 - £40,927 per annum. "
            "Fixed-term for 2 years. Start date: January 2027."
        ),
        "source_name": "Jobs.ac.uk - Geography",
        "source_type": "web_scrape",
        "raw_content": (
            "Title: Postdoctoral Research Associate in Economic Geography\n"
            "Employer: Cardiff University\n"
            "Department: School of Geography and Planning\n"
            "Location: Cardiff, Wales, UK\n"
            "Salary: £35,326 - £40,927 per annum\n"
            "Date placed: 15 Feb 2026\n"
            "Closing date: 30 April 2026\n"
            "URL: https://example.com/jobs/123\n"
            "Description: The School of Geography and Planning at Cardiff "
            "University is seeking a Postdoctoral Research Associate to "
            "join Prof Robert Huggins' research group on regional economic "
            "resilience and competitiveness. The role involves spatial "
            "econometric analysis of regional development patterns in the "
            "UK and Europe. Candidates should have (or be close to "
            "completing) a PhD in economic geography, regional economics, "
            "or a related discipline. Experience with spatial econometrics "
            "and policy evaluation is desirable. The position is full-time "
            "for 2 years, starting January 2027. International applicants "
            "are welcome; visa sponsorship is available."
        ),
    }

    # Check if API key is set
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Set it to run the live analysis test.")
        print("\nSystem prompt preview (first 2000 chars):")
        print(_build_system_prompt(config)[:2000])
        print("\n…\n")
        print("User message preview:")
        print(_build_user_message(test_grant))
        sys.exit(1)

    result = analyse_grant(test_grant, config)

    print("\n" + "=" * 60)
    print("Analysis Result")
    print("=" * 60)
    # Pretty-print, excluding raw_content for readability
    display = {k: v for k, v in result.items() if k != "raw_content"}
    print(json.dumps(display, indent=2, default=str))
