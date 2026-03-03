"""
notifier.py -- Email notification module for Grant Radar.

Sends formatted HTML emails for Tier 1 alerts (immediate) and
weekly digests (after the Opus run).  Uses Python's smtplib and
email.mime modules with inline CSS for mobile-friendly dark-theme
rendering.

Public API
----------
send_tier1_alert(grant, config)
send_weekly_digest(grants_by_tier, strategic_notes, config)

SMTP credentials come from environment variables:
  EMAIL_ADDRESS   -- sender address
  EMAIL_PASSWORD  -- SMTP app-password
  SMTP_SERVER     -- default smtp.gmail.com
  SMTP_PORT       -- default 587

The recipient address lives in config["email"]["recipient_email"].
"""

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Inline CSS tokens (dark theme, mobile-friendly)
# -------------------------------------------------------------------

_BODY_CSS = (
    "margin:0;padding:0;background-color:#1a1a2e;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "'Helvetica Neue',Arial,sans-serif;color:#e0e0e0;"
)
_CONTAINER_CSS = (
    "max-width:640px;margin:0 auto;padding:24px 16px;"
)
_HEADER_CSS = (
    "font-size:22px;font-weight:700;color:#ffffff;"
    "padding-bottom:12px;border-bottom:2px solid #e94560;"
    "margin-bottom:20px;"
)
_CARD_CSS = (
    "background-color:#16213e;border-radius:8px;"
    "padding:16px;margin-bottom:16px;"
    "border-left:4px solid {border_color};"
)
_CARD_TITLE_CSS = (
    "font-size:16px;font-weight:600;color:#ffffff;"
    "margin:0 0 8px 0;line-height:1.3;"
)
_CARD_META_CSS = (
    "font-size:13px;color:#a0a0b8;margin:2px 0;"
)
_PILL_CSS = (
    "display:inline-block;padding:2px 8px;border-radius:10px;"
    "font-size:11px;font-weight:600;"
    "color:#fff;background-color:{bg};"
)
_SECTION_TITLE_CSS = (
    "font-size:18px;font-weight:700;color:#e94560;"
    "margin:28px 0 12px 0;padding-bottom:6px;"
    "border-bottom:1px solid #2a2a4a;"
)
_LIST_ITEM_CSS = (
    "font-size:14px;color:#e0e0e0;padding:6px 0;"
    "border-bottom:1px solid #2a2a4a;"
)
_FOOTER_CSS = (
    "font-size:12px;color:#6a6a8a;text-align:center;"
    "padding-top:20px;margin-top:24px;"
    "border-top:1px solid #2a2a4a;"
)
_LINK_CSS = "color:#4fc3f7;text-decoration:none;"
_SUMMARY_CSS = (
    "font-size:14px;color:#c0c0d8;margin-bottom:20px;"
    "background-color:#0f3460;border-radius:8px;padding:14px;"
)
_PROS_CSS = "color:#66bb6a;font-size:13px;margin:2px 0;"
_CONS_CSS = "color:#ef5350;font-size:13px;margin:2px 0;"
_STEPS_CSS = "color:#4fc3f7;font-size:13px;margin:2px 0;"

# Eligibility badge colours
_VERDICT_COLOURS = {
    "eligible": "#2e7d32",
    "check": "#f9a825",
    "not_eligible": "#c62828",
}

# Tier accent colours
_TIER_COLOURS = {
    1: "#e94560",
    2: "#f9a825",
    3: "#4fc3f7",
    4: "#66bb6a",
    0: "#6a6a8a",
}

_TIER_NAMES = {
    1: "Tier 1 -- Cardiff + Welsh",
    2: "Tier 2 -- Strong Alternatives",
    3: "Tier 3 -- Good European",
    4: "Tier 4 -- Indonesia",
    0: "Tier 0 -- Other",
}


# -------------------------------------------------------------------
# HTML helpers
# -------------------------------------------------------------------

def _esc(text) -> str:
    """Minimal HTML escaping."""
    if text is None:
        return ""
    s = str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _deadline_countdown(deadline_str) -> str:
    """Return '(X days remaining)' or '(expired)' from an ISO date."""
    if not deadline_str:
        return ""
    try:
        dl = datetime.strptime(str(deadline_str)[:10], "%Y-%m-%d")
        today = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = (dl - today).days
        if delta < 0:
            return '<span style="color:#ef5350;">(expired)</span>'
        elif delta == 0:
            return '<span style="color:#f9a825;">(today!)</span>'
        elif delta <= 7:
            return f'<span style="color:#f9a825;">({delta}d remaining)</span>'
        else:
            return f'<span style="color:#a0a0b8;">({delta}d remaining)</span>'
    except (ValueError, TypeError):
        return ""


