// WinMarket AI — dossier admission table (lot 50 §3): shown between `POST /api/analyze/dossier-preview` and
// `POST /api/analyze/dossier-preview/{id}/confirm`. Every name/label/reason from the server is written with
// textContent (never innerHTML). No score, decision or scoring input is ever shown or editable here — only
// which pieces will be taken into account, under which category, and why.
(function () {
  "use strict";

  const panel = document.getElementById("dossier-preview-panel");
  const formPanel = document.getElementById("analyze-form-panel");
  if (!panel || !formPanel) return;

  const rowsEl = document.getElementById("dossier-preview-rows");
  const rejectedEl = document.getElementById("dossier-preview-rejected");
  const errorEl = document.getElementById("dossier-preview-error");
  const statusEl = document.getElementById("dossier-preview-status");
  const backBtn = document.getElementById("dossier-preview-back");
  const confirmBtn = document.getElementById("dossier-preview-confirm");

  const CATEGORY_LABELS = { rc: "RC", cctp: "CCTP", ccap: "CCAP", acte_engagement: "Acte d'engagement", annexe: "Annexe", autre: "Autre" };
  const SECURITY_LABELS = { authorized: "Autorisée", to_verify: "À vérifier", blocked: "Bloquée" };
  const SECURITY_BADGE = { authorized: "go", to_verify: "reserve", blocked: "nogo" };
  const RELEVANCE_LABELS = { lie: "Liée au dossier", incertain: "Incertaine", hors_sujet: "Hors sujet" };
  // Lot 50 bis §1 — which path actually produced a proposal: never silently implied as an LLM validation
  // that did not happen.
  const ORIGIN_LABELS = {
    heuristic: "lecture par mots-clés", llm: "avis IA", heuristic_llm_unavailable: "lecture par mots-clés (IA indisponible)",
    heuristic_llm_invalid: "lecture par mots-clés (réponse IA invalide)",
  };
  function originNote(source) {
    return el("div", { className: "field-hint", text: "Origine : " + (ORIGIN_LABELS[source] || source || "lecture par mots-clés") });
  }

  let current = null;
  let onConfirmed = null;

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      else if (k === "style") node.style.cssText = v;
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => { if (c !== null && c !== undefined) node.append(c); });
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function categorySelect(piece) {
    const select = el("select", { className: "input" });
    const current = piece.categorie_finale || piece.categorie;
    Object.keys(CATEGORY_LABELS).forEach((cat) => {
      let label = CATEGORY_LABELS[cat];
      if (cat === piece.categorie_proposee) label += " (proposée)";
      const opt = el("option", { value: cat, text: label });
      if (cat === current) opt.selected = true;
      select.append(opt);
    });
    return select;
  }

  function renderRow(piece) {
    const tr = el("tr", {});
    tr.append(el("td", {}, [
      el("div", { text: piece.nom }),
      el("div", { className: "field-hint", text: (piece.categorie_libelle || piece.categorie) + " déclarée" }),
    ]));

    const catCell = el("td");
    const select = categorySelect(piece);
    catCell.append(select);
    tr.append(catCell);

    const secCell = el("td");
    secCell.append(el("span", { className: "badge badge-" + (SECURITY_BADGE[piece.securite] || "reserve"), text: SECURITY_LABELS[piece.securite] || piece.securite }));
    if (piece.securite_motif) secCell.append(el("div", { className: "field-hint", text: piece.securite_motif }));
    if (piece.securite !== "authorized" || piece.securite_origine === "llm") secCell.append(originNote(piece.securite_origine));
    tr.append(secCell);

    const relCell = el("td");
    if (piece.pertinence) {
      relCell.append(el("div", { text: RELEVANCE_LABELS[piece.pertinence] || piece.pertinence }));
      if (piece.pertinence_motif) relCell.append(el("div", { className: "field-hint", text: piece.pertinence_motif }));
      relCell.append(originNote(piece.pertinence_origine));
    } else {
      relCell.append(el("span", { className: "text-tertiary", text: piece.doublon_de ? "Doublon — non relu" : "—" }));
    }
    tr.append(relCell);

    const includeCell = el("td");
    const checkbox = el("input", { type: "checkbox", checked: !!piece.sera_pris_en_compte });
    const blocked = piece.securite === "blocked";
    if (blocked) { checkbox.checked = false; checkbox.disabled = true; }
    const label = el("label", { style: "display:flex;align-items:center;gap:6px" }, [checkbox, el("span", { text: "Inclure" })]);
    includeCell.append(label);
    if (blocked) {
      includeCell.append(el("div", { className: "field-hint", text: "Blocage de sécurité : ne peut pas être levé ici. Corrigez ou remplacez la pièce." }));
    } else if (piece.raison_exclusion) {
      includeCell.append(el("div", { className: "field-hint", text: piece.raison_exclusion }));
    }
    tr.append(includeCell);

    const noteCell = el("td");
    const noteInput = el("textarea", { className: "textarea", style: "min-height:40px;width:100%", placeholder: "Lien avec ce dossier (facultatif)" });
    if (piece.lien_declare) noteInput.value = piece.lien_declare;
    noteCell.append(noteInput);
    tr.append(noteCell);

    tr._controls = { select, checkbox, noteInput, piece };
    return tr;
  }

  function render(data) {
    current = data;
    panel.dataset.dossierId = data.dossier_id; // read by the browser recette; harmless (own staging row's id, already implied by the table shown)
    clear(rowsEl);
    data.pieces.forEach((p) => rowsEl.append(renderRow(p)));
    clear(rejectedEl);
    if (data.rejetees && data.rejetees.length) {
      rejectedEl.hidden = false;
      rejectedEl.append(el("div", { className: "callout callout-warning" }, [
        el("strong", { text: "Pièce(s) non stockée(s) — format non pris en charge :" }),
        el("ul", { style: "margin:6px 0 0 18px" }, data.rejetees.map((r) => el("li", { text: (r.category_label || "Pièce") + " « " + r.piece + " » — " + r.message }))),
      ]));
    } else {
      rejectedEl.hidden = true;
    }
    if (!data.au_moins_une_piece_exploitable) {
      showError("Aucune pièce sûre et exploitable n'a été retenue par défaut : incluez explicitement au moins une pièce, ou modifiez la sélection.");
    } else {
      errorEl.hidden = true; clear(errorEl);
    }
    statusEl.textContent = "";
    formPanel.hidden = true;
    panel.hidden = false;
    panel.scrollIntoView({ block: "start", behavior: "instant" });
  }

  function showError(text) {
    clear(errorEl);
    errorEl.append(el("div", { text }));
    errorEl.hidden = false;
    errorEl.focus();
  }

  function decisions() {
    return Array.from(rowsEl.children).map((tr) => {
      const { select, checkbox, noteInput, piece } = tr._controls;
      const note = noteInput.value.trim();
      const out = { piece_id: piece.id, content_hash: piece.empreinte, include: checkbox.checked, category_final: select.value };
      if (note) out.link_note = note;
      return out;
    });
  }

  if (backBtn) backBtn.addEventListener("click", () => { panel.hidden = true; formPanel.hidden = false; });

  if (confirmBtn) confirmBtn.addEventListener("click", async () => {
    if (!current) return;
    errorEl.hidden = true;
    confirmBtn.disabled = true;
    statusEl.textContent = "Confirmation en cours…";
    let res, body;
    try {
      res = await fetch(`/api/analyze/dossier-preview/${encodeURIComponent(current.dossier_id)}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": window.wmCsrfToken() },
        body: JSON.stringify({ decisions: decisions() }),
      });
      body = await res.json().catch(() => ({}));
    } catch {
      statusEl.textContent = "";
      confirmBtn.disabled = false;
      showError("Impossible de contacter le serveur WinMarket AI.");
      return;
    }
    statusEl.textContent = "";
    confirmBtn.disabled = false;
    if (!res.ok) {
      showError(window.wmErrorMessage(body, "La confirmation a été refusée."));
      const code = body && body.detail && body.detail.error_code;
      if (code === "STALE_PREVIEW" || code === "PREVIEW_EXPIRED") {
        // Nothing was written — the user must submit the dossier again to get a fresh, valid preview.
        statusEl.textContent = "Renvoyez le dossier pour obtenir un nouvel aperçu.";
      }
      return;
    }
    // The preview is consumed: hide it, so a second click can never re-send the same confirmation (404).
    panel.hidden = true;
    current = null;
    if (typeof onConfirmed === "function") onConfirmed(body.job_id);
  });

  window.WMDossierPreview = {
    show(data, confirmedCallback) { onConfirmed = confirmedCallback; render(data); },
  };
})();
