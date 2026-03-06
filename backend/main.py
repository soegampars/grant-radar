"""
main.py -- Grant Radar orchestrator.

Called by GitHub Actions with a mode argument:

    python backend/main.py --mode daily
    python backend/main.py --mode weekly
    python backend/main.py --mode daily --dry-run

Daily mode:
  Fetch -> Deduplicate -> Analyse (Sonnet) -> Save -> Alert Tier 1

Weekly mode:
  Daily + Discover (Opus) -> Re-evaluate -> Strategic notes -> Digest email
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so `backend.*` imports resolve
# when the script is invoked as `python backend/main.py`.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.fetchers.rss_fetcher import get_all_rss_grants
from backend.fetchers.ejm_fetcher import fetch_ejm_ads
from backend.fetchers.web_scraper import get_all_scraped_grants
from backend.utils import (
    deduplicate,
    load_grants,
    save_grants,
    update_run_status,
    load_json,
    save_json,
)
from backend.analyser import analyse_grant, analyse_grants
from backend.discovery import (
    discover_opportunities,
    reevaluate_grants,
    generate_strategic_notes,
)
from backend.notifier import send_tier1_alert, send_weekly_digest

logger = logging.getLogger("grant_radar")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load and return the config.json dict."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _mark_expired(grants: list[dict]) -> list[dict]:
    """Set expired=True on grants whose deadline has passed.

    Never deletes grants -- only adds the flag.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    changed = 0
    for g in grants:
        dl = g.get("deadline")
        if dl and isinstance(dl, str) and len(dl) >= 10:
            try:
                if dl[:10] < today:
                    if not g.get("expired"):
                        g["expired"] = True
                        changed += 1
            except (TypeError, ValueError):
                pass
    if changed:
        logger.info("Marked %d grants as expired.", changed)
    return grants


def _grants_by_tier(grants: list[dict]) -> dict[int, list[dict]]:
    """Group a list of grants by their tier number."""
    by_tier: dict[int, list[dict]] = {}
    for g in grants:
        tier = g.get("tier", 0)
        if not isinstance(tier, int):
            try:
                tier = int(tier)
            except (ValueError, TypeError):
                tier = 0
        by_tier.setdefault(tier, []).append(g)
    return by_tier


