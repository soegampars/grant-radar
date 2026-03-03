# Grant Radar

AI-powered academic grant and postdoc monitoring system. Automatically discovers, analyses, and ranks funding opportunities tailored to a specific researcher's profile — then serves the results on a private dashboard via GitHub Pages.

> **Private repo for personal use.** This system is configured for a single researcher. All configuration, scoring logic, and strategic analysis are tuned to one career profile.

---

## How it works

Grant Radar runs as a three-layer pipeline, automated through GitHub Actions:

1. **Fetch** — Daily, the system pulls new postings from 8 configured sources: RSS feeds (arXiv Economics), API endpoints (EconJobMarket), and web scrapers (Jobs.ac.uk, Academic Transfer, EURAXESS, RSA, ERSA). Raw listings are collected and deduplicated.

2. **Analyse** — Each new listing is evaluated by Claude Sonnet against the researcher's full profile (education, skills, career timeline, geographic preferences). The model assigns a relevance score (0–100), eligibility verdict, tier classification (1–4), and structured pros/cons/next-steps.

3. **Discover** — Weekly, Claude Opus performs a deep web search for opportunities not covered by the standard sources, re-evaluates recent high-potential grants, and generates strategic notes on upcoming cycles, emerging opportunities, and CV gaps.

Results are committed back to the repo as JSON files. GitHub Pages serves the dashboard, which reads the data client-side — no server required.

### Dashboard

The frontend is a static single-page app (vanilla HTML/CSS/JS) with:
- Grant cards sorted by tier priority, with expand/collapse detail panels
- Score rings, eligibility badges, and timeline-fit indicators
- Tier filtering, search, starred grants (localStorage), and archive mode
- Strategic Notes tab with Opus-generated career guidance
- Source health monitoring table
- Dark theme, mobile-responsive

### Email notifications

After each pipeline run, the system sends an email summary with new findings. Weekly runs include a full digest with strategic recommendations.

---

## Repository structure

```
grant-radar/
  index.html            # Dashboard (GitHub Pages entry point)
  help.html             # User guide
  assets/
    style.css           # Dashboard styles (dark theme)
    app.js              # Dashboard logic (vanilla JS)
  data/
    grants.json         # All analysed grants (updated by pipeline)
    run_status.json     # Source health status
    strategic_notes.json # Opus weekly strategic analysis
  backend/
    config.json         # Researcher profile, sources, model settings
    main.py             # Pipeline orchestrator
    analyser.py         # Sonnet grant analysis
    discovery.py        # Opus deep search + re-evaluation
    notifier.py         # Email notifications
    utils.py            # Shared utilities
    fetchers/
      rss_fetcher.py    # RSS/Atom feed parser
      ejm_fetcher.py    # EconJobMarket API client
      web_scraper.py    # Configurable web scraper
  .github/workflows/
    daily.yml           # Runs daily at 06:00 UTC
    weekly.yml          # Runs Sundays at 08:00 UTC
```

---

## Setup

### Prerequisites
- A GitHub account
- An [Anthropic API key](https://console.anthropic.com/)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) for notifications

### Quick start

1. Create a **private** repository on GitHub and push this code
2. Go to **Settings > Pages** and set source to `Deploy from a branch`, branch `main`, folder `/ (root)`
3. Add three **repository secrets** under Settings > Secrets and variables > Actions:
   - `ANTHROPIC_API_KEY` — your Claude API key
   - `EMAIL_ADDRESS` — your Gmail address
   - `EMAIL_PASSWORD` — your Gmail App Password
4. Under **Settings > Actions > General**, set Workflow permissions to **Read and write permissions**
5. Trigger the first run manually from the **Actions** tab

For detailed instructions on customising the researcher profile, adding sources, adjusting tier logic, changing models, and troubleshooting, see the **[User Guide](help.html)** (also accessible from the dashboard).

---

## Cost estimate

The system uses two Claude models via the Anthropic API:

| Component | Model | Frequency | Est. monthly cost |
|---|---|---|---|
| Grant analysis | Claude Sonnet | Daily | ~$9.00 |
| Deep search + re-evaluation | Claude Opus | Weekly | ~$4.50 |
| **Total** | | | **~$13.50/month** |

Actual costs depend on the number of new listings found each day. The estimate assumes ~10–15 new listings per daily run. You can monitor usage on the [Anthropic billing dashboard](https://console.anthropic.com/settings/billing).

---

## License

Private project. Not intended for redistribution.
