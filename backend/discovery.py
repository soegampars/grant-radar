"""
discovery.py -- Weekly Opus-powered discovery layer for Grant Radar.

Uses Claude Opus with the server-side web_search tool to find
opportunities NOT covered by RSS feeds and scrapers.  Also provides
re-evaluation of Sonnet-analysed grants and strategic-notes generation.

Public API
----------
discover_opportunities(config)                     -> list[dict]
reevaluate_grants(recent_grants, config)           -> list[dict]
generate_strategic_notes(config)                   -> dict

Server-side web search
----------------------
The Anthropic API executes web searches automatically when the
web_search tool is enabled.  Response content blocks include
``server_tool_use`` and ``web_search_tool_result`` alongside normal
``text`` blocks.  If the server-side sampling loop hits its iteration
limit the response arrives with ``stop_reason == "pause_turn"`` and
we must send the assistant message back to continue.
"""

import json
import logging
import time
from datetime import datetime, timezone

from anthropic import Anthropic, APIError, RateLimitError

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Retry / back-off settings
# -------------------------------------------------------------------
_MAX_RETRIES = 5
_BASE_DELAY = 3.0          # seconds (Opus is slower, give more room)
_MAX_CONTINUATIONS = 6      # pause_turn continuations per call

# -------------------------------------------------------------------
# Web search tool definition
# -------------------------------------------------------------------
_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 20,         # allow generous searching per call
}


# ===================================================================
# DISCOVERY PROMPT
# ===================================================================
# Placeholders: {researcher_json}, {tiers_json}, {timeline_json},
#               {principles_json}, {today}

