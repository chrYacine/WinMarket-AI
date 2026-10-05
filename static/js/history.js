// WinMarket AI — Historique page.
//
// B27-T1 (DEFECT confirmed): this used to embed the ENTIRE (pre-B22-T1,
// 200-row-capped) history into window.WM_HISTORY server-side and filter it
// entirely client-side — never calling the real paginated GET /api/history
// (B22-T1), and silently re-introducing the exact "invisible past row 200"
// defect that endpoint was built to fix. Now fetches one real page at a
// time from the server (organization-scoped since B22-T2) and never claims
// a filter searches more than the currently loaded page — see the "(page
// affichée)" labels in templates/app_history.html.
(function () {
  "use strict";
  const root = document.getElementById("history-root");
  if (!root) return;

  const list = document.getElementById("history-list");
  const countLabel = document.getElementById("history-count");
  const errorBox = document.getElementById("history-error");
  const decisionFilter = document.getElementById("filter-decision");
  const scoreFilter = document.getElementById("filter-score");
  const scoreOut = document.getElementById("filter-score-out");
  const searchFilter = document.getElementById("filter-search");
  const pagination = document.getElementById("history-pagination");
  const pageLabel = document.getElementById("history-page-label");
  const prevBtn = document.getElementById("history-prev");
  const nextBtn = document.getElementById("history-next");

  const PAGE_SIZE = 50;
  let currentPage = 1;
  let totalPages = 1;
  let pageItems = [];

  function decisionClass(decision) {
    if (decision === "GO") return "go";
    if ((decision || "").toUpperCase().includes("RESERVE")) return "reserve";
    if (decision === "INCOMPLET") return "incomplete";
    return "nogo";
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function applyFiltersAndRender() {
    const decision = decisionFilter.value;
    const minScore = Number(scoreFilter.value);
    const query = searchFilter.value.trim().toLowerCase();
    scoreOut.textContent = minScore;

    const filtered = pageItems.filter((r) => {
      if (decision !== "Toutes" && !(r.decision || "").toUpperCase().includes(decision.toUpperCase().replace("É", "E"))) return false;
      if ((r.score || 0) < minScore) return false;
      if (query) {
        const haystack = [r.titre, r.client, ...(r.techs || [])].join(" ").toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });

    // Deliberately never a "X sur Y" count implying Y is the full
    // history — Y here is only ever this page's own row count (real
    // pagination lives server-side, see loadPage below).
    countLabel.textContent = `${filtered.length} affiché(s) sur cette page (page ${currentPage}/${totalPages}, ${pageItems.length} sur cette page)`;
    if (!filtered.length) {
      list.innerHTML = '<p class="text-tertiary">Aucune analyse ne correspond aux filtres sur cette page.</p>';
      return;
    }
    list.innerHTML = filtered
      .map((r) => {
        const cls = decisionClass(r.decision);
        const budget = r.budget ? `${Number(r.budget).toLocaleString("fr-FR")} €` : "–";
        const techs = (r.techs || []).join(", ") || "–";
        const link = r.job_id ? `/app/resultats/${r.job_id}` : null;
        // Lot 53 — a revision (declarative complement, lot 49) or a documentary re-analysis (lot 50 bis §3)
        // says so, with a link to its own private lineage — never merging the two kinds of wording. A
        // <span data-href> (not a nested <a>) since this row itself may already be wrapped in its own <a>
        // below — an anchor inside an anchor is invalid HTML and breaks click targeting in every browser;
        // the delegated click/keydown handlers further down navigate it instead. Lot 54 §1 (DEFECT
        // confirmed, real browser check): a bare <span data-href> is NOT reachable by Tab and does nothing
        // on Enter — role="link" + tabindex="0" make it a real keyboard-operable link, exactly like the
        // native <a> the row itself may already be. Also lot 54: never claims "pièces ajoutées" here (the
        // history list has no cheap way to compare piece sets per row — the honest, undetermined wording is
        // "dossier modifié"; the result page itself, which DOES compare, states the exact change).
        const LINK_ATTRS = 'role="link" tabindex="0" style="text-decoration:underline;cursor:pointer"';
        let lineage = "";
        if (r.parent_job_id) lineage = `<br><span class="lineage-link" data-href="/app/resultats/${r.parent_job_id}" ${LINK_ATTRS}>↳ révision d'une analyse précédente</span>`;
        else if (r.origin_job_id) lineage = `<br><span class="lineage-link" data-href="/app/resultats/${r.origin_job_id}" ${LINK_ATTRS}>↳ nouvelle analyse documentaire, dossier modifié</span>`;
        const inner = `
          <div>
            <div class="list-row-title">${escapeHtml(r.titre || "–")}</div>
            <div class="list-row-meta">${escapeHtml(r.client || "–")} · ${escapeHtml(r.secteur || "–")} · Budget : ${budget}<br>
              Technologies : ${escapeHtml(techs)} · Analysé le ${escapeHtml(r.date || "–")}${r.dossier ? "<br>Dossier d'appel d'offres : " + escapeHtml(r.dossier) : ""}${lineage}</div>
          </div>
          <div style="text-align:right;min-width:110px">
            <div class="badge badge-${cls}" style="font-size:.72rem;padding:.25rem .7rem">${escapeHtml(r.decision || "–")}</div>
            <div class="font-mono" style="margin-top:.4rem;font-weight:700">${r.score ?? "–"}/100</div>
          </div>`;
        return link
          ? `<a class="list-row" href="${link}" style="text-decoration:none">${inner}</a>`
          : `<div class="list-row">${inner}</div>`;
      })
      .join("");
  }

  async function loadPage(page) {
    errorBox.hidden = true;
    countLabel.textContent = "Chargement…";
    list.innerHTML = "";
    try {
      const res = await fetch(`/api/history?page=${page}&page_size=${PAGE_SIZE}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        errorBox.hidden = false;
        errorBox.textContent = window.wmErrorMessage(body, "Impossible de charger l'historique.");
        countLabel.textContent = "";
        pagination.hidden = true;
        return;
      }
      const data = await res.json();
      currentPage = data.page;
      totalPages = data.total_pages;
      pageItems = data.items;

      if (data.total === 0) {
        list.innerHTML = '<p class="text-tertiary">Aucun appel d’offres dans l’historique. Analysez un premier appel d’offres depuis « Nouvelle analyse ».</p>';
        countLabel.textContent = "";
        pagination.hidden = true;
        return;
      }

      pagination.hidden = totalPages <= 1;
      pageLabel.textContent = `Page ${currentPage} / ${totalPages} — ${data.total} analyse(s) au total`;
      prevBtn.disabled = currentPage <= 1;
      nextBtn.disabled = currentPage >= totalPages;
      applyFiltersAndRender();
    } catch {
      errorBox.hidden = false;
      errorBox.textContent = "Impossible de contacter le serveur WinMarket AI.";
      countLabel.textContent = "";
      pagination.hidden = true;
    }
  }

  list.addEventListener("click", (e) => {
    const el = e.target.closest(".lineage-link");
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    window.location.href = el.dataset.href;
  });
  // Lot 54 §1 — keyboard activation (Enter/Space) for the lineage span: a role="link" alone does not make
  // a <span> respond to a key press the way a native <a> does, that has to be wired explicitly.
  list.addEventListener("keydown", (e) => {
    const el = e.target.closest(".lineage-link");
    if (!el || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    e.stopPropagation();
    window.location.href = el.dataset.href;
  });

  prevBtn.addEventListener("click", () => { if (currentPage > 1) loadPage(currentPage - 1); });
  nextBtn.addEventListener("click", () => { if (currentPage < totalPages) loadPage(currentPage + 1); });
  [decisionFilter, scoreFilter, searchFilter].forEach((el) => el && el.addEventListener("input", applyFiltersAndRender));

  loadPage(1);
})();
