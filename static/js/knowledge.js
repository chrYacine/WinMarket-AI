// WinMarket AI — "Base de connaissances" page (lot 45, B27 / B03).
//
// Manages THIS account's own reference documents from the existing B03 routes:
//   GET  /api/knowledge/documents            list (name, dates, active version, latest upload state)
//   POST /api/knowledge/documents            upload (multipart `file`)
//   GET  /api/knowledge/documents/{id}       detail + versions
//   POST /api/knowledge/documents/{id}/versions   replacement
//   GET  /api/knowledge/documents/{id}/download   original of the active version
//   DELETE /api/knowledge/documents/{id}     logical deletion
//   GET  /api/knowledge, /api/knowledge/search, POST /api/knowledge/reload
//
// The server is the only authority for formats, size, quotas, extraction, roles
// and scope: this file shows what the routes answer and never invents an
// indexing percentage, a version or a document. Every server-supplied string
// (file names, error messages, passages) is written with textContent — never
// innerHTML — so a hostile file name is displayed, not executed.
(function () {
  "use strict";
  const root = document.getElementById("knowledge-root");
  if (!root) return;

  const ORG = root.dataset.org || "";
  const CAN_WRITE = root.dataset.canWrite === "1";
  const MAX_MB = Number(root.dataset.maxMb) || 0;
  const MAX_DOCS = Number(root.dataset.maxDocs) || 0;
  const FORMATS = (root.dataset.formats || "").split(",").filter(Boolean);
  const $ = (id) => document.getElementById(id);

  /* ── DOM helpers (nodes and text only — no HTML string is ever built) ── */
  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      else if (k === "style") node.style.cssText = v;
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => { if (c !== null && c !== undefined) node.append(c); });
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function formatDate(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "date inconnue" : d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
  }

  /* ── State ── */
  let epoch = 0;            // bumped when the organization changes under this page
  let scopeLost = false;
  let documents = [];       // last list received for ORG
  let openId = null;        // document whose detail panel is open
  let detail = null;        // detail body of openId
  let pendingDeleteId = null;
  let highlightId = null;
  let lastQuery = "";
  let searchSeq = 0;
  const busy = new Set();   // keys of operations in flight (double-submit guard)

  const feedbackBox = $("knowledge-feedback");
  const warningBox = $("knowledge-warning");
  const errorBox = $("knowledge-error");
  const listBox = $("knowledge-list");
  const resultsBox = $("knowledge-search-results");

  /* ── Messages ── */
  // A hidden box also loses its text: an earlier message never lingers for a screen reader.
  function clearMessages() {
    feedbackBox.hidden = true; clear(feedbackBox);
    warningBox.hidden = true; clear(warningBox);
    errorBox.hidden = true; clear(errorBox);
  }
  function showFeedback(text) {
    clearMessages();
    feedbackBox.hidden = false;
    feedbackBox.textContent = text;
  }
  // Lot 59: a batch where only SOME files were added is neither a success nor a failure.
  function showWarning(text) {
    clearMessages();
    warningBox.hidden = false;
    warningBox.textContent = text;
  }
  function showError(text, extra) {
    feedbackBox.hidden = true; clear(feedbackBox);
    warningBox.hidden = true; clear(warningBox);
    errorBox.hidden = false;
    clear(errorBox);
    errorBox.append(document.createTextNode(text));
    if (extra === "login") errorBox.append(document.createTextNode(" "), el("a", { href: "/login?next=/app/base-connaissances", text: "Se reconnecter" }));
    if (extra === "reload") errorBox.append(document.createTextNode(" "), el("a", { href: "/app/base-connaissances", text: "Recharger la page" }));
  }

  /* ── Organization scope: every request names its organization explicitly and
     an answer belonging to a previous scope is ignored. ── */
  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function loseScope() {
    scopeLost = true;
    epoch += 1;
    documents = []; detail = null; openId = null; pendingDeleteId = null; lastQuery = "";
    clear(listBox); clear(resultsBox);
    root.querySelectorAll("button, input").forEach((n) => { n.disabled = true; });
    showError("L'organisation active a changé depuis l'affichage de cette page : plus rien n'est envoyé ni affiché tant que la page n'est pas rechargée.", "reload");
  }
  // False once the selected organization differs from the one this page was
  // rendered for (another tab, the switcher): nothing may be written to the
  // wrong organization, and what is displayed no longer belongs to the current one.
  function scopeIsCurrent() {
    if (scopeLost) return false;
    const current = cookie("wm_org_id");
    if (current && ORG && current !== ORG) { loseScope(); return false; }
    return true;
  }
  window.addEventListener("focus", scopeIsCurrent);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) scopeIsCurrent(); });

  async function api(method, path, options) {
    const myEpoch = epoch;
    const init = { method, headers: { "X-CSRF-Token": window.wmCsrfToken() } };
    if (options && options.form) init.body = options.form; // multipart: the browser sets the boundary
    else if (options && options.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(options.json); }
    const url = path + (path.includes("?") ? "&" : "?") + "organization_id=" + encodeURIComponent(ORG);
    let res;
    try {
      res = await fetch(url, init);
    } catch {
      return myEpoch === epoch ? { networkError: true } : { stale: true };
    }
    if (options && options.blob && res.ok) {
      const blob = await res.blob();
      return myEpoch === epoch ? { res, status: res.status, ok: true, blob } : { stale: true };
    }
    let body = null;
    let nonJson = false;
    try { body = await res.json(); } catch { nonJson = true; body = {}; }
    if (myEpoch !== epoch) return { stale: true };
    return { res, status: res.status, ok: res.ok, body, nonJson };
  }

  /* ── Explanations (short, safe, with the useful next action) ── */
  const CODE_TEXT = {
    UNSUPPORTED_CONTENT: "Format non accepté, ou contenu qui ne correspond pas à l'extension du fichier. Formats acceptés : " + FORMATS.join(", ") + ".",
    CORRUPTED_FILE: "Le fichier est corrompu ou illisible (PDF ou archive DOCX invalide). Réenregistrez-le depuis son application d'origine puis renvoyez-le.",
    OCR_REQUIRED: "Ce PDF ne contient aucun texte extractible (scan ou images seules). La reconnaissance de caractères (OCR) n'est pas disponible : envoyez une version avec texte (PDF texte, DOCX, TXT ou MD).",
    EMPTY_CONTENT: "Le fichier ne contient aucun texte exploitable.",
    CONTENT_TOO_LARGE: "Le contenu extrait dépasse les limites de traitement (nombre de pages, de paragraphes ou de caractères). Découpez le document en plusieurs fichiers.",
  };
  function codeText(code) { return CODE_TEXT[code] || ("Le fichier n'a pas pu être exploité (code " + (code || "inconnu") + ").") ; }

  // Returns { text, extra } for a failed answer of one of the routes above.
  function explain(r, fallback) {
    if (r.networkError) return { text: "Impossible de contacter le serveur WinMarket AI — réessayez." };
    const body = r.body || {};
    const detailBody = body.detail;
    const code = detailBody && typeof detailBody === "object" ? (detailBody.error_code || (detailBody.version && detailBody.version.error_code)) : null;
    if (r.status === 401) return { text: "Votre session a expiré.", extra: "login" };
    if (r.status === 403) return { text: window.wmErrorMessage(body, "Action non autorisée pour votre rôle ou votre organisation.") };
    if (r.status === 404) return { text: "Ce document n'existe plus (supprimé depuis un autre onglet ?) — la liste a été actualisée." };
    if (r.status === 409) {
      if (code === "CORPUS_FULL") return { text: "Limite de " + MAX_DOCS + " documents atteinte pour ce compte : supprimez un document (y compris un document non exploitable) avant d'en ajouter un autre." };
      return { text: window.wmErrorMessage(body, "Conflit : l'état a changé — rechargez la page.") };
    }
    if (r.status === 413) return { text: "Fichier trop volumineux : la limite est de " + MAX_MB + " Mio par fichier. Rien n'a été enregistré." };
    if (r.status === 422 && code) return { text: codeText(code) };
    if (r.status === 429) return { text: window.wmErrorMessage(body, "Trop de requêtes — patientez un instant avant de réessayer.") };
    if (r.status === 503) return { text: "Service momentanément indisponible — réessayez dans un instant." };
    if (r.nonJson) return { text: "Réponse inattendue du serveur (code " + r.status + ") — réessayez ou rechargez la page." };
    return { text: window.wmErrorMessage(body, fallback || "L'opération a échoué.") };
  }
  function showFailure(r, fallback) {
    const { text, extra } = explain(r, fallback);
    showError(text, extra);
  }

  /* ── Double-submission guard ── */
  async function withBusy(key, button, busyLabel, fn) {
    if (busy.has(key)) return;
    busy.add(key);
    const original = button ? button.textContent : "";
    const hadFocus = !!button && document.activeElement === button;
    if (hadFocus) focusHint = focusKey();
    if (button) { button.disabled = true; button.textContent = busyLabel; }
    try {
      await fn();
    } finally {
      busy.delete(key);
      if (button && button.isConnected && !scopeLost) {
        button.disabled = false; button.textContent = original;
        if (hadFocus) button.focus({ preventScroll: true }); // a disabled control drops the focus; give it back
      }
    }
  }

  /* ── Content category (lot 50 bis §2 / lot 50 ter — was computed by the server but never shown here) ──
     A "certification" category is a PROPOSAL, never a verified certification; it is never applied to the
     profile or the scoring — display-only, with an honest correction control for a write-capable account. */
  const CATEGORY_LABEL = { reference: "Référence", certification: "Certification (proposée)", presentation: "Présentation", autre: "Autre", indetermine: "Indéterminée" };
  const CATEGORY_SOURCE_LABEL = { heuristic: "heuristique", llm: "IA", heuristic_llm_unavailable: "heuristique (IA indisponible)", heuristic_llm_invalid: "heuristique (réponse IA invalide)", user: "corrigée manuellement", unknown: "non déterminée (document antérieur à cette fonctionnalité)" };
  const CATEGORY_OPTIONS = ["indetermine", "reference", "certification", "presentation", "autre"];

  function categoryBlock(d) {
    const latest = d.latest_version;
    if (!latest) return null;
    const final = latest.content_category_final || "indetermine";
    const wrap = el("div", { style: "margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap", dataset: { role: "category" } });
    wrap.append(el("span", { className: "text-tertiary", style: "font-size:.8rem", text:
      "Catégorie : " + (CATEGORY_LABEL[final] || final) + " (" + (CATEGORY_SOURCE_LABEL[latest.content_category_source] || latest.content_category_source || "inconnue") + ")" }));
    if (CAN_WRITE) {
      const versionId = d.active_version_id || latest.id;
      const select = el("select", { className: "input", style: "font-size:.78rem;padding:2px 6px;width:auto", dataset: { role: "category-select" } },
        CATEGORY_OPTIONS.map((c) => el("option", { value: c, text: CATEGORY_LABEL[c], selected: c === final })));
      const btn = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Corriger", dataset: { action: "correct-category" } });
      btn.addEventListener("click", () => withBusy("category:" + d.id, btn, "…", () => correctCategory(d, versionId, select.value)));
      wrap.append(select, btn);
    }
    return wrap;
  }

  async function correctCategory(d, versionId, category) {
    if (!scopeIsCurrent()) return;
    clearMessages();
    const r = await api("POST", "/api/knowledge/documents/" + encodeURIComponent(d.id) + "/versions/" + encodeURIComponent(versionId) + "/category", {
      json: { category },
    });
    if (r.stale) return;
    if (!r.ok) { showFailure(r, "Impossible de corriger la catégorie."); return; }
    showFeedback("Catégorie de « " + d.original_filename + " » corrigée : " + (CATEGORY_LABEL[category] || category) + ". Une catégorie « certification » reste une proposition, jamais une certification vérifiée.");
    await refreshAll();
  }

  /* ── Vector index state (lot 51 — hybrid search, PostgreSQL/pgvector only) ──
     'not_applicable' on every SQLite deployment and on any PostgreSQL one without hybrid mode
     explicitly enabled: never shown as an error, just an honest "non applicable ici". A real
     failure ('failed') gets a retry action for a write-capable account, reusing the existing
     reindex route — never silently retried automatically, never hidden. */
  const EMBEDDING_STATUS_LABEL = {
    not_applicable: "Recherche sémantique : non activée sur cet environnement",
    pending: "Recherche sémantique : indexation pas encore effectuée",
    ready: "Recherche sémantique : active",
    failed: "Recherche sémantique : échec de l'indexation",
  };

  function embeddingBlock(d) {
    const latest = d.latest_version;
    if (!latest || !latest.embedding_status || latest.embedding_status === "not_applicable") return null;
    const wrap = el("div", { style: "margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap", dataset: { role: "embedding" } });
    const label = EMBEDDING_STATUS_LABEL[latest.embedding_status] || latest.embedding_status;
    wrap.append(el("span", { className: "text-tertiary", style: "font-size:.8rem", text:
      label + (latest.embedding_status === "failed" && latest.embedding_error_code ? " (" + latest.embedding_error_code + ")" : "") }));
    if (CAN_WRITE && (latest.embedding_status === "failed" || latest.embedding_status === "pending")) {
      const btn = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Réessayer l'indexation sémantique", dataset: { action: "reindex" } });
      btn.addEventListener("click", () => withBusy("reindex:" + d.id, btn, "…", () => reindexVersion(d, latest.id)));
      wrap.append(btn);
    }
    return wrap;
  }

  async function reindexVersion(d, versionId) {
    if (!scopeIsCurrent()) return;
    clearMessages();
    const r = await api("POST", "/api/knowledge/documents/" + encodeURIComponent(d.id) + "/versions/" + encodeURIComponent(versionId) + "/reindex", {});
    if (r.stale) return;
    if (!r.ok) { showFailure(r, "La réindexation sémantique a échoué."); return; }
    const status = r.body && r.body.embedding_status;
    showFeedback(status === "ready" ? "Indexation sémantique de « " + d.original_filename + " » réussie."
      : "Nouvelle tentative effectuée : " + (EMBEDDING_STATUS_LABEL[status] || status) + ".");
    await refreshAll();
  }

  /* ── Document state, derived from the LATEST upload — never from `status` alone ── */
  const VERSION_LABEL = { received: "Reçue", processing: "Traitement en cours", ready: "Exploitable", failed: "Échec" };
  function stateOf(d) {
    const latest = d.latest_version;
    const stale = latest && d.active_version_number !== null && latest.version_number !== d.active_version_number && latest.status !== "ready";
    if (d.active_version_id) {
      return {
        badge: { cls: "go", text: "Disponible" },
        note: stale ? (latest.status === "failed"
          ? "La dernière version envoyée (v" + latest.version_number + ") a échoué : " + codeText(latest.error_code) + " La version v" + d.active_version_number + " reste active."
          : "La version v" + latest.version_number + " est en cours de traitement ; la version v" + d.active_version_number + " reste active.") : null,
      };
    }
    if (latest && latest.status === "failed") {
      return {
        badge: { cls: "nogo", text: "Non exploitable" },
        note: "Aucune version exploitable : " + codeText(latest.error_code) + " Ce document n'est pas dans vos recherches mais occupe une place sur les " + MAX_DOCS + " autorisées. " +
          (CAN_WRITE
            ? "Que faire : « Détails et versions » pour consulter l'échec et envoyer une version corrigée, ou « Supprimer » (avec confirmation) pour libérer la place."
            : "Un compte autorisé à écrire peut l'ouvrir pour le remplacer, ou le supprimer pour libérer la place."),
      };
    }
    if (latest) return { badge: { cls: "reserve", text: VERSION_LABEL[latest.status] || "En cours" }, note: "Le traitement de la version v" + latest.version_number + " n'est pas terminé." };
    return { badge: { cls: "nogo", text: "Sans version" }, note: "Aucune version enregistrée pour ce document." };
  }

  /* ── Rendering ── */
  // Lot 59: a list or summary that could not be loaded says so — never a count left over from before, never "0".
  function markKpisUnavailable() {
    $("knowledge-total-docs").textContent = "Indisponible";
    $("knowledge-total-kb").textContent = "Indisponible";
    $("knowledge-last-update").textContent = "Indisponible";
  }

  function renderKpis(summary) {
    if (summary) {
      $("knowledge-total-docs").textContent = String(summary.total_documents);
      // The summary route floors to whole Ko: a small file must not read "0 Ko".
      $("knowledge-total-kb").textContent = summary.total_kb === 0 && summary.total_documents > 0 ? "< 1 Ko" : summary.total_kb + " Ko";
    } else {
      $("knowledge-total-docs").textContent = "Indisponible";
      $("knowledge-total-kb").textContent = "Indisponible";
    }
    const latest = documents.map((d) => d.updated_at).sort().pop();
    $("knowledge-last-update").textContent = latest ? formatDate(latest) : "—";
  }

  function versionsTable(d, det) {
    const table = el("table", { style: "width:100%;font-size:.82rem;border-collapse:collapse;margin-top:8px" });
    const head = (cells) => el("tr", {}, cells.map((c) => el("th", { text: c, style: "text-align:left;padding:4px 8px" })));
    table.append(head(["Version", "Envoyée le", "État", "Explication"]));
    det.versions.forEach((v) => {
      const active = d.active_version_id === v.id;
      table.append(el("tr", {}, [
        el("td", { text: "v" + v.version_number + (active ? " (active)" : ""), style: "padding:4px 8px;vertical-align:top" }),
        el("td", { text: formatDate(v.created_at), style: "padding:4px 8px;vertical-align:top" }),
        el("td", { text: VERSION_LABEL[v.status] || v.status, style: "padding:4px 8px;vertical-align:top" }),
        el("td", { text: v.status === "failed" ? codeText(v.error_code) : "", style: "padding:4px 8px;vertical-align:top" }),
      ]));
    });
    return table;
  }

  function detailPanel(d) {
    const panel = el("div", { className: "callout callout-info", style: "margin-top:12px;color:inherit", dataset: { role: "detail" } });
    if (!detail || detail.id !== d.id) {
      panel.append(el("span", { text: "Chargement des versions…" }));
      return panel;
    }
    panel.append(el("strong", { text: "Versions de « " + detail.original_filename + " »" }));
    panel.append(versionsTable(d, detail));
    panel.append(el("p", { className: "text-tertiary", style: "font-size:.78rem;margin:8px 0 0", text: "Seule la version active est utilisée par la recherche et par vos analyses ; les versions en échec ne sont jamais utilisées." }));
    if (CAN_WRITE) {
      const file = el("input", { type: "file", className: "input", accept: FORMATS.join(","), dataset: { role: "version-file" }, "aria-label": "Nouvelle version de " + d.original_filename });
      const send = el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "Envoyer la nouvelle version", dataset: { action: "send-version" } });
      const status = el("span", { className: "text-tertiary", style: "margin-left:8px;font-size:.82rem", role: "status" });
      send.addEventListener("click", () => withBusy("version:" + d.id, send, "Envoi…", () => uploadVersion(d, file, status)));
      panel.append(el("div", { style: "margin-top:12px" }, [
        el("div", { className: "field-hint", style: "margin-bottom:6px", text: "Remplacer par une nouvelle version : l'ancienne reste active tant que la nouvelle n'est pas exploitable." }),
        file, el("div", { style: "margin-top:8px" }, [send, status]),
      ]));
    }
    return panel;
  }

  function documentCard(d) {
    const st = stateOf(d);
    const isOpen = openId === d.id;
    const card = el("div", { className: "card card-tight", style: "margin-bottom:12px" + (d.id === highlightId ? ";outline:2px solid var(--primary)" : ""), dataset: { docId: d.id } });
    card.append(el("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap" }, [
      el("strong", { text: d.original_filename, dataset: { role: "name" }, style: "overflow-wrap:anywhere" }),
      el("span", { className: "badge badge-" + st.badge.cls, text: st.badge.text, dataset: { role: "state" } }),
    ]));
    card.append(el("div", { className: "text-tertiary", style: "font-size:.8rem;margin-top:6px", text:
      "Ajouté le " + formatDate(d.created_at) + " · modifié le " + formatDate(d.updated_at) + " · " +
      (d.active_version_number !== null ? "version active : v" + d.active_version_number : "aucune version active") }));
    if (st.note) card.append(el("div", { className: "text-secondary", style: "font-size:.82rem;margin-top:6px", text: st.note, dataset: { role: "note" } }));
    const catBlock = categoryBlock(d);
    if (catBlock) card.append(catBlock);
    const embBlock = embeddingBlock(d);
    if (embBlock) card.append(embBlock);

    const actions = el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:10px" });
    actions.append(el("button", { type: "button", className: "btn btn-secondary btn-sm", text: isOpen ? "Masquer les versions" : "Détails et versions", dataset: { action: "detail" },
      on: { click: () => toggleDetail(d) } }));
    if (d.active_version_id) {
      const dl = el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "Télécharger l'original", dataset: { action: "download" } });
      dl.addEventListener("click", () => withBusy("download:" + d.id, dl, "Téléchargement…", () => download(d)));
      actions.append(dl);
    }
    if (CAN_WRITE) {
      actions.append(el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Supprimer", dataset: { action: "delete" },
        on: { click: () => { pendingDeleteId = d.id; renderList(); } } }));
    }
    card.append(actions);

    if (CAN_WRITE && pendingDeleteId === d.id) {
      const confirmBtn = el("button", { type: "button", className: "btn btn-primary btn-sm", text: "Confirmer la suppression", dataset: { action: "confirm-delete" } });
      confirmBtn.addEventListener("click", () => withBusy("delete:" + d.id, confirmBtn, "Suppression…", () => remove(d)));
      card.append(el("div", { className: "callout callout-warning", style: "margin-top:10px", role: "alert" }, [
        el("div", { text: "Supprimer « " + d.original_filename + " » ? Le document et toutes ses versions seront exclus de vos recherches. Vos analyses déjà réalisées ne sont pas modifiées." }),
        el("div", { style: "margin-top:8px;display:flex;gap:8px" }, [
          confirmBtn,
          el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "Annuler", dataset: { action: "cancel-delete" }, on: { click: () => { pendingDeleteId = null; renderList(); } } }),
        ]),
      ]));
    }
    if (isOpen) card.append(detailPanel(d));
    return card;
  }

  // Keyboard users: a re-draw must not throw the focus back to the top of the page. The control that was used (or, when
  // its card is gone, the status message) gets the focus again.
  let focusHint = null;
  function focusKey() {
    const a = document.activeElement;
    if (!a || !listBox.contains(a)) return null;
    const card = a.closest("[data-doc-id]");
    return card ? { doc: card.dataset.docId, action: a.dataset.action || null } : null;
  }
  function restoreFocus(key) {
    if (!key) return;
    const inCard = (action) => listBox.querySelector('[data-doc-id="' + CSS.escape(key.doc) + '"] [data-action="' + action + '"]');
    const next = { delete: "confirm-delete", "cancel-delete": "delete" }[key.action] || key.action;
    const target = (next && inCard(next)) || inCard(key.action || "detail") || inCard("detail");
    // preventScroll: moving the focus must never move the page under the user's pointer (a smooth scroll to the
    // message at the top made a click on "Ajouter" right after a deletion miss its target — reproduced 2 times in 5)
    if (target) { target.focus({ preventScroll: true }); return; }
    const box = [feedbackBox, warningBox, errorBox].find((b) => !b.hidden) || null;
    if (box) { box.tabIndex = -1; box.focus({ preventScroll: true }); }
  }

  function renderList() {
    const key = focusHint || focusKey();
    focusHint = null;
    drawList();
    restoreFocus(key);
  }

  function drawList() {
    clear(listBox);
    if (!documents.length) {
      listBox.append(el("div", { className: "card", dataset: { role: "empty" } }, [
        el("strong", { text: "Aucun document de référence pour ce compte." }),
        el("p", { className: "text-secondary", style: "margin:8px 0 0;font-size:.9rem", text:
          (CAN_WRITE ? "Ajoutez vos propres références (offres passées, mémoires techniques, attestations…) avec le formulaire ci-dessus. " : "") +
          "Les documents de ce compte seront consultés par ses analyses. Aucun document d'exemple n'est fourni par défaut : sans document, vos analyses ne s'appuient sur aucune référence." }),
      ]));
      return;
    }
    documents.forEach((d) => listBox.append(documentCard(d)));
    if (highlightId) {
      const target = listBox.querySelector('[data-doc-id="' + CSS.escape(highlightId) + '"]');
      if (target && target.scrollIntoView) target.scrollIntoView({ block: "nearest" });
    }
  }

  /* ── Loading ── */
  function renderListFailure(r) {
    clear(listBox);
    const { text, extra } = explain(r, "Impossible de charger vos documents.");
    listBox.append(el("div", { className: "callout callout-danger" }, [document.createTextNode(text + " "),
      extra === "login" ? el("a", { href: "/login?next=/app/base-connaissances", text: "Se reconnecter" })
        : el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "Réessayer", on: { click: () => refreshAll() } })]));
  }

  // Everything the page shows about the account is fetched FIRST (list, counters,
  // open detail) and drawn ONCE: a re-draw between two answers would throw away
  // a file the user has just chosen in the version panel.
  async function refreshAll() {
    listBox.setAttribute("aria-busy", "true");
    try {
      const [listAnswer, summaryAnswer, detailAnswer] = await Promise.all([
        api("GET", "/api/knowledge/documents"),
        api("GET", "/api/knowledge"),
        openId ? api("GET", "/api/knowledge/documents/" + encodeURIComponent(openId)) : Promise.resolve(null),
      ]);
      if (listAnswer.stale) return;
      if (!listAnswer.ok) { renderListFailure(listAnswer); markKpisUnavailable(); return; }
      documents = (listAnswer.body && listAnswer.body.documents) || [];
      if (openId && !documents.some((d) => d.id === openId)) { openId = null; detail = null; }
      if (pendingDeleteId && !documents.some((d) => d.id === pendingDeleteId)) pendingDeleteId = null;
      if (openId && detailAnswer && !detailAnswer.stale) {
        if (detailAnswer.ok) detail = detailAnswer.body;
        else if (detailAnswer.status === 404) { openId = null; detail = null; }
        else showFailure(detailAnswer, "Impossible de charger les versions de ce document.");
      }
      renderList();
      renderKpis(summaryAnswer && summaryAnswer.ok ? summaryAnswer.body : null);
      if (lastQuery) await runSearch(lastQuery);
    } finally {
      listBox.removeAttribute("aria-busy");
    }
  }

  async function loadDetail(id) {
    const r = await api("GET", "/api/knowledge/documents/" + encodeURIComponent(id));
    if (r.stale) return;
    if (!r.ok) {
      if (r.status === 404) { openId = null; detail = null; await refreshAll(); }
      showFailure(r, "Impossible de charger les versions de ce document.");
      return;
    }
    if (openId === id) { detail = r.body; renderList(); }
  }

  async function toggleDetail(d) {
    if (!scopeIsCurrent()) return;
    if (openId === d.id) { openId = null; detail = null; renderList(); return; }
    openId = d.id; detail = null; renderList();
    await loadDetail(d.id);
  }

  /* ── Mutations ── */
  const label = (file) => "« " + file.name + " »";

  async function uploadOne(file, status) {
    // Reuses the SAME B03 route as a single upload always has — no second/batch pipeline. Returns a short,
    // safe outcome line naming this file (its own success or failure), never innerHTML.
    const form = new FormData();
    form.append("file", file);
    const r = await api("POST", "/api/knowledge/documents", { form });
    if (r.stale) return { stale: true };
    if (r.ok) {
      highlightId = r.body.document.id;
      return { ok: true, text: label(file) + " : ajouté (version " + r.body.version.version_number + " exploitable)." };
    }
    const detailBody = r.body && r.body.detail;
    if (r.status === 422 && detailBody && detailBody.document) {
      // The document exists (with a failed version): shown in the list, NOT re-sent automatically.
      highlightId = detailBody.document.id;
      return { ok: false, text: label(file) + " : reçu mais non exploitable — " + codeText(detailBody.version && detailBody.version.error_code) };
    }
    if (r.status === 409 && detailBody && detailBody.error_code === "CORPUS_FULL") {
      return { ok: false, stop: true, text: label(file) + " : limite de " + MAX_DOCS + " documents atteinte — non ajouté." };
    }
    if (r.networkError || r.status >= 500) {
      // Lot 59: the answer was lost (server restarting, proxy 502…), so the file may or may not have been saved.
      // Never re-sent automatically, and the batch stops: the reloaded list is the only source of truth.
      const cause = r.networkError ? "serveur injoignable" : "HTTP " + r.status;
      return { ok: false, unknown: true, stop: true,
        text: label(file) + " : réponse perdue (" + cause + ") — état inconnu, vérifiez la liste avant de le renvoyer." };
    }
    const { text } = explain(r, "l'ajout a échoué");
    return { ok: false, text: label(file) + " : " + text };
  }

  async function uploadNew() {
    if (!scopeIsCurrent()) return;
    const input = $("knowledge-file");
    const status = $("knowledge-upload-status");
    const files = input.files ? Array.from(input.files) : [];
    clearMessages();
    if (!files.length) { showError("Choisissez d'abord un fichier à ajouter."); return; }
    if (files.length === 1) {
      status.textContent = "Envoi et traitement en cours…";
      const outcome = await uploadOne(files[0], status);
      status.textContent = "";
      if (outcome.stale) return;
      input.value = "";
      if (outcome.ok) showFeedback(outcome.text); else showError(outcome.text);
      await refreshAll();
      return;
    }
    // Several files: the EXISTING single-document route, called once per file, in the order chosen — each
    // outcome (success or failure) is named individually; no automatic dedup, no cross-account mixing (each
    // call carries this same, one, authenticated scope), no silent partial success.
    const lines = [];
    const counts = { ok: 0, failed: 0, unknown: 0, notSent: 0 };
    let sent = 0;
    for (let i = 0; i < files.length; i += 1) {
      status.textContent = `Envoi ${i + 1}/${files.length} : ${files[i].name}…`;
      const outcome = await uploadOne(files[i], status);
      if (outcome.stale) return;
      sent += 1;
      lines.push(outcome.text);
      if (outcome.ok) counts.ok += 1; else if (outcome.unknown) counts.unknown += 1; else counts.failed += 1;
      // corpus full, or an answer lost: further files would fail too — stop, never re-send anything automatically
      if (outcome.stop) break;
    }
    counts.notSent = files.length - sent;
    if (counts.notSent) lines.push(counts.notSent + " fichier(s) non envoyé(s) : l'envoi s'est arrêté, rien n'est renvoyé automatiquement.");
    status.textContent = "";
    input.value = "";
    const { kind, text } = batchSummary(counts, files.length);
    const summary = text + " " + lines.join(" ");
    if (kind === "success") showFeedback(summary); else if (kind === "partial") showWarning(summary); else showError(summary);
    await refreshAll();
  }

  // Lot 59: the banner's colour describes the WHOLE batch — green only when every file was added.
  function batchSummary(counts, total) {
    const parts = [counts.ok + " réussi(s)"];
    if (counts.failed) parts.push(counts.failed + " échoué(s)");
    if (counts.unknown) parts.push(counts.unknown + " à vérifier");
    if (counts.notSent) parts.push(counts.notSent + " non envoyé(s)");
    const kind = counts.ok === total ? "success" : (counts.ok > 0 ? "partial" : "failure");
    const title = { success: "Import terminé", partial: "Import partiel", failure: "Import échoué" }[kind];
    return { kind, text: title + " : " + parts.join(", ") + " sur " + total + "." };
  }

  async function uploadVersion(d, input, status) {
    if (!scopeIsCurrent()) return;
    const file = input.files && input.files[0];
    clearMessages();
    if (!file) { showError("Choisissez d'abord le fichier de la nouvelle version."); return; }
    status.textContent = "Envoi et traitement en cours…";
    const form = new FormData();
    form.append("file", file);
    const r = await api("POST", "/api/knowledge/documents/" + encodeURIComponent(d.id) + "/versions", { form });
    if (r.stale) return;
    status.textContent = "";
    highlightId = d.id;
    if (r.ok) {
      showFeedback("Nouvelle version v" + r.body.version.version_number + " de « " + d.original_filename + " » exploitable et active : l'ancienne version n'est plus utilisée dans les recherches.");
      await refreshAll();
      return;
    }
    const detailBody = r.body && r.body.detail;
    if (r.status === 422 && detailBody && detailBody.document) {
      const active = detailBody.document.active_version_number;
      showError("La version " + label(file) + " n'a pas pu être exploitée. " + codeText(detailBody.version && detailBody.version.error_code) + " " +
        (active !== null && active !== undefined ? "La version v" + active + " reste active et utilisée." : "Aucune version n'est active pour ce document."));
      await refreshAll();
      return;
    }
    if (r.status === 422) {
      showError(explain(r).text + " La version active n'a pas changé.");
      return;
    }
    showFailure(r, "Impossible d'envoyer cette version.");
    if (r.status === 404) await refreshAll();
  }

  async function remove(d) {
    if (!scopeIsCurrent()) return;
    clearMessages();
    const r = await api("DELETE", "/api/knowledge/documents/" + encodeURIComponent(d.id));
    if (r.stale) return;
    pendingDeleteId = null;
    if (!r.ok) {
      showFailure(r, "Impossible de supprimer ce document.");
      await refreshAll();
      return;
    }
    if (openId === d.id) { openId = null; detail = null; }
    highlightId = null;
    const cleaned = r.body && r.body.physically_cleaned;
    showFeedback("Document « " + d.original_filename + " » supprimé et exclu de vos recherches. " + (cleaned
      ? "Ses fichiers ont été retirés du stockage de l'application (les éventuelles sauvegardes du serveur ne sont pas concernées)."
      : "Attention : le nettoyage de ses fichiers sur le serveur n'a pas pu être confirmé ; le document reste bien supprimé côté application et l'incident est journalisé pour l'exploitation."));
    await refreshAll();
  }

  function filenameFrom(res, fallback) {
    const header = res.headers.get("Content-Disposition") || "";
    const star = header.match(/filename\*=(?:UTF-8|utf-8)''([^;]+)/);
    if (star) { try { return decodeURIComponent(star[1]); } catch { /* fall through */ } }
    const plain = header.match(/filename="?([^";]+)"?/);
    return plain ? plain[1] : fallback;
  }

  async function download(d) {
    if (!scopeIsCurrent()) return;
    clearMessages();
    const r = await api("GET", "/api/knowledge/documents/" + encodeURIComponent(d.id) + "/download", { blob: true });
    if (r.stale) return;
    if (!r.ok) {
      if (r.status === 404) {
        showError("Le fichier original de « " + d.original_filename + " » n'est pas (ou plus) disponible au téléchargement. Le texte déjà indexé reste utilisé par la recherche et vos analyses passées ne sont pas modifiées.");
        await refreshAll();
      } else {
        showFailure(r, "Impossible de télécharger ce document.");
      }
      return;
    }
    const url = URL.createObjectURL(r.blob);
    const link = el("a", { href: url, download: filenameFrom(r.res, d.original_filename) });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    showFeedback("Téléchargement de « " + d.original_filename + " » lancé (" + r.blob.size + " octets).");
  }

  /* ── Search mode (lot 51 §4 — "mode de recherche et dégradation éventuelle lisibles") ──
     Never a technical distance/score presented as business confidence — just an honest label
     of what actually ran, and a visible warning when a real provider failure degraded the
     search to lexical-only (never silently presented as a normal empty/complete result). */
  const SEARCH_MODE_LABEL = {
    empty_query: null,
    empty_corpus: null,
    lexical: "Recherche lexicale (mots-clés).",
    hybrid: "Recherche sémantique + lexicale.",
    hybrid_partial: "Recherche sémantique + lexicale — un ou plusieurs documents ne sont pas (encore) indexés sémantiquement ; ils restent trouvables par mots-clés.",
    hybrid_degraded_vector_unavailable: "Recherche lexicale uniquement — la recherche sémantique est momentanément indisponible, réessayez plus tard.",
  };

  /* ── Search in this account's own corpus ── */
  async function runSearch(query) {
    const q = (query || "").trim();
    lastQuery = q;
    if (!q) { clear(resultsBox); return; }
    const seq = ++searchSeq;
    const r = await api("GET", "/api/knowledge/search?q=" + encodeURIComponent(q));
    if (r.stale || seq !== searchSeq) return; // an older answer never overwrites a newer one
    clear(resultsBox);
    if (!r.ok) {
      const { text, extra } = explain(r, "La recherche a échoué.");
      resultsBox.append(el("div", { className: "callout callout-danger", text }));
      if (extra === "login") resultsBox.append(el("a", { href: "/login?next=/app/base-connaissances", text: "Se reconnecter" }));
      return;
    }
    const mode = r.body && r.body.mode;
    const modeLabel = SEARCH_MODE_LABEL[mode];
    if (modeLabel) {
      resultsBox.append(el("p", {
        className: mode === "hybrid_degraded_vector_unavailable" ? "callout callout-warning" : "text-tertiary",
        style: "font-size:.8rem;margin-bottom:8px", dataset: { role: "search-mode" }, text: modeLabel,
      }));
    }
    const results = (r.body && r.body.results) || [];
    if (!results.length) {
      resultsBox.append(el("p", { className: "text-tertiary", dataset: { role: "no-result" }, text: r.body && r.body.corpus_empty
        ? "Votre base ne contient aucun document exploitable : ajoutez-en pour pouvoir rechercher."
        : "Aucun passage correspondant dans vos documents actifs." }));
      return;
    }
    resultsBox.append(el("p", { className: "text-tertiary", style: "margin-bottom:12px", text: results.length + " passage(s) dans vos documents" }));
    results.forEach((res) => {
      const header = [
        el("div", { style: "font-weight:600;overflow-wrap:anywhere", text: "Source : " + res.source, dataset: { role: "source" } }),
        el("div", { className: "text-tertiary", style: "font-size:.78rem", text: "pertinence : " + res.relevance_pct + " %" }),
      ];
      // Lot 51 bis — a vector nearest-neighbor has no relevance floor of its own: shown here as
      // an honest, distinct "pertinence non confirmée" note (never hidden, never rejected) —
      // this is a search RESULT, not a claim it would be retained as scoring evidence.
      if (res.lexically_confirmed === false) {
        header.push(el("div", { className: "text-tertiary", style: "font-size:.75rem;font-style:italic", text: "Pertinence non confirmée par recoupement lexical — résultat sémantique seul.", dataset: { role: "unconfirmed" } }));
      }
      resultsBox.append(el("div", { className: "card card-tight", style: "margin-bottom:10px", dataset: { role: "result" } }, [
        ...header,
        el("p", { className: "text-secondary", style: "font-size:.85rem;white-space:pre-wrap;margin:8px 0 0", text: res.excerpt, dataset: { role: "excerpt" } }),
      ]));
    });
  }

  /* ── Wiring ── */
  const uploadBtn = $("knowledge-upload");
  if (uploadBtn) uploadBtn.addEventListener("click", () => withBusy("upload", uploadBtn, "Envoi en cours…", uploadNew));

  const reloadBtn = $("knowledge-reload");
  if (reloadBtn) {
    reloadBtn.addEventListener("click", () => withBusy("reload", reloadBtn, "Actualisation…", async () => {
      if (!scopeIsCurrent()) return;
      clearMessages();
      const r = await api("POST", "/api/knowledge/reload");
      if (r.stale) return;
      if (!r.ok) { showFailure(r, "Impossible d'actualiser l'index."); return; }
      showFeedback("Index de recherche de votre compte reconstruit.");
      await refreshAll();
    }));
  }

  const searchInput = $("knowledge-search");
  const searchBtn = $("knowledge-search-btn");
  function submitSearch() { if (scopeIsCurrent()) runSearch(searchInput.value); }
  searchBtn.addEventListener("click", submitSearch);
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitSearch(); } });

  refreshAll();
})();