_DISCOVERY_SYSTEM = r"""You are the discovery engine of Grant Radar, an academic grant and postdoc monitoring system.  You have access to a web_search tool.  Your mission is to find academic job and funding opportunities for the researcher described below that are NOT already covered by the system's RSS feeds and scrapers.

===================================================================
RESEARCHER PROFILE
===================================================================
{researcher_json}

===================================================================
TIER SYSTEM
===================================================================
{tiers_json}

Tier assignment rules:
- Tier 1: Cardiff School of Geography and Planning positions, OR any portable fellowship that could be hosted at Cardiff.  Prof Robert Huggins is the established contact.
- Tier 2: Strong alternatives -- (a) other Cardiff departments, (b) any Dutch university (Groningen MSc connection), (c) Politecnico di Milano postdoc / assegno di ricerca / portable fellowships.
- Tier 3: Any economic geography, regional economics, regional science, spatial economics, or urban economics postdoc or fellowship in Europe.
- Tier 4: BRIN or UGM (Indonesia) positions.  Researcher is a UGM alumnus with IRSA network connections.
- Tier 0: Broadly academic but outside the above tiers.  Still include.

===================================================================
TIMELINE
===================================================================
{timeline_json}

Weight searches toward positions starting between October 2026 and mid-2027.  NEVER exclude earlier or later positions -- still report them with a timeline note.  Today's date is {today}.

===================================================================
DESIGN PRINCIPLES
===================================================================
{principles_json}

===================================================================
SEARCH STRATEGY
===================================================================
You MUST search strategically using a variety of query categories.  Below are suggested starting queries -- you may add others that you judge useful.  Spend your search budget wisely: run the most promising queries first, then branch out.

General academic:
- "postdoc economic geography 2026"
- "research fellow regional economics Europe"
- "postdoctoral position spatial economics"
- "research associate urban economics"

UK funding-specific:
- "MSCA postdoctoral fellowship 2026 call"
- "Leverhulme early career fellowship 2026"
- "ERC starting grant 2026 social science"
- "ESRC postdoctoral fellowship"
- "British Academy postdoctoral fellowship"
- "UKRI Future Leaders Fellowship 2026"
- "British Academy International Fellowship 2026"
- "Nuffield Foundation grant geographical inequality"
- "BA Leverhulme small research grant"

European funding:
- "NWO Veni social science 2026"
- "NWO Vidi economic geography"
- "Humboldt postdoctoral fellowship economics"
- "DFG Walter Benjamin economic geography"
- "FWF Lise Meitner programme social science"
- "ANR JCJC economics geography"
- "ANR Access-ERC postdoc"
- "ESPON call territorial analysis"
- "NORFACE call social science"
- "Open Research Area ORA social science call"

Cardiff-specific:
- "Cardiff University geography postdoc"
- "Wales economic geography research position"
- "Cardiff School of Geography and Planning"

Netherlands-specific:
- "economic geography postdoc Netherlands"
- "NWO Veni economic geography"
- "postdoc VU Amsterdam spatial economics"
- "postdoc Utrecht University economic geography"
- "postdoc University of Groningen regional economics"

Italy-specific:
- "assegno di ricerca geografia economica"
- "bando ricercatore economia regionale"
- "postdoc Politecnico di Milano economics"

Germany-specific:
- "postdoc economic geography Germany"
- "Emmy Noether economic geography"

Japan-specific:
- "JSPS international postdoctoral fellowship"
- "JSPS fellowship social science"
- "Canon Foundation Europe fellowship"
- "Japan Foundation research fellowship economics"

Indonesia-specific:
- "postdoc BRIN 2026"
- "lowongan peneliti BRIN"
- "postdoc UGM ekonomi"
- "beasiswa postdoc Indonesia"

Field-specific:
- "RSA early career research grant"
- "Regional Studies Association grant"
- "ERSA fellowship regional science"

Job boards (cross-check):
- "economic geography postdoc site:inomics.com"
- "postdoc economic geography site:findapostdoc.com"
- "regional economics site:jrecin.jst.go.jp"

Academic social media:
- "postdoc economic geography site:x.com"
- "research fellow regional economics site:x.com"

===================================================================
PORTABLE FELLOWSHIP KNOWLEDGE BASE
===================================================================
UK-BASED:
* MSCA Postdoctoral Fellowships (Marie Curie): PORTABLE. Open to all nationalities. If hostable at Cardiff -> Tier 1.
* Leverhulme Early Career Fellowships: Requires "UK connection". Researcher has 6-month Cardiff visit — may partially count -> check, cap Tier 2.
* ESRC Postdoctoral Fellowships: Requires UK-connected degree. Cardiff visit unlikely sufficient -> check, cap Tier 2.
* ESRC New Investigator Grants: £100K–£300K, rolling. PI at UK institution. If Cardiff -> Tier 1.
* UKRI Future Leaders Fellowships: £300K–£2M+, 4–7yr. NO nationality restrictions. If Cardiff -> Tier 1.
* ERC Starting Grants: PORTABLE within EU/associated. Up to €1.5M/5yr. 2–7yr post-PhD. If Cardiff -> Tier 1.
* British Academy Postdoctoral Fellowships: Requires UK/Commonwealth nationality OR UK PhD. Researcher has neither -> Tier 0, not_eligible.
* British Academy International Fellowships (formerly Newton): For non-UK researchers. Up to £420K/3yr. Indonesian eligible. If Cardiff -> Tier 1.
* BA/Leverhulme Small Research Grants: Up to £10K, lottery-based. 400+ awards/year.
* Nuffield Foundation: Under £300K. Priorities: geographical inequality, cost of living, labour mobility.

EUROPE-WIDE:
* NWO Veni (Netherlands): €320K/3yr. Dutch host required. Any nationality. -> Tier 2.
* NWO Vidi: €850K/5yr. -> Tier 2.
* Humboldt Research Fellowships: Germany. €3K/month, 6–24 months. Open to all, no deadlines. -> Tier 3.
* DFG Walter Benjamin: Postdoc, up to 2yr, can apply from abroad. -> Tier 3.
* FWF Lise Meitner (Austria): ~€200K/2yr for incoming international postdocs. -> Tier 3.
* ANR JCJC (France): Up to ~€300K for early-career.
* ANR Access-ERC (France): Hosts postdocs who commit to ERC application within 2yr.
* ESPON: Service contracts for applied territorial analysis. Core economic geography domain.

JAPAN:
* JSPS International Postdoctoral Fellowships: ¥362K/month, 12–24 months. Within 6yr of PhD. ~230 awards/yr. -> Tier 0.
* Canon Foundation in Europe: Up to €30K/yr, 3–12 months. ~15 awards/yr. -> Tier 0.
* Japan Foundation Research Fellowships: ¥400K/month, 4–12 months. Japan-related research. -> Tier 0.

FIELD-SPECIFIC:
* RSA Early Career Research Grants: Up to £10K. Directly targets regional development.
* RSA Fellowship Research Grants: Up to £8,250.

BILATERAL:
* NORFACE: ~20 national agencies, collaborative social science funding.
* Open Research Area (ORA): Joint proposals linking ANR, DFG, ESRC, NWO, JSPS.

===================================================================
ELIGIBILITY HARD FACTS
===================================================================
* Indonesian nationality.  NOT EU/EEA citizen.
* Italian student residence permit (time-limited, tied to PhD).  NOT equivalent to EU residency.
* Dutch MSc (Groningen).  Satisfies "European degree" but NOT "British degree".
* Does NOT have a British degree or UK nationality/settled status.
* Italian is NOT a proficient working language.
* PhD not yet complete (submission October 2026, defence Q1 2027).

===================================================================
OUTPUT FORMAT
===================================================================
After completing your searches, return a JSON array of opportunity objects.
Each object must have these fields (use null for unknown values):

{
  "title": "standardised title",
  "institution": "institution name",
  "country": "country",
  "city": "city or null",
  "url": "direct URL to the listing",
  "date_posted": "ISO date or null",
  "deadline": "ISO date or null",
  "deadline_display": "human-readable deadline or null",
  "salary": "salary string or null",
  "salary_eur": "EUR equivalent or null",
  "duration": "e.g. '2 years' or null",
  "start_date": "ISO date or null",
  "full_time": true/false/null,
  "funding_covers": "what funding covers or null",
  "career_stage": "requirement description",
  "nationality_requirement": "requirement or 'none stated'",
  "degree_requirement": "requirement or 'none stated'",
  "language_requirement": "requirement or 'none stated'",
  "eligibility_verdict": "eligible" | "not_eligible" | "check",
  "eligibility_reason": "explanation",
  "eligibility_timeline_note": "note or null",
  "research_theme": "required research theme",
  "theme_flexibility": "locked" | "flexible" | "open",
  "methods_relevance": "how methods match",
  "policy_relevant": true/false/null,
  "funding_portable": true/false/null,
  "portability_note": "explanation or null",
  "tier": 1/2/3/4/0,
  "tier_reason": "explanation",
  "relevance_score": 0-100,
  "timeline_fit": "ideal" | "acceptable" | "early" | "gap" | "expired",
  "timeline_note": "explanation",
  "pros": ["list of pros"],
  "cons": ["list of cons"],
  "next_steps": ["suggested actions"],
  "competition_level": "high" | "medium" | "low" | "unknown",
  "one_line_summary": "one sentence for dashboard card",
  "discovery_source": "the search query or method that found this"
}

Return ONLY the JSON array.  No markdown fences, no commentary outside the array.
If you find no new opportunities, return an empty array: []
"""


