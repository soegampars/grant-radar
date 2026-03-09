/* ================================================================
   Grant Radar — Frontend Application
   Vanilla JS, no frameworks, no build tools.
   ================================================================ */

(function () {
  "use strict";

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  let allGrants   = [];
  let runStatus   = [];
  let strategic   = {};
  let starred     = loadStarred();
  let ratings     = loadRatings();

  // Current filter/sort state
  let filters = {
    tier:     "all",
    eligible: false,
    newOnly:  false,
    starred:  false,
    search:   "",
    sort:     "tier",
    archive:  false,
    hidePhd:  true,
  };

  // Country flag map (subset of common countries)
  const FLAGS = {
    "united kingdom": "\uD83C\uDDEC\uD83C\uDDE7",
    "uk":  "\uD83C\uDDEC\uD83C\uDDE7",
    "netherlands": "\uD83C\uDDF3\uD83C\uDDF1",
    "italy": "\uD83C\uDDEE\uD83C\uDDF9",
    "germany": "\uD83C\uDDE9\uD83C\uDDEA",
    "france": "\uD83C\uDDEB\uD83C\uDDF7",
    "spain": "\uD83C\uDDEA\uD83C\uDDF8",
    "belgium": "\uD83C\uDDE7\uD83C\uDDEA",
    "austria": "\uD83C\uDDE6\uD83C\uDDF9",
    "switzerland": "\uD83C\uDDE8\uD83C\uDDED",
    "sweden": "\uD83C\uDDF8\uD83C\uDDEA",
    "norway": "\uD83C\uDDF3\uD83C\uDDF4",
    "denmark": "\uD83C\uDDE9\uD83C\uDDF0",
    "finland": "\uD83C\uDDEB\uD83C\uDDEE",
    "ireland": "\uD83C\uDDEE\uD83C\uDDEA",
    "portugal": "\uD83C\uDDF5\uD83C\uDDF9",
    "poland": "\uD83C\uDDF5\uD83C\uDDF1",
    "czech republic": "\uD83C\uDDE8\uD83C\uDDFF",
    "indonesia": "\uD83C\uDDEE\uD83C\uDDE9",
    "usa": "\uD83C\uDDFA\uD83C\uDDF8",
    "united states": "\uD83C\uDDFA\uD83C\uDDF8",
    "canada": "\uD83C\uDDE8\uD83C\uDDE6",
    "australia": "\uD83C\uDDE6\uD83C\uDDFA",
    "japan": "\uD83C\uDDEF\uD83C\uDDF5",
    "south korea": "\uD83C\uDDF0\uD83C\uDDF7",
    "china": "\uD83C\uDDE8\uD83C\uDDF3",
    "wales": "\uD83C\uDFF4\uDB40\uDC67\uDB40\uDC62\uDB40\uDC77\uDB40\uDC6C\uDB40\uDC73\uDB40\uDC7F",
  };

  // ------------------------------------------------------------------
  // Initialise
  // ------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    bindEvents();
    await Promise.all([
      fetchJSON("data/grants.json").then(d => { allGrants = _dedup(d || []); }),
      fetchJSON("data/run_status.json").then(d => { runStatus = d || []; }),
      fetchJSON("data/strategic_notes.json").then(d => { strategic = d || {}; }),
    ]);
    render();
  }

  async function fetchJSON(url) {
    try {
      const sep = url.includes("?") ? "&" : "?";
      const res = await fetch(url + sep + "_v=" + Date.now());
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      console.warn("Failed to fetch " + url, e);
      return null;
    }
  }

  // ------------------------------------------------------------------
  // Event binding
  // ------------------------------------------------------------------
  function bindEvents() {
    // Tab navigation
    document.querySelectorAll(".nav-btn[data-tab]").forEach(btn => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // Tier filter buttons
    document.getElementById("tierBtns").addEventListener("click", e => {
      const btn = e.target.closest(".tier-btn");
      if (!btn) return;
      document.querySelectorAll(".tier-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      filters.tier = btn.dataset.tier;
      renderCards();
    });

    // Checkbox filters
    document.getElementById("filterEligible").addEventListener("change", e => {
      filters.eligible = e.target.checked;
      renderCards();
    });
    document.getElementById("filterNew").addEventListener("change", e => {
      filters.newOnly = e.target.checked;
      renderCards();
    });
    document.getElementById("filterStarred").addEventListener("change", e => {
      filters.starred = e.target.checked;
      renderCards();
    });

    // Sort
    document.getElementById("sortSelect").addEventListener("change", e => {
      filters.sort = e.target.value;
      renderCards();
    });

    // Search (debounced)
    let searchTimer;
    document.getElementById("searchBox").addEventListener("input", e => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        filters.search = e.target.value.trim().toLowerCase();
        renderCards();
      }, 200);
    });

    // Archive toggle
    document.getElementById("showArchive").addEventListener("change", e => {
      filters.archive = e.target.checked;
      renderCards();
    });

    // Hide PhD/pre-doc toggle
    document.getElementById("hidePhd").addEventListener("change", e => {
      filters.hidePhd = e.target.checked;
      renderCards();
    });

    // Stat card tier clicks — act as filter buttons
    document.getElementById("statsBar").addEventListener("click", e => {
      const card = e.target.closest("[data-filter-tier]");
      if (card) {
        const tier = card.dataset.filterTier;
        // Toggle: if already filtering this tier, reset to all
        const newTier = filters.tier === tier ? "all" : tier;
        filters.tier = newTier;
        // Sync the tier button bar
        document.querySelectorAll(".tier-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.tier === newTier)
        );
        switchTab("dashboard");
        renderCards();
        // Scroll to cards
        document.getElementById("cardsContainer").scrollIntoView({ behavior: "smooth" });
        return;
      }
    });

    // Next deadline card click — scroll to the soonest-deadline card
    document.getElementById("statDeadlineCard").addEventListener("click", () => {
      const today = todayISO();
      const upcoming = allGrants
        .filter(g => !g.expired && g.deadline && g.deadline >= today)
        .sort((a, b) => a.deadline.localeCompare(b.deadline));
      if (!upcoming.length) return;
      const targetId = upcoming[0].id;
      // Ensure we're on the dashboard with no conflicting filters
      filters.tier = "all";
      filters.eligible = false;
      filters.newOnly = false;
      filters.starred = false;
      filters.search = "";
      document.querySelectorAll(".tier-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.tier === "all")
      );
      switchTab("dashboard");
      renderCards();
      // Find and scroll to + expand the card
      const cardEl = document.querySelector(`[data-grant-id="${targetId}"]`);
      if (cardEl) {
        cardEl.classList.add("expanded");
        cardEl.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });

    // Feedback export / clear / reminder
    const exportBtn = document.getElementById("exportFeedbackBtn");
    if (exportBtn) exportBtn.addEventListener("click", exportFeedbackLog);

    const clearBtn = document.getElementById("clearFeedbackBtn");
    if (clearBtn) clearBtn.addEventListener("click", () => {
      if (confirm("Clear all ratings? This cannot be undone.")) {
        ratings = {};
        saveRatings();
        updateFeedbackSettings();
        renderCards();
      }
    });

    const reminderExportBtn = document.getElementById("reminderExportBtn");
    if (reminderExportBtn) reminderExportBtn.addEventListener("click", () => {
      exportFeedbackLog();
      document.getElementById("feedbackReminder").classList.add("hidden");
    });

    const reminderDismissBtn = document.getElementById("reminderDismissBtn");
    if (reminderDismissBtn) reminderDismissBtn.addEventListener("click", () => {
      localStorage.setItem("grantRadar_feedbackReminderDismissed", new Date().toISOString());
      document.getElementById("feedbackReminder").classList.add("hidden");
    });
  }

  // ------------------------------------------------------------------
  // Tab switching
  // ------------------------------------------------------------------
  function switchTab(tab) {
    document.querySelectorAll(".nav-btn[data-tab]").forEach(b => {
      b.setAttribute("aria-current", b.dataset.tab === tab ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));

    const map = {
      dashboard: "panelDashboard",
      strategic: "panelStrategic",
      sources:   "panelSources",
      settings:  "panelSettings",
    };
    const panel = document.getElementById(map[tab]);
    if (panel) panel.classList.add("active");

    // Show/hide filter bar
    document.getElementById("filterBar").style.display = tab === "dashboard" ? "" : "none";
  }

  // ------------------------------------------------------------------
  // Render all
  // ------------------------------------------------------------------
  function render() {
    renderLastUpdated();
    renderStats();
    renderTimeline();
    renderCards();
    renderStrategic();
    renderSources();
    updateFeedbackSettings();
    checkFeedbackReminder();
  }

  // ------------------------------------------------------------------
  // Last updated
  // ------------------------------------------------------------------
  function renderLastUpdated() {
    const daily = runStatus.find(s => s.source_name === "daily_pipeline");
    const weekly = runStatus.find(s => s.source_name === "weekly_pipeline");
    const latest = weekly && weekly.last_checked > (daily ? daily.last_checked : "")
      ? weekly : daily;
    const el = document.getElementById("lastUpdated");
    if (latest && latest.last_checked) {
      el.textContent = "Updated " + timeAgo(latest.last_checked);
    } else if (allGrants.length === 0) {
      el.textContent = "No data yet - run the pipeline first";
    } else {
      el.textContent = "";
    }
  }

  // ------------------------------------------------------------------
  // Stats bar
  // ------------------------------------------------------------------
  function renderStats() {
    // Use the same base filter as the card view (minus tier filter)
    // so stats reflect what the user actually sees
    const savedTier = filters.tier;
    filters.tier = "all";
    const visible = filterGrants(allGrants);
    filters.tier = savedTier;

    const cutoff = daysAgoISO(7);
    const newThisWeek = visible.filter(g => (g.date_found || "") >= cutoff);
    const byTier = groupByTier(visible);

    document.getElementById("statTotal").textContent = visible.length;
    document.getElementById("statNew").textContent = newThisWeek.length;
    document.getElementById("statT1").textContent = (byTier[1] || []).length;
    document.getElementById("statT2").textContent = (byTier[2] || []).length;
    document.getElementById("statT3").textContent = (byTier[3] || []).length;
    document.getElementById("statT4").textContent = (byTier[4] || []).length;

    // Eligibility
    const eligible = visible.filter(g => g.eligibility_verdict === "eligible").length;
    const check    = visible.filter(g => g.eligibility_verdict === "check").length;
    document.getElementById("statEligible").textContent = eligible;
    document.getElementById("statCheck").textContent = check;

    // Next deadline
    const today = todayISO();
    const upcoming = visible
      .filter(g => g.deadline && g.deadline >= today)
      .sort((a, b) => a.deadline.localeCompare(b.deadline));
    if (upcoming.length > 0) {
      const days = daysBetween(today, upcoming[0].deadline);
      document.getElementById("statDeadline").textContent =
        days === 0 ? "Today!" : days + "d";
    } else {
      document.getElementById("statDeadline").textContent = "--";
    }
  }

  // ------------------------------------------------------------------
  // Timeline
  // ------------------------------------------------------------------
  function renderTimeline() {
    const track = document.getElementById("timelineTrack");
    track.innerHTML = "";

    // Range: today - 1 month  to  2027-12-31
    const rangeStart = new Date();
    rangeStart.setMonth(rangeStart.getMonth() - 1);
    const rangeEnd = new Date("2027-12-31");
    const totalMs = rangeEnd - rangeStart;
    const pct = d => {
      const ms = new Date(d) - rangeStart;
      return Math.min(Math.max((ms / totalMs) * 96 + 2, 2), 98);
    };

    // Axis line
    const axis = document.createElement("div");
    axis.className = "tl-axis";
    track.appendChild(axis);

    // Milestones
    const milestones = [
      { date: "2026-10-01", label: "PhD Submit" },
      { date: "2027-03-01", label: "Defence" },
      { date: "2026-10-01", label: "PNRR Expiry" },
    ];
    // Deduplicate same-date milestones
    const msByDate = {};
    milestones.forEach(m => {
      if (!msByDate[m.date]) msByDate[m.date] = [];
      msByDate[m.date].push(m.label);
    });
    Object.entries(msByDate).forEach(([date, labels]) => {
      const x = pct(date);
      const ms = document.createElement("div");
      ms.className = "tl-milestone";
      ms.style.left = x + "%";
      ms.style.background = "var(--purple)";
      const lbl = document.createElement("span");
      lbl.className = "tl-ml-label";
      lbl.textContent = labels.join(" / ");
      ms.appendChild(lbl);
      track.appendChild(ms);
    });

    // Today marker
    const todayX = pct(new Date().toISOString().slice(0, 10));
    const todayEl = document.createElement("div");
    todayEl.className = "tl-today";
    todayEl.style.left = todayX + "%";
    const todayLbl = document.createElement("span");
    todayLbl.className = "tl-today-label";
    todayLbl.textContent = "Today";
    todayEl.appendChild(todayLbl);
    track.appendChild(todayEl);

    // Year labels
    ["2026-01-01", "2026-07-01", "2027-01-01", "2027-07-01"].forEach(d => {
      const x = pct(d);
      if (x < 3 || x > 97) return;
      const lbl = document.createElement("span");
      lbl.className = "tl-label";
      lbl.style.left = x + "%";
      lbl.textContent = d.slice(0, 7);
      track.appendChild(lbl);
    });

    // Grant dots (only those with start_date)
    allGrants
      .filter(g => g.start_date && !g.expired)
      .forEach(g => {
        const x = pct(g.start_date);
        const dot = document.createElement("div");
        dot.className = "tl-dot tier-" + (g.tier || 0);
        dot.style.left = x + "%";
        dot.title = (g.title || "Grant") + " - Start: " + g.start_date;
        track.appendChild(dot);
      });
  }

  // ------------------------------------------------------------------
  // Grant cards
  // ------------------------------------------------------------------
  function renderCards() {
    const container = document.getElementById("cardsContainer");
    const loadingMsg = document.getElementById("loadingMsg");
    const emptyMsg = document.getElementById("emptyMsg");

    loadingMsg.classList.add("hidden");

    // Apply filters
    let grants = filterGrants(allGrants);

    // Sort
    grants = sortGrants(grants, filters.sort);

    // Render
    // Remove old cards (keep loading/empty messages)
    container.querySelectorAll(".grant-card").forEach(c => c.remove());

    if (grants.length === 0) {
      emptyMsg.classList.remove("hidden");
      return;
    }
    emptyMsg.classList.add("hidden");

    const frag = document.createDocumentFragment();
    grants.forEach(g => frag.appendChild(createCard(g)));
    container.appendChild(frag);
  }

  const _PHD_PATTERNS = [
    "phd position", "phd student", "phd candidate", "phd scholarship",
    "phd fellowship", "phd researcher", "phd opportunity", "phd studentship",
    "doctoral position", "doctoral student", "doctoral candidate",
    "doctoral researcher", "doctoral scholarship", "doctoral fellowship",
    "doctorate position", "ph.d. student", "ph.d. position",
    "fully funded phd", "funded phd", "pre-doctoral", "predoctoral",
  ];
  function _isPhd(g) {
    if ((g.career_stage || "").toLowerCase() === "phd") return true;
    const t = (g.title || "").toLowerCase();
    return _PHD_PATTERNS.some(p => t.includes(p));
  }

  function filterGrants(grants) {
    const cutoff = daysAgoISO(7);
    const today = todayISO();

    return grants.filter(g => {
      // Hide PhD / pre-doctoral (on by default)
      if (filters.hidePhd && _isPhd(g)) return false;

      // Archive filter
      if (!filters.archive && g.expired) return false;
      if (filters.archive && !g.expired) return false; // archive mode shows only expired

      // Tier
      if (filters.tier !== "all" && String(g.tier) !== filters.tier) return false;

      // Eligible only
      if (filters.eligible && g.eligibility_verdict !== "eligible") return false;

      // New this week
      if (filters.newOnly && (g.date_found || "") < cutoff) return false;

      // Starred
      if (filters.starred && !starred[g.id]) return false;

      // Search
      if (filters.search) {
        const hay = [
          g.title, g.institution, g.country, g.city,
          g.one_line_summary, g.research_theme,
          g.source_name, g.eligibility_reason,
        ].filter(Boolean).join(" ").toLowerCase();
        if (!hay.includes(filters.search)) return false;
      }

      return true;
    });
  }

  function sortGrants(grants, sortKey) {
    const copy = [...grants];
    switch (sortKey) {
      case "tier":
        copy.sort((a, b) => {
          const ta = a.tier || 99, tb = b.tier || 99;
          if (ta !== tb) return ta - tb;
          return (b.relevance_score || 0) - (a.relevance_score || 0);
        });
        break;
      case "deadline":
        copy.sort((a, b) => {
          const da = a.deadline || "9999-12-31", db = b.deadline || "9999-12-31";
          return da.localeCompare(db);
        });
        break;
      case "date_found":
        copy.sort((a, b) => {
          const da = a.date_found || "0000-00-00", db = b.date_found || "0000-00-00";
          return db.localeCompare(da);  // newest first
        });
        break;
      case "score":
        copy.sort((a, b) => (b.relevance_score || 0) - (a.relevance_score || 0));
        break;
    }
    return copy;
  }

  function createCard(g) {
    const card = document.createElement("div");
    const tier = g.tier || 0;
    card.className = "grant-card tier-" + tier + (g.expired ? " expired" : "");
    card.dataset.grantId = g.id || "";

    const isStarred = !!starred[g.id];
    const curRating = ratings[g.id];
    const isUp = !!(curRating && curRating.rating === "up");
    const isDown = !!(curRating && curRating.rating === "down");
    const ratedClass = isUp ? " rated-up" : isDown ? " rated-down" : "";

    card.className += ratedClass;

    card.innerHTML = `
      <div class="card-header">
        <span class="card-tier-badge badge-t${tier}">Tier ${tier === 0 ? "?" : tier}</span>
        <div class="card-main">
          <div class="card-title">
            ${g.url ? `<a href="${esc(g.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(g.title || "Untitled")}</a>` : esc(g.title || "Untitled")}
          </div>
          <div class="card-meta">
            ${g.id ? `<span class="card-meta-item card-id-badge" title="Grant ID: ${esc(g.id)}">#${esc(g.id.slice(0, 6))}</span>` : ""}
            ${g.institution ? `<span class="card-meta-item">${countryFlag(g.country)}${esc(g.institution)}</span>` : ""}
            ${g.country ? `<span class="card-meta-item">${esc(g.country)}</span>` : ""}
            ${renderDeadline(g)}
            ${g.salary ? `<span class="card-meta-item">${esc(g.salary)}</span>` : ""}
            ${g.duration ? `<span class="card-meta-item">${esc(g.duration)}</span>` : ""}
          </div>
          <div class="card-summary">${esc(g.one_line_summary || "")}</div>
        </div>
        <div class="card-right">
          ${renderScoreRing(g.relevance_score)}
          ${renderEligBadge(g.eligibility_verdict)}
          ${renderTimelineBadge(g.timeline_fit)}
          <button class="rate-up-btn ${isUp ? "active" : ""}" onclick="event.stopPropagation()" title="Good match">&#x1F44D;</button>
          <button class="rate-down-btn ${isDown ? "active" : ""}" onclick="event.stopPropagation()" title="Poor match">&#x1F44E;</button>
          <button class="star-btn ${isStarred ? "starred" : ""}" data-star="${g.id || ""}" onclick="event.stopPropagation()" title="Star this grant">${isStarred ? "\u2605" : "\u2606"}</button>
        </div>
      </div>
      <div class="card-detail">
        <div class="card-detail-inner">
          ${renderDetailPros(g)}
          ${renderDetailCons(g)}
          ${renderDetailNext(g)}
          ${renderDetailEligibility(g)}
          ${renderDetailResearch(g)}
          ${renderDetailMeta(g)}
          ${g.reeval_notes ? renderDetailReeval(g) : ""}
        </div>
      </div>
    `;

    // Click to expand
    card.querySelector(".card-header").addEventListener("click", () => {
      card.classList.toggle("expanded");
    });

    // Star button
    card.querySelector(".star-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleStar(g.id, card);
    });

    // Rating buttons
    card.querySelector(".rate-up-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      setRating(g.id, g, "up", card);
    });
    card.querySelector(".rate-down-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      setRating(g.id, g, "down", card);
    });

    return card;
  }

  // ------------------------------------------------------------------
  // Card sub-renderers
  // ------------------------------------------------------------------
  function renderDeadline(g) {
    if (!g.deadline) return "";
    const days = daysBetween(todayISO(), g.deadline);
    let cls = "card-deadline";
    let suffix = "";
    if (days < 0) {
      suffix = " (expired)";
      cls += " urgent";
    } else if (days <= 14) {
      suffix = ` (${days}d left)`;
      cls += " urgent";
    } else if (days <= 30) {
      suffix = ` (${days}d left)`;
      cls += " soon";
    }
    return `<span class="card-meta-item ${cls}">${esc(g.deadline_display || g.deadline)}${suffix}</span>`;
  }

  function renderScoreRing(score) {
    if (score == null || score === 0) return "";
    const s = Math.max(0, Math.min(100, score));
    const r = 16;
    const c = 2 * Math.PI * r;
    const offset = c - (s / 100) * c;
    let color = "var(--green)";
    if (s < 40) color = "var(--red)";
    else if (s < 65) color = "var(--yellow)";
    else if (s < 80) color = "var(--blue)";
    return `<div class="score-ring" title="Score: ${s}/100">
      <svg viewBox="0 0 36 36">
        <circle class="ring-bg" cx="18" cy="18" r="${r}"/>
        <circle class="ring-fg" cx="18" cy="18" r="${r}" stroke="${color}"
          stroke-dasharray="${c}" stroke-dashoffset="${offset}"/>
      </svg>
      <span class="ring-text">${s}</span>
    </div>`;
  }

  function renderEligBadge(v) {
    if (!v) return "";
    const cls = v === "eligible" ? "elig-eligible" :
                v === "check"    ? "elig-check" : "elig-not";
    const label = v === "eligible" ? "Eligible" :
                  v === "check"    ? "Check" :
                  v === "not_eligible" ? "Not Eligible" : v;
    return `<span class="elig-badge ${cls}">${label}</span>`;
  }

  function renderTimelineBadge(v) {
    if (!v) return "";
    const cls = "tl-" + v;
    const label = v.charAt(0).toUpperCase() + v.slice(1);
    return `<span class="timeline-badge ${cls}">${label}</span>`;
  }

  function renderDetailPros(g) {
    const items = g.pros || [];
    if (items.length === 0) return "";
    return `<div class="detail-section detail-pros">
      <h4>Strengths</h4>
      <ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>
    </div>`;
  }

  function renderDetailCons(g) {
    const items = g.cons || [];
    if (items.length === 0) return "";
    return `<div class="detail-section detail-cons">
      <h4>Concerns</h4>
      <ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>
    </div>`;
  }

  function renderDetailNext(g) {
    const items = g.next_steps || [];
    if (items.length === 0) return "";
    return `<div class="detail-section detail-next">
      <h4>Next Steps</h4>
      <ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>
    </div>`;
  }

  function renderDetailEligibility(g) {
    return `<div class="detail-section">
      <h4>Eligibility</h4>
      <dl class="detail-kv">
        <dt>Verdict</dt><dd>${esc(g.eligibility_verdict || "Unknown")}</dd>
      </dl>
      <dl class="detail-kv">
        <dt>Reason</dt><dd>${esc(g.eligibility_reason || "N/A")}</dd>
      </dl>
      ${g.nationality_requirement ? `<dl class="detail-kv"><dt>Nationality</dt><dd>${esc(g.nationality_requirement)}</dd></dl>` : ""}
      ${g.degree_requirement ? `<dl class="detail-kv"><dt>Degree</dt><dd>${esc(g.degree_requirement)}</dd></dl>` : ""}
      ${g.language_requirement ? `<dl class="detail-kv"><dt>Language</dt><dd>${esc(g.language_requirement)}</dd></dl>` : ""}
      ${g.eligibility_timeline_note ? `<dl class="detail-kv"><dt>Timeline Note</dt><dd>${esc(g.eligibility_timeline_note)}</dd></dl>` : ""}
      ${g.eligibility_unclear && g.eligibility_unclear.length ? `<dl class="detail-kv eligibility-unclear"><dt>⚠️ Needs Clarification</dt><dd><ul>${g.eligibility_unclear.map(q => `<li>${esc(q)}</li>`).join("")}</ul></dd></dl>` : ""}
    </div>`;
  }

  function renderDetailResearch(g) {
    return `<div class="detail-section">
      <h4>Research Fit</h4>
      ${g.research_theme ? `<dl class="detail-kv"><dt>Theme</dt><dd>${esc(g.research_theme)}</dd></dl>` : ""}
      ${g.theme_flexibility ? `<dl class="detail-kv"><dt>Flexibility</dt><dd>${esc(g.theme_flexibility)}</dd></dl>` : ""}
      ${g.methods_relevance ? `<dl class="detail-kv"><dt>Methods</dt><dd>${esc(g.methods_relevance)}</dd></dl>` : ""}
      ${g.competition_level ? `<dl class="detail-kv"><dt>Competition</dt><dd>${esc(g.competition_level)}</dd></dl>` : ""}
      ${g.funding_portable ? `<dl class="detail-kv"><dt>Portable</dt><dd>Yes${g.portability_note ? " - " + esc(g.portability_note) : ""}</dd></dl>` : ""}
      ${g.tier_reason ? `<dl class="detail-kv"><dt>Tier Reason</dt><dd>${esc(g.tier_reason)}</dd></dl>` : ""}
      ${g.timeline_note ? `<dl class="detail-kv"><dt>Timeline</dt><dd>${esc(g.timeline_note)}</dd></dl>` : ""}
    </div>`;
  }

  function renderDetailMeta(g) {
    return `<div class="detail-section detail-full-width">
      <h4>Metadata</h4>
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap">
        ${g.source_name ? `<dl class="detail-kv"><dt>Source</dt><dd>${esc(g.source_name)}</dd></dl>` : ""}
        ${g.date_found ? `<dl class="detail-kv"><dt>Found</dt><dd>${esc(g.date_found)}</dd></dl>` : ""}
        ${g.date_posted ? `<dl class="detail-kv"><dt>Posted</dt><dd>${esc(g.date_posted)}</dd></dl>` : ""}
        ${g.career_stage ? `<dl class="detail-kv"><dt>Stage</dt><dd>${esc(g.career_stage)}</dd></dl>` : ""}
        ${g.start_date ? `<dl class="detail-kv"><dt>Start</dt><dd>${esc(g.start_date)}</dd></dl>` : ""}
        ${g.salary_eur ? `<dl class="detail-kv"><dt>Salary (EUR)</dt><dd>${esc(String(g.salary_eur))}</dd></dl>` : ""}
        ${g.full_time != null ? `<dl class="detail-kv"><dt>Full-time</dt><dd>${g.full_time ? "Yes" : "No"}</dd></dl>` : ""}
      </div>
    </div>`;
  }

  function renderDetailReeval(g) {
    return `<div class="detail-section detail-full-width detail-reeval">
      <h4>Opus Re-evaluation</h4>
      <p>${esc(g.reeval_notes)}</p>
    </div>`;
  }

  // ------------------------------------------------------------------
  // Strategic notes
  // ------------------------------------------------------------------
  function renderStrategic() {
    const el = document.getElementById("strategicContent");

    if (!strategic || Object.keys(strategic).length === 0) {
      el.innerHTML = '<p class="section-sub">No strategic notes available yet. Run the weekly pipeline to generate.</p>';
      return;
    }

    el.innerHTML = "";

    const sections = [
      { key: "upcoming_deadlines", title: "Upcoming Deadlines", icon: "", cls: "strat-deadlines" },
      { key: "strategy_recommendations", title: "Recommendations", icon: "", cls: "strat-recommendations" },
      { key: "grant_cycles", title: "Grant Cycles", icon: "", cls: "strat-cycles" },
      { key: "emerging_opportunities", title: "Emerging Opportunities", icon: "", cls: "strat-emerging" },
      { key: "cv_gaps_to_address", title: "CV Gaps to Address", icon: "", cls: "strat-cv" },
    ];

    sections.forEach(sec => {
      const items = strategic[sec.key];
      if (!items || items.length === 0) return;

      const card = document.createElement("div");
      card.className = "strategic-card " + sec.cls;
      card.innerHTML = `<h3>${sec.title}</h3>
        <ul>${items.map(i => `<li>${esc(typeof i === "string" ? i : JSON.stringify(i))}</li>`).join("")}</ul>`;
      el.appendChild(card);
    });

    if (strategic.generated_at) {
      const note = document.createElement("p");
      note.className = "section-sub";
      note.style.marginTop = ".75rem";
      note.textContent = "Generated: " + formatDate(strategic.generated_at);
      el.appendChild(note);
    }
  }

  // ------------------------------------------------------------------
  // Source status table
  // ------------------------------------------------------------------
  function renderSources() {
    const tbody = document.getElementById("sourceTableBody");
    tbody.innerHTML = "";

    // Filter out pipeline entries, show only actual sources
    const sources = runStatus.filter(s =>
      !s.source_name.includes("pipeline")
    );

    if (sources.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No source data yet.</td></tr>';
      return;
    }

    sources.forEach(s => {
      const tr = document.createElement("tr");
      const ok = s.status === "success";
      tr.innerHTML = `
        <td><span class="source-status-dot ${ok ? "ok" : "err"}"></span></td>
        <td>${esc(s.source_name)}</td>
        <td>${s.last_checked ? timeAgo(s.last_checked) : "Never"}</td>
        <td>${s.grants_found != null ? s.grants_found : "--"}</td>
        <td style="color:${s.error ? "var(--red)" : "var(--text-muted)"}">${s.error ? esc(s.error) : "None"}</td>
      `;
      tbody.appendChild(tr);
    });

    // Also show pipeline status at the bottom
    const pipelines = runStatus.filter(s => s.source_name.includes("pipeline"));
    pipelines.forEach(s => {
      const tr = document.createElement("tr");
      tr.style.fontStyle = "italic";
      const ok = s.status === "success";
      tr.innerHTML = `
        <td><span class="source-status-dot ${ok ? "ok" : "err"}"></span></td>
        <td>${esc(s.source_name)}</td>
        <td>${s.last_checked ? timeAgo(s.last_checked) : "Never"}</td>
        <td>${s.grants_found != null ? s.grants_found : "--"}</td>
        <td style="color:${s.error ? "var(--red)" : "var(--text-muted)"}">${s.error ? esc(s.error) : "None"}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // ------------------------------------------------------------------
  // Star management (localStorage)
  // ------------------------------------------------------------------
  function loadStarred() {
    try {
      return JSON.parse(localStorage.getItem("grantRadar_starred") || "{}");
    } catch {
      return {};
    }
  }

  function saveStarred() {
    localStorage.setItem("grantRadar_starred", JSON.stringify(starred));
  }

  function toggleStar(grantId, cardEl) {
    if (!grantId) return;
    if (starred[grantId]) {
      delete starred[grantId];
    } else {
      starred[grantId] = true;
    }
    saveStarred();
    const btn = cardEl.querySelector(".star-btn");
    btn.classList.toggle("starred");
    btn.textContent = starred[grantId] ? "\u2605" : "\u2606";
  }

  // ------------------------------------------------------------------
  // Rating management (thumbs up/down — localStorage)
  // ------------------------------------------------------------------
  function loadRatings() {
    try {
      return JSON.parse(localStorage.getItem("grantRadar_ratings") || "{}");
    } catch {
      return {};
    }
  }

  function saveRatings() {
    localStorage.setItem("grantRadar_ratings", JSON.stringify(ratings));
  }

  function setRating(grantId, grant, ratingValue, cardEl) {
    if (!grantId) return;
    // Toggle: if same rating, remove it
    if (ratings[grantId] && ratings[grantId].rating === ratingValue) {
      delete ratings[grantId];
    } else {
      ratings[grantId] = {
        rating: ratingValue,
        timestamp: new Date().toISOString(),
        title: grant.title || "",
        institution: grant.institution || "",
        tier: grant.tier,
        score: grant.relevance_score,
        eligibility_verdict: grant.eligibility_verdict || null,
        eligibility_unclear: grant.eligibility_unclear || null,
      };
    }
    saveRatings();
    _updateRatingUI(cardEl, grantId);
    updateFeedbackSettings();
  }

  function _updateRatingUI(cardEl, grantId) {
    const upBtn = cardEl.querySelector(".rate-up-btn");
    const downBtn = cardEl.querySelector(".rate-down-btn");
    if (!upBtn || !downBtn) return;
    const cur = ratings[grantId];
    upBtn.classList.toggle("active", !!(cur && cur.rating === "up"));
    downBtn.classList.toggle("active", !!(cur && cur.rating === "down"));
    cardEl.classList.remove("rated-up", "rated-down");
    if (cur) cardEl.classList.add(cur.rating === "up" ? "rated-up" : "rated-down");
  }

  // ------------------------------------------------------------------
  // Feedback export
  // ------------------------------------------------------------------
  function exportFeedbackLog() {
    const entries = Object.entries(ratings);
    if (entries.length === 0) {
      alert("No ratings to export.");
      return;
    }
    const timestamps = entries.map(([_, r]) => r.timestamp).sort();
    const fromDate = timestamps[0].slice(0, 10);
    const toDate = timestamps[timestamps.length - 1].slice(0, 10);
    const upCount = entries.filter(([_, r]) => r.rating === "up").length;
    const downCount = entries.filter(([_, r]) => r.rating === "down").length;

    // By-tier breakdown
    const byTier = {};
    entries.forEach(([, r]) => {
      const t = r.tier || 0;
      if (!byTier[t]) byTier[t] = { up: 0, down: 0 };
      byTier[t][r.rating]++;
    });

    // Collect all unclear eligibility questions for improvement review
    const unclearQuestions = [];
    entries.forEach(([id, r]) => {
      if (r.eligibility_unclear && r.eligibility_unclear.length) {
        r.eligibility_unclear.forEach(q => {
          unclearQuestions.push({
            grant_id: id,
            title: r.title,
            tier: r.tier,
            verdict: r.eligibility_verdict || "unknown",
            question: q,
            user_rating: r.rating,
          });
        });
      }
    });

    const exportData = {
      export_date: todayISO(),
      period: { from: fromDate, to: toDate },
      ratings: entries.map(([id, r]) => ({
        grant_id: id,
        title: r.title,
        institution: r.institution || "",
        tier: r.tier,
        score: r.score,
        rating: r.rating,
        rated_at: r.timestamp,
        eligibility_verdict: r.eligibility_verdict || null,
        eligibility_unclear: r.eligibility_unclear || null,
      })),
      unclear_requirements: unclearQuestions,
      summary: {
        total_rated: entries.length,
        thumbs_up: upCount,
        thumbs_down: downCount,
        by_tier: byTier,
        unclear_count: unclearQuestions.length,
      },
    };

    // Trigger download
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "grant_radar_feedback_" + todayISO().replace(/-/g, "_") + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    localStorage.setItem("grantRadar_lastFeedbackExport", new Date().toISOString());
    updateFeedbackSettings();
  }

  function updateFeedbackSettings() {
    const entries = Object.entries(ratings);
    const upCount = entries.filter(([_, r]) => r.rating === "up").length;
    const downCount = entries.filter(([_, r]) => r.rating === "down").length;
    const el = (id) => document.getElementById(id);
    if (el("settingsFeedbackCount")) el("settingsFeedbackCount").textContent = entries.length;
    if (el("settingsFeedbackUp")) el("settingsFeedbackUp").textContent = upCount;
    if (el("settingsFeedbackDown")) el("settingsFeedbackDown").textContent = downCount;
    const lastExport = localStorage.getItem("grantRadar_lastFeedbackExport");
    if (el("settingsFeedbackLastExport")) {
      el("settingsFeedbackLastExport").textContent = lastExport ? timeAgo(lastExport) : "Never";
    }
  }

  function checkFeedbackReminder() {
    const reminderEl = document.getElementById("feedbackReminder");
    if (!reminderEl) return;
    const entries = Object.entries(ratings);
    if (entries.length === 0) { reminderEl.classList.add("hidden"); return; }
    const lastExport = localStorage.getItem("grantRadar_lastFeedbackExport");
    const dismissed = localStorage.getItem("grantRadar_feedbackReminderDismissed");
    const thirtyDaysAgo = new Date();
    thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
    const needsReminder = !lastExport || new Date(lastExport) < thirtyDaysAgo;
    const recentlyDismissed = dismissed && new Date(dismissed) > thirtyDaysAgo;
    if (needsReminder && !recentlyDismissed) {
      reminderEl.classList.remove("hidden");
    } else {
      reminderEl.classList.add("hidden");
    }
  }

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------
  function _dedup(grants) {
    const seen = new Map();
    for (const g of grants) {
      const id = g.id || "";
      if (!seen.has(id)) seen.set(id, g);
    }
    return [...seen.values()];
  }

  function esc(s) {
    if (!s) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  function countryFlag(country) {
    if (!country) return "";
    const f = FLAGS[country.toLowerCase()];
    return f ? f + " " : "";
  }

  function todayISO() {
    return new Date().toISOString().slice(0, 10);
  }

  function daysAgoISO(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function daysBetween(a, b) {
    const da = new Date(a + "T00:00:00Z");
    const db = new Date(b + "T00:00:00Z");
    return Math.round((db - da) / 86400000);
  }

  function groupByTier(grants) {
    const m = {};
    grants.forEach(g => {
      const t = g.tier || 0;
      (m[t] = m[t] || []).push(g);
    });
    return m;
  }

  function timeAgo(iso) {
    const d = new Date(iso);
    const now = new Date();
    const diff = now - d;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + "h ago";
    const days = Math.floor(hrs / 24);
    if (days < 7) return days + "d ago";
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }

  function formatDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleDateString("en-GB", {
        day: "numeric", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

})();