def _pill(label: str, bg: str) -> str:
    """Render a coloured pill/badge."""
    css = _PILL_CSS.replace("{bg}", bg)
    return f'<span style="{css}">{_esc(label)}</span>'


def _eligibility_badge(verdict) -> str:
    """Render eligibility verdict as a coloured badge."""
    verdict = str(verdict or "check").lower()
    bg = _VERDICT_COLOURS.get(verdict, "#6a6a8a")
    label_map = {
        "eligible": "ELIGIBLE",
        "check": "CHECK",
        "not_eligible": "NOT ELIGIBLE",
    }
    return _pill(label_map.get(verdict, verdict.upper()), bg)


def _tier_badge(tier) -> str:
    """Render tier number as a coloured badge."""
    tier = int(tier) if tier is not None else 0
    bg = _TIER_COLOURS.get(tier, "#6a6a8a")
    return _pill(f"Tier {tier}", bg)


def _score_bar(score) -> str:
    """Render a small inline score indicator."""
    score = int(score or 0)
    if score >= 70:
        colour = "#66bb6a"
    elif score >= 50:
        colour = "#f9a825"
    else:
        colour = "#ef5350"
    bar_width = max(score, 5)
    return (
        f'<div style="display:inline-block;width:60px;height:8px;'
        f'background:#2a2a4a;border-radius:4px;vertical-align:middle;">'
        f'<div style="width:{bar_width}%;height:100%;'
        f'background:{colour};border-radius:4px;"></div></div>'
        f' <span style="font-size:12px;color:{colour};font-weight:600;">{score}</span>'
    )


# -------------------------------------------------------------------
# Grant card renderers
# -------------------------------------------------------------------

def _render_full_card(grant: dict) -> str:
    """Full card for Tier 1 alerts and Tier 1/2 digest sections."""
    tier = grant.get("tier", 0)
    border = _TIER_COLOURS.get(tier, "#6a6a8a")
    card_css = _CARD_CSS.replace("{border_color}", border)

    title = _esc(grant.get("title", "(no title)"))
    url = _esc(grant.get("url", ""))
    institution = _esc(grant.get("institution", ""))
    country = _esc(grant.get("country", ""))
    city = _esc(grant.get("city", ""))
    location = ", ".join(p for p in [city, country] if p) or "Location unknown"

    deadline_display = _esc(grant.get("deadline_display") or grant.get("deadline") or "Not specified")
    countdown = _deadline_countdown(grant.get("deadline"))
    salary = _esc(grant.get("salary") or "Not specified")
    duration = _esc(grant.get("duration") or "Not specified")
    start_date = _esc(grant.get("start_date") or "Not specified")

    summary = _esc(grant.get("one_line_summary", ""))
    eligibility = _eligibility_badge(grant.get("eligibility_verdict"))
    elig_reason = _esc(grant.get("eligibility_reason", ""))
    timeline_fit = _esc(grant.get("timeline_fit", ""))
    timeline_note = _esc(grant.get("timeline_note", ""))

    # Pros
    pros_html = ""
    for p in (grant.get("pros") or []):
        pros_html += f'<div style="{_PROS_CSS}">+ {_esc(p)}</div>'

    # Cons
    cons_html = ""
    for c in (grant.get("cons") or []):
        cons_html += f'<div style="{_CONS_CSS}">&minus; {_esc(c)}</div>'

    # Next steps
    steps_html = ""
    for s in (grant.get("next_steps") or []):
        steps_html += f'<div style="{_STEPS_CSS}">&rarr; {_esc(s)}</div>'

    return f"""<div style="{card_css}">
  <div style="{_CARD_TITLE_CSS}">
    <a href="{url}" style="{_LINK_CSS}">{title}</a>
  </div>
  <div style="{_CARD_META_CSS}">{_esc(institution)} &mdash; {location}</div>
  <div style="{_CARD_META_CSS}">Deadline: {deadline_display} {countdown}</div>
  <div style="{_CARD_META_CSS}">Salary: {salary} &bull; Duration: {duration} &bull; Start: {start_date}</div>
  <div style="margin:8px 0;">
    {_tier_badge(tier)} {eligibility} {_score_bar(grant.get('relevance_score'))}
  </div>
  <div style="font-size:14px;color:#c0c0d8;margin:8px 0;font-style:italic;">{summary}</div>
  <div style="font-size:12px;color:#a0a0b8;margin:4px 0;">{elig_reason}</div>
  <div style="font-size:12px;color:#a0a0b8;margin:4px 0;">Timeline: {timeline_fit} &mdash; {timeline_note}</div>
  {pros_html}{cons_html}{steps_html}
</div>"""