# ===================================================================
# RE-EVALUATION PROMPT
# ===================================================================

_REEVAL_SYSTEM = r"""You are the re-evaluation engine of Grant Radar.  You have been given grants that were initially analysed by Claude Sonnet during the daily run.  Your job as Opus is to review each grant with deeper reasoning and check for:

1. **Tier accuracy** -- Did Sonnet assign the right tier?  Look especially for:
   - Portable fellowships that Sonnet may not have recognised (MSCA, Leverhulme, ERC, Newton, etc.)
   - Fellowships that could be hosted at Cardiff but were not flagged as Tier 1
   - NWO grants that qualify for Tier 2 via the Groningen connection
2. **Eligibility nuance** -- Review verdicts for edge cases:
   - "PhD required" positions where PhD is imminent (submission Oct 2026)
   - EU citizenship requirements vs Italian residency permit
   - UK degree requirements (researcher has Dutch MSc, not British)
3. **Creative framing** -- Identify opportunities where the researcher's skills could be framed advantageously even if the listing doesn't perfectly match
4. **Missing connections** -- Links to the researcher's network (Prof Huggins at Cardiff, UGM alumni, IRSA connections, Groningen contacts)
5. **Score calibration** -- Are relevance scores well-calibrated?  A Cardiff economic geography postdoc should be near 95-100.

===================================================================
RESEARCHER PROFILE
===================================================================
{researcher_json}

===================================================================
TIER SYSTEM
===================================================================
{tiers_json}

===================================================================
TIMELINE
===================================================================
{timeline_json}

Today's date is {today}.

===================================================================
OUTPUT FORMAT
===================================================================
Return a JSON array of updated grant objects.  For each grant, include ALL original fields plus:
- "reeval_notes": a string explaining what you changed and why (or "No changes" if the Sonnet analysis was correct)
- Updated tier, relevance_score, eligibility_verdict, and any other fields you changed

Preserve the original "id", "url", "source_name", "source_type", and "raw_content" fields unchanged.
Return ONLY the JSON array.  No markdown fences, no commentary outside the array.
"""