def _recent_grants(grants: list[dict], days: int = 7) -> list[dict]:
    """Return grants with date_found in the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [
        g for g in grants
        if (g.get("date_found") or "0000-00-00") >= cutoff
    ]


# ---------------------------------------------------------------------------
# DAILY MODE
# ---------------------------------------------------------------------------

def run_daily(config: dict, dry_run: bool = False) -> dict:
    """Execute the daily grant-fetch pipeline.

    Returns a summary dict with counts and status.
    """
    summary = {
        "mode": "daily",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "fetched": 0,
        "new_after_dedup": 0,
        "analysed": 0,
        "analysis_errors": 0,
        "tier1_alerts_sent": 0,
        "errors": [],
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- 1. Load existing grants ---
    logger.info("Loading existing grants...")
    existing_grants = load_grants()
    logger.info("Existing grants: %d", len(existing_grants))

    # --- 2. Fetch from all sources ---
    all_fetched: list[dict] = []

    # RSS feeds
    logger.info("Fetching RSS feeds...")
    try:
        rss_grants = get_all_rss_grants(config)
        all_fetched.extend(rss_grants)
        logger.info("RSS: %d grants fetched.", len(rss_grants))
    except Exception as exc:
        logger.error("RSS fetcher failed: %s", exc, exc_info=True)
        summary["errors"].append(f"RSS fetcher: {exc}")

    # EconJobMarket API
    logger.info("Fetching EconJobMarket API...")
    try:
        ejm_grants = fetch_ejm_ads(config)
        all_fetched.extend(ejm_grants)
        logger.info("EJM: %d grants fetched.", len(ejm_grants))
    except Exception as exc:
        logger.error("EJM fetcher failed: %s", exc, exc_info=True)
        summary["errors"].append(f"EJM fetcher: {exc}")

    # Web scrapers
    logger.info("Fetching web scrapers...")
    try:
        scraped_grants = get_all_scraped_grants(config)
        all_fetched.extend(scraped_grants)
        logger.info("Scrapers: %d grants fetched.", len(scraped_grants))
    except Exception as exc:
        logger.error("Web scraper failed: %s", exc, exc_info=True)
        summary["errors"].append(f"Web scraper: {exc}")

    summary["fetched"] = len(all_fetched)
    logger.info("Total fetched: %d grants from all sources.", len(all_fetched))

    # --- 3. Deduplicate ---
    new_grants = deduplicate(all_fetched, existing_grants)
    summary["new_after_dedup"] = len(new_grants)
    logger.info("After deduplication: %d new grants.", len(new_grants))

    if not new_grants:
        logger.info("No new grants to analyse.")
        # Still mark expired and save
        existing_grants = _mark_expired(existing_grants)
        save_grants(existing_grants)
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        return summary

    # --- 4. Analyse with Sonnet ---
    if dry_run:
        logger.info("[DRY RUN] Skipping Sonnet analysis of %d grants.", len(new_grants))
        analysed = []
        for g in new_grants:
            mock = g.copy()
            mock["tier"] = 0
            mock["relevance_score"] = 0
            mock["eligibility_verdict"] = "check"
            mock["eligibility_reason"] = "[DRY RUN] Not analysed"
            mock["timeline_fit"] = "ideal"
            mock["timeline_note"] = "[DRY RUN]"
            mock["pros"] = []
            mock["cons"] = []
            mock["next_steps"] = []
            mock["competition_level"] = "unknown"
            mock["one_line_summary"] = f"[DRY RUN] {g.get('title', '')}"
            mock["date_found"] = today
            mock["analysed_at"] = datetime.now(timezone.utc).isoformat()
            analysed.append(mock)
    else:
        logger.info("Analysing %d new grants with Sonnet...", len(new_grants))
        analysed = analyse_grants(new_grants, config)

    # Stamp metadata on each analysed grant
    for g in analysed:
        g.setdefault("date_found", today)
        g.setdefault("collection_method", "daily_fetch")

    succeeded = sum(1 for g in analysed if not g.get("analysis_error"))
    failed = len(analysed) - succeeded
    summary["analysed"] = succeeded
    summary["analysis_errors"] = failed
    logger.info("Analysis: %d succeeded, %d failed.", succeeded, failed)

    # --- 5. Tier 1 alerts ---
    tier1_new = [g for g in analysed if g.get("tier") == 1]
    if tier1_new:
        logger.info("Found %d new Tier 1 grants!", len(tier1_new))
        for g in tier1_new:
            if dry_run:
                logger.info("[DRY RUN] Would send Tier 1 alert: %s", g.get("title"))
            else:
                try:
                    sent = send_tier1_alert(g, config)
                    if sent:
                        summary["tier1_alerts_sent"] += 1
                except Exception as exc:
                    logger.error("Tier 1 alert failed for '%s': %s",
                                 g.get("title"), exc)
                    summary["errors"].append(f"Tier 1 alert: {exc}")

    # --- 6. Append and save ---
    all_grants = existing_grants + analysed
    all_grants = _mark_expired(all_grants)
    save_grants(all_grants)
    logger.info("Saved %d total grants (%d new).", len(all_grants), len(analysed))

    # --- 7. Update run status ---
    update_run_status(
        "daily_pipeline",
        status="success",
        grants_found=len(analysed),
    )

    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    return summary


# ---------------------------------------------------------------------------
# WEEKLY MODE
# ---------------------------------------------------------------------------

def run_weekly(config: dict, dry_run: bool = False) -> dict:
    """Execute the weekly deep-search pipeline.

    Runs daily first, then adds Opus discovery, re-evaluation,
    strategic notes, and the weekly digest email.

    Returns a summary dict with counts and status.
    """
    # --- Run daily first ---
    daily_summary = run_daily(config, dry_run=dry_run)

    summary = {
        "mode": "weekly",
        "daily_summary": daily_summary,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "discovered": 0,
        "reevaluated": 0,
        "reeval_changes": 0,
        "tier1_alerts_sent": daily_summary.get("tier1_alerts_sent", 0),
        "digest_sent": False,
        "errors": list(daily_summary.get("errors", [])),
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- 1. Opus discovery ---
    if dry_run:
        logger.info("[DRY RUN] Skipping Opus discovery.")
        discovered = []
    else:
        logger.info("Running Opus web discovery...")
        try:
            discovered = discover_opportunities(config)
            logger.info("Discovery: %d opportunities found.", len(discovered))
        except Exception as exc:
            logger.error("Discovery failed: %s", exc, exc_info=True)
            summary["errors"].append(f"Discovery: {exc}")
            discovered = []

    # --- 2. Deduplicate discovered grants ---
    existing_grants = load_grants()
    if discovered:
        new_discovered = deduplicate(discovered, existing_grants)
        logger.info("Discovery after dedup: %d new.", len(new_discovered))

        # Stamp metadata
        for g in new_discovered:
            g.setdefault("date_found", today)
            g.setdefault("collection_method", "opus_discovery")
            g.setdefault("source_type", "discovery")
            g.setdefault("source_name", "Opus Discovery")

        # Append
        existing_grants.extend(new_discovered)
        summary["discovered"] = len(new_discovered)

        # Check for Tier 1 among discovered
        disc_tier1 = [g for g in new_discovered if g.get("tier") == 1]
        for g in disc_tier1:
            if dry_run:
                logger.info("[DRY RUN] Would send Tier 1 alert (discovery): %s",
                            g.get("title"))
            else:
                try:
                    sent = send_tier1_alert(g, config)
                    if sent:
                        summary["tier1_alerts_sent"] += 1
                except Exception as exc:
                    logger.error("Tier 1 alert (discovery) failed: %s", exc)
                    summary["errors"].append(f"Tier 1 discovery alert: {exc}")

    # --- 3. Re-evaluate recent grants ---
    recent = _recent_grants(existing_grants, days=7)
    if recent and not dry_run:
        logger.info("Re-evaluating %d recent grants with Opus...", len(recent))
        try:
            reevaluated = reevaluate_grants(recent, config)
            summary["reevaluated"] = len(reevaluated)

            # Apply updates back into the full grants list
            reeval_map = {g.get("id"): g for g in reevaluated if g.get("id")}
            newly_tier1 = []
            changes = 0
            for i, g in enumerate(existing_grants):
                gid = g.get("id", "")
                if gid in reeval_map:
                    updated = reeval_map[gid]
                    # Check if tier was upgraded to 1
                    old_tier = g.get("tier")
                    new_tier = updated.get("tier")
                    if new_tier == 1 and old_tier != 1:
                        newly_tier1.append(updated)
                    if updated.get("reeval_notes", "No changes") != "No changes":
                        changes += 1
                    existing_grants[i] = updated

            summary["reeval_changes"] = changes
            logger.info("Re-evaluation: %d changes applied.", changes)

            # Alert on newly-upgraded Tier 1
            for g in newly_tier1:
                try:
                    sent = send_tier1_alert(g, config)
                    if sent:
                        summary["tier1_alerts_sent"] += 1
                        logger.info("Tier 1 upgrade alert sent: %s", g.get("title"))
                except Exception as exc:
                    logger.error("Tier 1 upgrade alert failed: %s", exc)
                    summary["errors"].append(f"Tier 1 upgrade alert: {exc}")

        except Exception as exc:
            logger.error("Re-evaluation failed: %s", exc, exc_info=True)
            summary["errors"].append(f"Re-evaluation: {exc}")
    elif dry_run:
        logger.info("[DRY RUN] Skipping Opus re-evaluation of %d recent grants.",
                     len(recent))

    # --- 4. Strategic notes ---
    if dry_run:
        logger.info("[DRY RUN] Skipping strategic notes generation.")
        strategic_notes = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "grant_cycles": [],
            "upcoming_deadlines": [],
            "strategy_recommendations": ["[DRY RUN] No strategic notes generated."],
            "emerging_opportunities": [],
            "cv_gaps_to_address": [],
        }
    else:
        logger.info("Generating strategic notes with Opus...")
        try:
            strategic_notes = generate_strategic_notes(config)
            logger.info("Strategic notes generated.")
        except Exception as exc:
            logger.error("Strategic notes failed: %s", exc, exc_info=True)
            summary["errors"].append(f"Strategic notes: {exc}")
            strategic_notes = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "grant_cycles": [],
                "upcoming_deadlines": [],
                "strategy_recommendations": [],
                "emerging_opportunities": [],
                "cv_gaps_to_address": [],
            }

    # --- 5. Save everything ---
    existing_grants = _mark_expired(existing_grants)
    save_grants(existing_grants)
    save_json("strategic_notes.json", strategic_notes)
    logger.info("Saved %d grants and strategic notes.", len(existing_grants))

    # --- 6. Send weekly digest ---
    # Compile this week's new grants by tier
    this_week = _recent_grants(existing_grants, days=7)
    by_tier = _grants_by_tier(this_week)

    # Load source status for the digest
    source_status = load_json("run_status.json")
    if not isinstance(source_status, list):
        source_status = []

    if dry_run:
        logger.info("[DRY RUN] Would send weekly digest with %d grants.",
                     len(this_week))
    else:
        try:
            sent = send_weekly_digest(by_tier, strategic_notes, config, source_status)
            summary["digest_sent"] = sent
            if sent:
                logger.info("Weekly digest sent.")
            else:
                logger.warning("Weekly digest send returned False.")
        except Exception as exc:
            logger.error("Weekly digest failed: %s", exc, exc_info=True)
            summary["errors"].append(f"Weekly digest: {exc}")

    # --- 7. Update run status ---
    update_run_status(
        "weekly_pipeline",
        status="success",
        grants_found=summary["discovered"] + daily_summary.get("new_after_dedup", 0),
    )

    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    return summary


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    """Parse arguments, set up logging, and run the requested mode.

    Returns:
        0 on success, 1 on critical failure.
    """
    parser = argparse.ArgumentParser(
        description="Grant Radar -- academic grant monitoring orchestrator",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly"],
        default="daily",
        help="Pipeline mode: 'daily' (fetch+analyse) or 'weekly' (daily+discovery+digest).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without calling Claude API or sending emails.  Useful for testing.",
    )
    args = parser.parse_args()

    # --- Logging: console + in-memory buffer ---
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    root_logger.addHandler(console)

    # In-memory handler (for summary / status)
    log_buffer = StringIO()
    mem_handler = logging.StreamHandler(log_buffer)
    mem_handler.setLevel(logging.WARNING)
    mem_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    root_logger.addHandler(mem_handler)

    # --- Check API key ---
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY environment variable is not set.")
        logger.error("Set it or use --dry-run for testing without the API.")
        return 1

    # --- Run ---
    logger.info("=" * 60)
    logger.info("Grant Radar -- %s mode%s",
                args.mode.upper(),
                " (DRY RUN)" if args.dry_run else "")
    logger.info("=" * 60)

    start = time.time()

    try:
        if args.mode == "weekly":
            summary = run_weekly(_load_config(), dry_run=args.dry_run)
        else:
            summary = run_daily(_load_config(), dry_run=args.dry_run)
    except Exception as exc:
        logger.critical("Pipeline crashed: %s", exc, exc_info=True)
        # Try to save a crash record
        try:
            update_run_status(
                f"{args.mode}_pipeline",
                status="error",
                error_msg=str(exc),
            )
        except Exception:
            pass
        return 1

    elapsed = time.time() - start

    # --- Print summary ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info("Mode:            %s%s", args.mode,
                " (dry run)" if args.dry_run else "")
    logger.info("Duration:        %.1fs", elapsed)
    logger.info("Fetched:         %d grants", summary.get("fetched", 0))
    logger.info("New (deduped):   %d", summary.get("new_after_dedup", 0))
    logger.info("Analysed:        %d", summary.get("analysed", 0))
    logger.info("Analysis errors: %d", summary.get("analysis_errors", 0))
    logger.info("Tier 1 alerts:   %d", summary.get("tier1_alerts_sent", 0))

    if args.mode == "weekly":
        logger.info("Discovered:      %d", summary.get("discovered", 0))
        logger.info("Re-eval changes: %d", summary.get("reeval_changes", 0))
        logger.info("Digest sent:     %s", summary.get("digest_sent", False))

    errors = summary.get("errors", [])
    if errors:
        logger.info("Errors (%d):", len(errors))
        for e in errors:
            logger.info("  - %s", e)
    else:
        logger.info("Errors:          none")

    logger.info("=" * 60)

    # Save the summary itself for debugging
    try:
        save_json("last_run_summary.json", summary)
    except Exception:
        pass

    # Exit code: 0 if no critical errors, 1 if pipeline had issues
    # (partial failures are still 0 -- only total crashes return 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