def _render_compact_card(grant: dict) -> str:
    """Compact card for Tier 2 in weekly digest."""
    tier = grant.get("tier", 0)
    border = _TIER_COLOURS.get(tier, "#6a6a8a")
    card_css = _CARD_CSS.replace("{border_color}", border)

    title = _esc(grant.get("title", "(no title)"))
    url = _esc(grant.get("url", ""))
    institution = _esc(grant.get("institution", ""))
    country = _esc(grant.get("country", ""))
    deadline_display = _esc(grant.get("deadline_display") or grant.get("deadline") or "N/A")
    countdown = _deadline_countdown(grant.get("deadline"))
    summary = _esc(grant.get("one_line_summary", ""))
    eligibility = _eligibility_badge(grant.get("eligibility_verdict"))

    return f"""<div style="{card_css}">
  <div style="{_CARD_TITLE_CSS}">
    <a href="{url}" style="{_LINK_CSS}">{title}</a>
  </div>
  <div style="{_CARD_META_CSS}">{institution} &mdash; {_esc(country)} &bull; Deadline: {deadline_display} {countdown}</div>
  <div style="margin:6px 0;">{eligibility} {_score_bar(grant.get('relevance_score'))}</div>
  <div style="font-size:13px;color:#c0c0d8;font-style:italic;">{summary}</div>
</div>"""


def _render_list_item(grant: dict) -> str:
    """Single list row for Tier 3/4."""
    title = _esc(grant.get("title", "(no title)"))
    url = _esc(grant.get("url", ""))
    institution = _esc(grant.get("institution", ""))
    deadline_display = _esc(grant.get("deadline_display") or grant.get("deadline") or "N/A")
    countdown = _deadline_countdown(grant.get("deadline"))
    eligibility = _eligibility_badge(grant.get("eligibility_verdict"))

    return (
        f'<div style="{_LIST_ITEM_CSS}">'
        f'<a href="{url}" style="{_LINK_CSS};font-weight:600;">{title}</a>'
        f'<span style="color:#a0a0b8;font-size:12px;"> &mdash; {institution}</span>'
        f'<br>'
        f'<span style="font-size:12px;color:#a0a0b8;">Deadline: {deadline_display} {countdown}</span> '
        f'{eligibility}'
        f'</div>'
    )


# -------------------------------------------------------------------
# Full email builders
# -------------------------------------------------------------------