# ===================================================================
# STRATEGIC NOTES PROMPT
# ===================================================================

_STRATEGIC_SYSTEM = r"""You are the strategic advisor of Grant Radar.  Your task is to provide the researcher with forward-looking intelligence about grant cycles, upcoming deadlines, and positioning advice.

===================================================================
RESEARCHER PROFILE
===================================================================
{researcher_json}

===================================================================
TIMELINE
===================================================================
{timeline_json}

Today's date is {today}.

===================================================================
TASK
===================================================================
Search for and compile strategic intelligence.  Focus on:

1. **Upcoming grant cycles** relevant to economic geography / regional economics:
   - When do MSCA-PF, Leverhulme ECF, ERC StG, NWO Veni, ESRC, British Academy open?
   - What are their approximate deadlines for 2026-2027?
   - Any new programmes or changes to existing ones?

2. **Deadlines in the next 6 months** (from {today}):
   - List any known deadlines for relevant fellowships and grants
   - Flag deadlines the researcher should start preparing for NOW

3. **Application strategy**:
   - Which fellowships should be prioritised given the researcher's profile?
   - Where is the strongest match (methods, theme, eligibility)?
   - What gaps in the researcher's CV should be addressed?

4. **Emerging opportunities**:
   - New funding schemes, pilot programmes, or institutional initiatives
   - Policy shifts that may create new funding (e.g. UKRI strategy changes)

===================================================================
OUTPUT FORMAT
===================================================================
Return a JSON object with this structure:

{
  "generated_at": "ISO timestamp",
  "grant_cycles": [
    {
      "programme": "programme name",
      "funder": "funding body",
      "typical_deadline": "approximate date or period",
      "next_deadline": "specific date if known, or null",
      "eligibility_match": "eligible" | "check" | "not_eligible",
      "notes": "key information"
    }
  ],
  "upcoming_deadlines": [
    {
      "programme": "name",
      "deadline": "ISO date or description",
      "action_needed": "what to prepare",
      "priority": "high" | "medium" | "low"
    }
  ],
  "strategy_recommendations": [
    "recommendation string"
  ],
  "emerging_opportunities": [
    {
      "opportunity": "description",
      "source": "where you found this",
      "relevance": "why it matters"
    }
  ],
  "cv_gaps_to_address": [
    "gap description and how to address it"
  ]
}

Return ONLY the JSON object.  No markdown fences, no commentary.
"""


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