def _build_tier1_html(grant: dict, config: dict) -> str:
    """Build full HTML body for a Tier 1 alert."""
    dashboard_url = _esc(
        config.get("researcher", {}).get("website", "#")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="{_BODY_CSS}">
<div style="{_CONTAINER_CSS}">
  <div style="{_HEADER_CSS}">Grant Radar &mdash; Tier 1 Alert</div>
  <div style="{_SUMMARY_CSS}">
    A high-priority opportunity matching your Cardiff + Welsh Economic Geography profile has been found.
    Even if eligibility needs checking, this is worth immediate attention.
  </div>
  {_render_full_card(grant)}
  <div style="text-align:center;margin:20px 0;">
    <a href="{dashboard_url}" style="{_LINK_CSS};font-size:14px;font-weight:600;">
      View full dashboard &rarr;
    </a>
  </div>
  <div style="{_FOOTER_CSS}">
    Grant Radar &bull; Automated academic opportunity monitoring<br>
    Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
  </div>
</div>
</body>
</html>"""


def _build_weekly_html(
    grants_by_tier: dict[int, list[dict]],
    strategic_notes: dict,
    config: dict,
    source_status: list[dict] | None = None,
) -> str:
    """Build full HTML body for the weekly digest."""
    dashboard_url = _esc(
        config.get("researcher", {}).get("website", "#")
    )

    # Counts
    total = sum(len(gs) for gs in grants_by_tier.values())
    tier_counts = []
    for t in [1, 2, 3, 4, 0]:
        n = len(grants_by_tier.get(t, []))
        if n:
            tier_counts.append(f"{n} {_TIER_NAMES.get(t, f'Tier {t}')}")
    counts_str = ", ".join(tier_counts) if tier_counts else "none"

    today = datetime.now(timezone.utc)
    date_range = today.strftime("%d %b %Y")

    sections = []

    # --- Summary box ---
    sections.append(
        f'<div style="{_SUMMARY_CSS}">'
        f"<strong>{total} new opportunities</strong> this week ({counts_str})"
        f"</div>"
    )

    # --- Tier 1 ---
    tier1 = grants_by_tier.get(1, [])
    if tier1:
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Tier 1 -- Cardiff + Welsh ({len(tier1)})</div>')
        for g in tier1:
            sections.append(_render_full_card(g))

    # --- Tier 2 ---
    tier2 = grants_by_tier.get(2, [])
    if tier2:
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Tier 2 -- Strong Alternatives ({len(tier2)})</div>')
        for g in tier2:
            sections.append(_render_compact_card(g))

    # --- Tier 3 ---
    tier3 = grants_by_tier.get(3, [])
    if tier3:
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Tier 3 -- Good European ({len(tier3)})</div>')
        for g in tier3:
            sections.append(_render_list_item(g))

    # --- Tier 4 ---
    tier4 = grants_by_tier.get(4, [])
    if tier4:
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Tier 4 -- Indonesia ({len(tier4)})</div>')
        for g in tier4:
            sections.append(_render_list_item(g))

    # --- Tier 0 ---
    tier0 = grants_by_tier.get(0, [])
    if tier0:
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Other Opportunities ({len(tier0)})</div>')
        for g in tier0:
            sections.append(_render_list_item(g))

    # --- Strategic notes ---
    if strategic_notes and not strategic_notes.get("error"):
        sections.append(f'<div style="{_SECTION_TITLE_CSS}">Strategic Intelligence</div>')
        strat_parts = []

        deadlines = strategic_notes.get("upcoming_deadlines", [])
        if deadlines:
            strat_parts.append('<div style="margin-bottom:10px;"><strong style="color:#f9a825;">Upcoming Deadlines</strong></div>')
            for dl in deadlines:
                prog = _esc(dl.get("programme", ""))
                date = _esc(dl.get("deadline", ""))
                action = _esc(dl.get("action_needed", ""))
                priority = dl.get("priority", "medium")
                pri_colour = {"high": "#ef5350", "medium": "#f9a825", "low": "#66bb6a"}.get(priority, "#a0a0b8")
                strat_parts.append(
                    f'<div style="{_LIST_ITEM_CSS}">'
                    f'<span style="color:{pri_colour};font-weight:600;">[{_esc(priority).upper()}]</span> '
                    f'<strong>{prog}</strong> &mdash; {date}<br>'
                    f'<span style="font-size:12px;color:#a0a0b8;">{action}</span>'
                    f'</div>'
                )

        recs = strategic_notes.get("strategy_recommendations", [])
        if recs:
            strat_parts.append('<div style="margin:12px 0 6px 0;"><strong style="color:#4fc3f7;">Recommendations</strong></div>')
            for r in recs:
                strat_parts.append(f'<div style="{_STEPS_CSS}">&rarr; {_esc(r)}</div>')

        cycles = strategic_notes.get("grant_cycles", [])
        if cycles:
            strat_parts.append('<div style="margin:12px 0 6px 0;"><strong style="color:#66bb6a;">Grant Cycles</strong></div>')
            for c in cycles:
                prog = _esc(c.get("programme", ""))
                funder = _esc(c.get("funder", ""))
                next_dl = _esc(c.get("next_deadline") or c.get("typical_deadline", "TBD"))
                match = c.get("eligibility_match", "check")
                badge = _eligibility_badge(match)
                strat_parts.append(
                    f'<div style="{_LIST_ITEM_CSS}">'
                    f'<strong>{prog}</strong> ({funder}) &mdash; next deadline: {next_dl} {badge}'
                    f'</div>'
                )

        emerging = strategic_notes.get("emerging_opportunities", [])
        if emerging:
            strat_parts.append('<div style="margin:12px 0 6px 0;"><strong style="color:#e94560;">Emerging</strong></div>')
            for e in emerging:
                strat_parts.append(
                    f'<div style="{_LIST_ITEM_CSS}">{_esc(e.get("opportunity", ""))}'
                    f'<br><span style="font-size:12px;color:#a0a0b8;">{_esc(e.get("relevance", ""))}</span></div>'
                )

        sections.append(
            f'<div style="background-color:#0f3460;border-radius:8px;padding:14px;margin-bottom:16px;">'
            + "\n".join(strat_parts)
            + "</div>"
        )

    # --- Source status ---
    if source_status:
        failures = [s for s in source_status if s.get("status") == "error"]
        if failures:
            sections.append(f'<div style="{_SECTION_TITLE_CSS}">Source Warnings</div>')
            for s in failures:
                name = _esc(s.get("source_name", "unknown"))
                err = _esc(s.get("error", "unknown error"))
                sections.append(
                    f'<div style="font-size:12px;color:#ef5350;margin:4px 0;">'
                    f'&#9888; {name}: {err}</div>'
                )

    body_content = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="{_BODY_CSS}">
<div style="{_CONTAINER_CSS}">
  <div style="{_HEADER_CSS}">Grant Radar &mdash; Weekly Digest</div>
  {body_content}
  <div style="text-align:center;margin:20px 0;">
    <a href="{dashboard_url}" style="{_LINK_CSS};font-size:14px;font-weight:600;">
      View full dashboard &rarr;
    </a>
  </div>
  <div style="{_FOOTER_CSS}">
    Grant Radar &bull; Automated academic opportunity monitoring<br>
    Generated {today.strftime('%Y-%m-%d %H:%M UTC')}
  </div>
</div>
</body>
</html>"""


# -------------------------------------------------------------------
# SMTP sender
# -------------------------------------------------------------------

def _send_email(subject: str, html_body: str, config: dict) -> bool:
    """Send an HTML email via SMTP.

    Credentials come from environment variables (never hardcoded):
      EMAIL_ADDRESS, EMAIL_PASSWORD, SMTP_SERVER, SMTP_PORT

    Falls back to config["email"] fields if env vars are not set.

    Returns True on success, False on failure.
    """
    email_cfg = config.get("email", {})

    sender = os.environ.get("EMAIL_ADDRESS") or email_cfg.get("sender_email", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    if not password:
        pw_env = email_cfg.get("sender_password_env", "EMAIL_PASSWORD")
        password = os.environ.get(pw_env, "")

    smtp_server = os.environ.get("SMTP_SERVER") or email_cfg.get("smtp_server", "") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT", 0) or email_cfg.get("smtp_port", 587))
    recipient = email_cfg.get("recipient_email", "")

    if not sender:
        logger.error("No sender email configured (set EMAIL_ADDRESS env var).")
        return False
    if not password:
        logger.error("No email password configured (set EMAIL_PASSWORD env var).")
        return False
    if not recipient:
        logger.error("No recipient email in config.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Grant Radar <{sender}>"
    msg["To"] = recipient

    # Plain-text fallback
    plain = (
        f"{subject}\n\n"
        "This email is best viewed in an HTML-capable email client.\n"
        "Visit your Grant Radar dashboard for full details."
    )
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        logger.info("Connecting to %s:%d...", smtp_server, smtp_port)
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        logger.info("Email sent to %s: %s", recipient, subject)
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed: %s", exc)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
        return False
    except OSError as exc:
        logger.error("Network error sending email: %s", exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error sending email: %s", exc, exc_info=True)
        return False


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------

def send_tier1_alert(grant: dict, config: dict) -> bool:
    """Send an immediate email alert for a Tier 1 opportunity.

    Sends even if eligibility is flagged as "check" -- includes the
    flag in the email so the researcher can decide.

    Args:
        grant:  A single analysed grant dict (tier == 1).
        config: The full config dict from config.json.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    title = grant.get("title", "(untitled)")
    subject = f"\U0001f514 Grant Radar: Tier 1 -- {title}"

    logger.info("Sending Tier 1 alert: %s", title)
    html = _build_tier1_html(grant, config)
    return _send_email(subject, html, config)


def send_weekly_digest(
    grants_by_tier: dict[int, list[dict]],
    strategic_notes: dict,
    config: dict,
    source_status: list[dict] | None = None,
) -> bool:
    """Send the weekly digest email after the Opus run.

    Args:
        grants_by_tier: Dict mapping tier numbers to lists of grants.
        strategic_notes: Opus-generated strategic intelligence dict.
        config:          The full config dict from config.json.
        source_status:   Optional list of run_status entries for
                         source failure warnings.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    total = sum(len(gs) for gs in grants_by_tier.values())
    today = datetime.now(timezone.utc)
    date_str = today.strftime("%d %b %Y")
    subject = f"\U0001f4cb Grant Radar Weekly Digest -- {date_str}"

    logger.info("Sending weekly digest: %d grants across %d tiers.",
                total, len(grants_by_tier))
    html = _build_weekly_html(grants_by_tier, strategic_notes, config, source_status)
    return _send_email(subject, html, config)


# -------------------------------------------------------------------
# CLI testing
# -------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = Path(__file__).resolve().parent / "config.json"
    with open(config_path, "r") as f:
        config = json.load(f)

    # Synthetic test data
    sample_tier1 = {
        "id": "test_t1",
        "title": "Postdoctoral Research Associate in Economic Geography",
        "institution": "Cardiff University",
        "country": "United Kingdom",
        "city": "Cardiff",
        "url": "https://www.jobs.ac.uk/job/example/postdoc-econ-geog",
        "date_posted": "2026-02-15",
        "deadline": "2026-04-30",
        "deadline_display": "30 April 2026",
        "salary": "GBP 35,326 - 40,927",
        "salary_eur": "EUR 41,000 - 47,500",
        "duration": "2 years",
        "start_date": "2027-01-01",
        "full_time": True,
        "funding_covers": "Salary, travel budget, conference attendance",
        "career_stage": "PhD + 0-3 years",
        "nationality_requirement": "none stated",
        "degree_requirement": "PhD in geography, economics, or related field",
        "language_requirement": "English",
        "eligibility_verdict": "eligible",
        "eligibility_reason": "No nationality or UK degree requirement. PhD expected by start date.",
        "eligibility_timeline_note": "PhD defence Q1 2027, position starts Jan 2027 -- tight but feasible.",
        "research_theme": "Regional economic resilience and competitiveness",
        "theme_flexibility": "flexible",
        "methods_relevance": "Strong -- spatial econometrics directly applicable",
        "policy_relevant": True,
        "funding_portable": False,
        "portability_note": None,
        "tier": 1,
        "tier_reason": "Cardiff School of Geography and Planning, direct connection to Prof Huggins",
        "relevance_score": 95,
        "timeline_fit": "ideal",
        "timeline_note": "January 2027 start is ideal given PhD defence timeline",
        "pros": [
            "Direct connection to Prof Robert Huggins",
            "Spatial econometrics core skill match",
            "Policy-relevant economic geography",
            "No UK degree or nationality requirement",
        ],
        "cons": [
            "Tight timing with PhD defence",
            "Fixed-term (2 years)",
        ],
        "next_steps": [
            "Contact Prof Huggins to discuss application",
            "Prepare research statement on regional economic resilience",
            "Gather two academic references",
        ],
        "competition_level": "medium",
        "one_line_summary": "Ideal Tier 1 match at Cardiff with Huggins group, spatial econometrics focus.",
        "source_name": "Jobs.ac.uk - Geography",
        "source_type": "web_scrape",
        "analysed_at": "2026-03-01T12:00:00+00:00",
    }

    sample_tier2 = {
        "id": "test_t2",
        "title": "Assistant Professor in Economic Geography",
        "institution": "University of Groningen",
        "country": "Netherlands",
        "city": "Groningen",
        "url": "https://www.academictransfer.com/en/jobs/example",
        "deadline": "2026-05-15",
        "deadline_display": "15 May 2026",
        "eligibility_verdict": "check",
        "eligibility_reason": "Check NWO nationality requirements for Dutch institutions.",
        "relevance_score": 78,
        "tier": 2,
        "one_line_summary": "Groningen MSc connection, economic geography match, check eligibility.",
        "source_name": "Academic Transfer",
        "source_type": "web_scrape",
    }

    sample_tier3 = {
        "id": "test_t3",
        "title": "Postdoc in Urban Economics",
        "institution": "ETH Zurich",
        "country": "Switzerland",
        "url": "https://example.com/postdoc-urban",
        "deadline": "2026-06-01",
        "deadline_display": "1 June 2026",
        "eligibility_verdict": "eligible",
        "relevance_score": 62,
        "tier": 3,
        "one_line_summary": "Urban economics postdoc, methods overlap, outside core geography.",
        "source_name": "EURAXESS",
        "source_type": "web_scrape",
    }

    sample_tier4 = {
        "id": "test_t4",
        "title": "Peneliti Ekonomi Regional -- BRIN",
        "institution": "BRIN",
        "country": "Indonesia",
        "url": "https://brin.go.id/example",
        "deadline": None,
        "deadline_display": None,
        "eligibility_verdict": "eligible",
        "relevance_score": 55,
        "tier": 4,
        "one_line_summary": "BRIN research position, home-country fallback.",
        "source_name": "Opus Discovery",
        "source_type": "discovery",
    }

    sample_strategic = {
        "generated_at": "2026-03-01T12:00:00+00:00",
        "grant_cycles": [
            {
                "programme": "MSCA Postdoctoral Fellowships",
                "funder": "European Commission",
                "typical_deadline": "September 2026",
                "next_deadline": "2026-09-11",
                "eligibility_match": "eligible",
                "notes": "Portable -- can be hosted at Cardiff.",
            },
            {
                "programme": "NWO Veni",
                "funder": "NWO",
                "typical_deadline": "January 2027",
                "next_deadline": "2027-01-15",
                "eligibility_match": "check",
                "notes": "Dutch host required.  Check nationality rules.",
            },
        ],
        "upcoming_deadlines": [
            {
                "programme": "Leverhulme ECF",
                "deadline": "2026-05-01",
                "action_needed": "Identify Cardiff mentor, draft research proposal",
                "priority": "high",
            },
            {
                "programme": "MSCA-PF 2026 Call",
                "deadline": "2026-09-11",
                "action_needed": "Start Part B proposal draft, contact Cardiff host",
                "priority": "high",
            },
        ],
        "strategy_recommendations": [
            "Prioritise MSCA-PF with Cardiff as host -- strongest match.",
            "Contact Prof Huggins about supporting a Leverhulme ECF application.",
            "Begin NWO Veni pre-proposal with Groningen as host.",
        ],
        "emerging_opportunities": [
            {
                "opportunity": "UKRI Future Leaders Fellowships Round 10",
                "source": "UKRI website",
                "relevance": "New round expected 2026 -- check eligibility for non-UK nationals.",
            },
        ],
        "cv_gaps_to_address": [
            "Need a first-author journal publication before MSCA deadline.",
        ],
    }

    sample_source_status = [
        {"source_name": "Jobs.ac.uk - Geography", "status": "success", "grants_found": 25, "error": None},
        {"source_name": "ERSA", "status": "error", "grants_found": 0, "error": "Connection timeout after 20s"},
    ]

    mode = sys.argv[1] if len(sys.argv) > 1 else "preview"

    if mode == "preview":
        # Write HTML to files for browser preview
        print("Generating HTML previews...")

        t1_html = _build_tier1_html(sample_tier1, config)
        t1_path = Path(__file__).resolve().parent.parent / "data" / "preview_tier1_alert.html"
        with open(t1_path, "w", encoding="utf-8") as f:
            f.write(t1_html)
        print(f"  Tier 1 alert: {t1_path}")

        grants_by_tier = {
            1: [sample_tier1],
            2: [sample_tier2],
            3: [sample_tier3],
            4: [sample_tier4],
        }
        digest_html = _build_weekly_html(grants_by_tier, sample_strategic, config, sample_source_status)
        digest_path = Path(__file__).resolve().parent.parent / "data" / "preview_weekly_digest.html"
        with open(digest_path, "w", encoding="utf-8") as f:
            f.write(digest_html)
        print(f"  Weekly digest: {digest_path}")
        print("Open these files in a browser to preview.")

    elif mode == "send":
        if not os.environ.get("EMAIL_ADDRESS"):
            print("ERROR: Set EMAIL_ADDRESS and EMAIL_PASSWORD env vars to send.")
            sys.exit(1)
        print("Sending test Tier 1 alert...")
        ok = send_tier1_alert(sample_tier1, config)
        print(f"  Result: {'sent' if ok else 'FAILED'}")

        print("Sending test weekly digest...")
        grants_by_tier = {
            1: [sample_tier1],
            2: [sample_tier2],
            3: [sample_tier3],
            4: [sample_tier4],
        }
        ok = send_weekly_digest(grants_by_tier, sample_strategic, config, sample_source_status)
        print(f"  Result: {'sent' if ok else 'FAILED'}")
    else:
        print(f"Usage: python notifier.py [preview|send]")