def _render_prompt(template: str, config: dict) -> str:
    """Fill placeholders in a prompt template with config data."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = template
    prompt = prompt.replace("{researcher_json}",
                            json.dumps(config.get("researcher", {}), indent=2))
    prompt = prompt.replace("{tiers_json}",
                            json.dumps(config.get("tiers", {}), indent=2))
    prompt = prompt.replace("{timeline_json}",
                            json.dumps(config.get("timeline", {}), indent=2))
    prompt = prompt.replace("{principles_json}",
                            json.dumps(config.get("design_principles", {}), indent=2))
    prompt = prompt.replace("{today}", today)
    return prompt


def _extract_text(response) -> str:
    """Extract all text blocks from an Anthropic Messages response."""
    parts = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _parse_json_from_text(text: str):
    """Parse a JSON array or object from mixed text output.

    Tries:
      1. Direct parse of the full text.
      2. Strip markdown fences and parse.
      3. Find the outermost [ ... ] (array).
      4. Find the outermost { ... } (object).
    """
    stripped = text.strip()

    # 1. Direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown fences
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        stripped = stripped[first_nl + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # 3. Find outermost array
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end > arr_start:
        try:
            return json.loads(text[arr_start:arr_end + 1])
        except json.JSONDecodeError:
            pass

    # 4. Find outermost object
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        try:
            return json.loads(text[obj_start:obj_end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _call_with_web_search(
    client: Anthropic,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 16384,
) -> str:
    """Send a request with the web_search tool and handle pause_turn.

    Web search is a server-side tool: the Anthropic API executes
    searches automatically.  If the server-side loop hits its iteration
    limit, the response arrives with stop_reason="pause_turn" and we
    must send the assistant content back to continue.

    Returns:
        Combined text from all response blocks across all continuations.
    """
    messages = [{"role": "user", "content": user_message}]
    all_text_parts: list[str] = []

    for turn in range(_MAX_CONTINUATIONS):
        response = _api_call_with_retry(
            client, model, system, messages, max_tokens,
            tools=[_WEB_SEARCH_TOOL],
        )
        if response is None:
            logger.error("API call returned None after retries.")
            break

        # Log search activity
        search_count = 0
        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "server_tool_use":
                query = getattr(block, "input", {}).get("query", "")
                logger.info("  Web search: %s", query)
                search_count += 1
            elif block_type == "text" and hasattr(block, "text"):
                all_text_parts.append(block.text)

        if search_count:
            total_searches = 0
            if hasattr(response, "usage") and hasattr(response.usage, "server_tool_use"):
                stu = response.usage.server_tool_use
                if isinstance(stu, dict):
                    total_searches = stu.get("web_search_requests", 0)
            logger.info("  Searches this turn: %d (total: %d)",
                        search_count, total_searches)

        # Check if we need to continue
        if response.stop_reason != "pause_turn":
            logger.info("  Discovery complete (stop_reason=%s).",
                        response.stop_reason)
            break

        logger.info("  pause_turn -- continuing (turn %d/%d)...",
                     turn + 1, _MAX_CONTINUATIONS)
        # Send the full response back as assistant message
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": response.content},
        ]

    return "\n".join(all_text_parts)


def _call_without_tools(
    client: Anthropic,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 16384,
) -> str:
    """Send a plain request (no tools) and return the text."""
    messages = [{"role": "user", "content": user_message}]
    response = _api_call_with_retry(
        client, model, system, messages, max_tokens, tools=None,
    )
    if response is None:
        return ""
    return _extract_text(response)


def _api_call_with_retry(
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
):
    """Messages.create with exponential backoff on rate limits / 5xx."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            if tools:
                kwargs["tools"] = tools
            return client.messages.create(**kwargs)

        except RateLimitError as exc:
            delay = _BASE_DELAY * (2 ** (attempt - 1))
            logger.warning("Rate limited (attempt %d/%d). Retrying in %.1fs: %s",
                           attempt, _MAX_RETRIES, delay, exc)
            time.sleep(delay)

        except APIError as exc:
            if exc.status_code and exc.status_code >= 500:
                delay = _BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("API server error %s (attempt %d/%d). Retrying in %.1fs",
                               exc.status_code, attempt, _MAX_RETRIES, delay)
                time.sleep(delay)
            else:
                logger.error("Anthropic API error: %s", exc)
                return None

        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)
            return None

    logger.error("All %d retries exhausted.", _MAX_RETRIES)
    return None


# -------------------------------------------------------------------
# 1. discover_opportunities
# -------------------------------------------------------------------

def discover_opportunities(config: dict) -> list[dict]:
    """Search the web for grant opportunities not in RSS/scraper sources.

    Uses Claude Opus with the web_search server-side tool.  Opus
    autonomously searches using a variety of strategic queries and
    returns structured opportunity objects.

    Args:
        config: The full config dict from config.json.

    Returns:
        List of discovered grant dicts in the standard analysed schema,
        each with an extra ``discovery_source`` field.  Returns ``[]``
        on failure.
    """
    model = config.get("models", {}).get("weekly_discovery", "claude-opus-4-20250514")
    system = _render_prompt(_DISCOVERY_SYSTEM, config)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_message = (
        f"Today is {today}.  Please search for academic postdoc and "
        f"fellowship opportunities for the researcher described in your "
        f"system prompt.  Focus on positions starting between October "
        f"2026 and mid-2027, but do not exclude positions outside that "
        f"window.  Use the search queries suggested in your instructions "
        f"and any others you think are useful.  Return a JSON array of "
        f"opportunity objects."
    )

    logger.info("Starting Opus discovery (model=%s)...", model)
    client = Anthropic()

    raw_text = _call_with_web_search(client, model, system, user_message)

    if not raw_text.strip():
        logger.warning("Discovery returned empty text.")
        return []

    parsed = _parse_json_from_text(raw_text)

    if parsed is None:
        logger.error("Failed to parse discovery JSON. Raw text:\n%s",
                      raw_text[:3000])
        return []

    # Ensure we have a list
    if isinstance(parsed, dict):
        # Single object -- wrap in list
        parsed = [parsed]

    if not isinstance(parsed, list):
        logger.error("Discovery returned non-list type: %s", type(parsed))
        return []

    # Stamp each result
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, grant in enumerate(parsed):
        if not isinstance(grant, dict):
            continue
        grant.setdefault("source_type", "discovery")
        grant.setdefault("source_name", "Opus Discovery")
        grant.setdefault("analysed_at", now_iso)
        grant.setdefault("date_found", today)
        # Generate ID if missing
        if not grant.get("id"):
            from backend.utils import generate_grant_id
            grant["id"] = generate_grant_id(
                grant.get("url", ""),
                grant.get("title", f"discovery_{i}"),
            )

    # Filter out non-dict items
    results = [g for g in parsed if isinstance(g, dict)]
    logger.info("Discovery found %d opportunities.", len(results))
    return results


# -------------------------------------------------------------------
# 2. reevaluate_grants
# -------------------------------------------------------------------

def reevaluate_grants(recent_grants: list[dict], config: dict) -> list[dict]:
    """Re-evaluate Sonnet-analysed grants with deeper Opus reasoning.

    Sends recent grants to Opus for review of tier assignments,
    eligibility nuance, portability detection, and creative framing.

    Args:
        recent_grants: Grants analysed by Sonnet this week.
        config:        The full config dict from config.json.

    Returns:
        Updated grant dicts, each with a ``reeval_notes`` field
        explaining any changes.  Returns the originals unchanged on
        failure.
    """
    if not recent_grants:
        logger.info("No grants to re-evaluate.")
        return []

    model = config.get("models", {}).get("weekly_reeval", "claude-opus-4-20250514")
    system = _render_prompt(_REEVAL_SYSTEM, config)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build the user message with all grants
    # Strip raw_content to save tokens -- Opus sees the already-analysed fields
    grants_for_review = []
    for g in recent_grants:
        slim = {k: v for k, v in g.items() if k != "raw_content"}
        # Keep a truncated raw_content for context
        raw = g.get("raw_content", "")
        if raw:
            slim["raw_content_preview"] = raw[:2000]
        grants_for_review.append(slim)

    user_message = (
        f"Today is {today}.  Below are {len(grants_for_review)} grants "
        f"analysed by Sonnet this week.  Please review each one for "
        f"tier accuracy, eligibility nuance, missed portability, and "
        f"creative framing opportunities.  Return a JSON array of "
        f"updated grant objects.\n\n"
        f"{json.dumps(grants_for_review, indent=2, default=str)}"
    )

    logger.info("Starting Opus re-evaluation of %d grants (model=%s)...",
                len(recent_grants), model)
    client = Anthropic()

    raw_text = _call_without_tools(client, model, system, user_message)

    if not raw_text.strip():
        logger.warning("Re-evaluation returned empty text. Returning originals.")
        return recent_grants

    parsed = _parse_json_from_text(raw_text)

    if parsed is None or not isinstance(parsed, list):
        logger.error("Failed to parse re-evaluation JSON. Returning originals.")
        return recent_grants

    # Merge re-evaluated data back onto originals (match by id)
    reeval_map = {}
    for g in parsed:
        if isinstance(g, dict) and g.get("id"):
            reeval_map[g["id"]] = g

    results = []
    changes = 0
    for orig in recent_grants:
        gid = orig.get("id", "")
        if gid in reeval_map:
            updated = orig.copy()
            reeval = reeval_map[gid]
            # Preserve immutable fields from original
            for key in ("id", "url", "source_name", "source_type", "raw_content"):
                reeval.pop(key, None)
            updated.update(reeval)
            updated["reevaluated_at"] = datetime.now(timezone.utc).isoformat()
            if reeval.get("reeval_notes", "No changes") != "No changes":
                changes += 1
            results.append(updated)
        else:
            # Opus didn't return this grant -- keep original
            orig_copy = orig.copy()
            orig_copy["reeval_notes"] = "Not reviewed by Opus"
            results.append(orig_copy)

    logger.info("Re-evaluation complete: %d/%d grants had changes.",
                changes, len(results))
    return results


# -------------------------------------------------------------------
# 3. generate_strategic_notes
# -------------------------------------------------------------------

def generate_strategic_notes(config: dict) -> dict:
    """Generate strategic intelligence about grant cycles and deadlines.

    Uses Opus with web search to research upcoming grant cycles,
    deadlines in the next 6 months, and application strategy advice.

    Args:
        config: The full config dict from config.json.

    Returns:
        Structured strategic notes dict for data/strategic_notes.json.
        Returns a minimal dict on failure.
    """
    model = config.get("models", {}).get("weekly_discovery", "claude-opus-4-20250514")
    system = _render_prompt(_STRATEGIC_SYSTEM, config)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_message = (
        f"Today is {today}.  Please search for upcoming grant cycles "
        f"and deadlines relevant to the researcher in your system prompt.  "
        f"Focus on the next 6 months and any programmes the researcher "
        f"should start preparing for now.  Return a JSON object with "
        f"grant_cycles, upcoming_deadlines, strategy_recommendations, "
        f"emerging_opportunities, and cv_gaps_to_address."
    )

    logger.info("Generating strategic notes (model=%s)...", model)
    client = Anthropic()

    raw_text = _call_with_web_search(client, model, system, user_message)

    if not raw_text.strip():
        logger.warning("Strategic notes returned empty text.")
        return _empty_strategic_notes(today)

    parsed = _parse_json_from_text(raw_text)

    if parsed is None or not isinstance(parsed, dict):
        logger.error("Failed to parse strategic notes JSON.")
        return _empty_strategic_notes(today)

    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("Strategic notes generated: %d cycles, %d deadlines, %d recommendations.",
                len(parsed.get("grant_cycles", [])),
                len(parsed.get("upcoming_deadlines", [])),
                len(parsed.get("strategy_recommendations", [])))
    return parsed


def _empty_strategic_notes(today: str) -> dict:
    """Return a minimal strategic notes dict on failure."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grant_cycles": [],
        "upcoming_deadlines": [],
        "strategy_recommendations": ["Strategic notes generation failed -- manual review needed."],
        "emerging_opportunities": [],
        "cv_gaps_to_address": [],
        "error": "Generation failed",
    }


# -------------------------------------------------------------------
# CLI testing
# -------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print()
        print("System prompt preview (discovery, first 2000 chars):")
        sys_prompt = _render_prompt(_DISCOVERY_SYSTEM, config)
        print(sys_prompt[:2000])
        print("...")
        print(f"\nFull discovery prompt: {len(sys_prompt)} chars")
        print(f"Re-eval prompt: {len(_render_prompt(_REEVAL_SYSTEM, config))} chars")
        print(f"Strategic prompt: {len(_render_prompt(_STRATEGIC_SYSTEM, config))} chars")
        sys.exit(1)

    # Run discovery
    mode = sys.argv[1] if len(sys.argv) > 1 else "discover"

    if mode == "discover":
        grants = discover_opportunities(config)
        print(f"\nDiscovered {len(grants)} opportunities.")
        for g in grants[:5]:
            display = {k: v for k, v in g.items() if k != "raw_content"}
            print(json.dumps(display, indent=2, default=str))
            print()

    elif mode == "strategic":
        notes = generate_strategic_notes(config)
        print(json.dumps(notes, indent=2, default=str))

    elif mode == "reeval":
        # Load recent grants for re-evaluation
        from backend.utils import load_grants
        grants = load_grants()
        if not grants:
            print("No grants in data/grants.json to re-evaluate.")
            sys.exit(0)
        updated = reevaluate_grants(grants[:10], config)  # limit to 10 for testing
        changes = sum(1 for g in updated
                      if g.get("reeval_notes", "No changes") != "No changes"
                      and g.get("reeval_notes") != "Not reviewed by Opus")
        print(f"\nRe-evaluated {len(updated)} grants, {changes} had changes.")
        for g in updated:
            if g.get("reeval_notes") and g["reeval_notes"] not in ("No changes", "Not reviewed by Opus"):
                print(f"  {g.get('title', '?')}: {g['reeval_notes']}")
