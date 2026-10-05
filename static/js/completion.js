// WinMarket AI — "Compléter les informations" (lot 49): guided completion of an analysis, from the
// server's own needs contract (GET /api/analyze/{job}/completion) to a new, linked revision
// (POST /api/analyze/{job}/complete). Every name/value is written with textContent (never innerHTML).
// No score, decision, weight or threshold is ever editable here — only data.
(function () {
  "use strict";

  const jobId = window.WM_JOB_ID;
  const section = document.getElementById("completion-section");
  if (!jobId || !section || window.WM_HAS_REVISION) return; // a revision already exists: the server-rendered banner covers it

  const openBtn = document.getElementById("completion-open");
  const closeBtn = document.getElementById("completion-close");
  const panel = document.getElementById("completion-panel");
  const needsEl = document.getElementById("completion-needs");
  const capacityEl = document.getElementById("completion-capacity");
  const errorEl = document.getElementById("completion-error");
  const statusEl = document.getElementById("completion-status");
  const submitBtn = document.getElementById("completion-submit");

  const SUBJECT_LABEL = { ao: "Appel d'offres", acheteur: "Acheteur", prestataire: "Votre profil", politique: "Politique de scoring", references: "Références" };
  // Lot 52 — needs eligible for "Chercher dans mes documents" (src/agents/fact_search.py's own whitelist).
  const SEARCHABLE_ACTIONS = new Set(["declare_prestataire", "declare_ao", "declare_acheteur"]);
  const SEARCH_STATUS_LABEL = {
    absent: "Aucune valeur trouvée dans vos documents pour cette information.",
    no_source: "Aucune source consultable pour cette information (aucun document, ou aucune pièce d'appel d'offres disponible).",
    no_candidates: "Aucun passage suffisamment proche n'a été trouvé.",
    llm_unavailable: "Le service de recherche n'est pas disponible actuellement.",
    llm_invalid_response: "La recherche n'a pas produit de réponse exploitable.",
    unknown_need: "Cette information n'est plus attendue pour cette analyse.",
  };
  let state = null; // the last GET /completion response
  const inputs = {}; // need_id -> {get: () => value|undefined, getSourceProposal: () => object|undefined}

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => { if (c !== null && c !== undefined) node.append(c); });
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function showError(text) {
    clear(errorEl);
    errorEl.append(el("div", { text }));
    errorEl.hidden = false;
    errorEl.focus();
  }
  function clearError() { errorEl.hidden = true; clear(errorEl); }

  /* ── one need -> its input row ── */
  function fieldInput(need) {
    if (need.type === "number") {
      return el("input", { type: "number", className: "input", "aria-label": need.label, step: "any" });
    }
    if (need.type === "boolean") {
      const wrap = el("label", { style: "display:flex;align-items:center;gap:8px" });
      const box = el("input", { type: "checkbox" });
      wrap.append(box, el("span", { text: "Oui" }));
      wrap.__value = () => box.checked;
      return wrap;
    }
    if (need.type === "list") {
      const area = el("textarea", { className: "textarea", "aria-label": need.label, placeholder: "Un élément par ligne", style: "min-height:70px" });
      area.__value = () => area.value.split("\n").map((s) => s.trim()).filter(Boolean);
      return area;
    }
    return el("input", { type: "text", className: "input", "aria-label": need.label, maxLength: 500 });
  }

  function needValue(need, node) {
    if (typeof node.__value === "function") return node.__value();
    if (node.tagName === "INPUT" && node.type === "number") return node.value.trim() === "" ? undefined : Number(node.value);
    return node.value.trim() === "" ? undefined : node.value;
  }

  /* Lot 52 — writes a proposed value into a need's own input, whatever its type/shape. */
  function setInputValue(need, node, value) {
    if (need.type === "boolean") { node.querySelector("input").checked = !!value; return; }
    if (need.type === "list") { node.value = Array.isArray(value) ? value.join("\n") : ""; return; }
    node.value = value === null || value === undefined ? "" : String(value);
  }

  function renderDeclarable(need) {
    const row = el("div", { className: "field", "data-need": need.id, style: "margin-bottom:14px;padding:12px;border:1px solid var(--border);border-radius:var(--radius-md)" });
    row.append(el("label", { text: `${need.label} — ${SUBJECT_LABEL[need.subject] || need.subject}` }));
    if (need.criteria && need.criteria.length > 1) {
      row.append(el("p", { className: "field-hint", text: `Concerne ${need.criteria.length} critères de votre politique.` }));
    }
    if (need.note) row.append(el("p", { className: "field-hint", text: need.note }));
    const input = fieldInput(need);
    row.append(input);
    const skip = el("label", { style: "display:flex;align-items:center;gap:6px;margin-top:8px;font-size:.82rem" }, [
      el("input", { type: "checkbox", on: { change: (e) => { input.disabled = e.target.checked; if (e.target.checked && input.value !== undefined) input.value = ""; } } }),
      el("span", { text: "Je ne sais pas" }),
    ]);
    row.append(skip);

    // Lot 52 — "Chercher dans mes documents": a proposal is accepted UNMODIFIED (acceptedProposal is
    // re-sent as-is and re-verified server-side) or cleared the instant the user edits the field by hand —
    // never presented as still supporting a value the user has since changed.
    let acceptedProposal = null;
    const clearAcceptedOnEdit = () => { acceptedProposal = null; };
    input.addEventListener("input", clearAcceptedOnEdit);
    const innerCheckbox = input.querySelector && input.querySelector("input");
    if (innerCheckbox) innerCheckbox.addEventListener("change", clearAcceptedOnEdit);

    if (SEARCHABLE_ACTIONS.has(need.action)) {
      const searchBtn = el("button", {
        type: "button", className: "btn btn-secondary btn-sm", style: "margin-top:8px", text: "Chercher dans mes documents",
      });
      const resultBox = el("div", { style: "margin-top:8px" });
      searchBtn.addEventListener("click", async () => {
        searchBtn.disabled = true;
        searchBtn.textContent = "Recherche…";
        clear(resultBox);
        let body, ok = false;
        try {
          const res = await fetch(`/api/analyze/${jobId}/completion/search-facts`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": window.wmCsrfToken() },
            body: JSON.stringify({ need_ids: [need.id] }),
          });
          body = await res.json().catch(() => ({}));
          ok = res.ok;
        } catch {
          body = null;
        }
        searchBtn.disabled = false;
        searchBtn.textContent = "Chercher à nouveau";
        if (!ok || !body) {
          resultBox.append(el("p", { className: "field-hint", text: window.wmErrorMessage(body, "La recherche a échoué.") }));
          return;
        }
        const result = (body.results || [])[0];
        if (!result || result.status !== "proposed") {
          resultBox.append(el("p", { className: "field-hint", text: (result && SEARCH_STATUS_LABEL[result.status]) || "Aucune valeur trouvée." }));
          return;
        }
        const box = el("div", { className: "callout callout-info" });
        const sourceLabel = (result.source && result.source.source_label) || "un de vos documents";
        box.append(el("p", { style: "font-size:.85rem", text: `Proposition trouvée dans « ${sourceLabel} » — citation retrouvable : « ${result.citation} »` }));
        box.append(el("p", { className: "field-hint", text:
          "Une citation retrouvable dans vos documents, pas une certification indépendante ni une interprétation déjà validée — vérifiez avant d'accepter." }));
        const acceptBtn = el("button", { type: "button", className: "btn btn-primary btn-sm", text: "Accepter cette proposition" });
        const rejectBtn = el("button", { type: "button", className: "btn btn-secondary btn-sm", style: "margin-left:8px", text: "Rejeter" });
        acceptBtn.addEventListener("click", () => {
          setInputValue(need, input, result.value);
          acceptedProposal = result;
          skip.querySelector("input").checked = false;
          input.disabled = false;
          box.remove();
        });
        rejectBtn.addEventListener("click", () => { acceptedProposal = null; box.remove(); });
        box.append(el("div", { style: "margin-top:6px" }, [acceptBtn, rejectBtn]));
        resultBox.append(box);
      });
      row.append(searchBtn, resultBox);
    }

    inputs[need.id] = {
      need,
      get: () => (skip.querySelector("input").checked ? undefined : needValue(need, input)),
      getSourceProposal: () => {
        if (!acceptedProposal) return undefined;
        const current = needValue(need, input);
        // Forwarded only when the input still holds EXACTLY the accepted proposal's own value — a manual
        // edit already cleared acceptedProposal above; this is defense in depth, not the only guard.
        return JSON.stringify(current) === JSON.stringify(acceptedProposal.value) ? acceptedProposal : undefined;
      },
    };
    return row;
  }

  function renderConflict(need) {
    const box = el("div", { className: "callout callout-warning", style: "margin-bottom:14px" });
    box.append(el("div", {}, [el("strong", { text: need.label })]));
    box.append(el("p", { style: "font-size:.85rem;margin-top:6px", text:
      "Deux pièces de ce dossier donnent des valeurs différentes : aucune n'est retenue. Corrigez les pièces "
      + "en désaccord puis lancez une nouvelle analyse de dossier — cette information ne peut pas être choisie ici." }));
    if (need.current && Array.isArray(need.current.valeurs)) {
      const list = el("ul", { style: "margin:6px 0 0 18px;font-size:.82rem" });
      need.current.valeurs.forEach((v) => list.append(el("li", { text: String(v.valeur) })));
      box.append(list);
    }
    return box;
  }

  function renderPolicy(need) {
    return el("div", { className: "callout callout-info", style: "margin-bottom:14px" }, [
      el("strong", { text: need.label }),
      el("p", { style: "font-size:.85rem;margin-top:4px" }, [
        el("a", { href: "/app/parametres", text: "Configurer dans Paramètres de scoring" }),
      ]),
    ]);
  }

  function renderInformational(need) {
    const box = el("div", { className: "callout callout-info", style: "margin-bottom:14px" });
    box.append(el("strong", { text: need.label }));
    if (need.note) box.append(el("p", { style: "font-size:.85rem;margin-top:4px", text: need.note }));
    if (need.action === "add_reference") box.append(el("p", { style: "margin-top:6px" }, [el("a", { href: "/app/base-connaissances", text: "Aller à la base de connaissances" })]));
    return box;
  }

  function renderCapacity(capacity) {
    clear(capacityEl);
    if (!capacity || !capacity.current || !capacity.changed) return;
    const box = el("div", { className: "callout callout-info", style: "margin:14px 0" });
    box.append(el("div", {}, [el("strong", { text: "Disponibilité de l'équipe" })]));
    box.append(el("p", { style: "font-size:.85rem;margin-top:6px", text:
      `Enregistrée pour cette analyse : ${capacity.frozen ? (capacity.frozen.equipe_disponible ? "disponible" : "non disponible") : "–"} · `
      + `Actuellement enregistrée : ${capacity.current.equipe_disponible ? "disponible" : "non disponible"} (${capacity.current.capacite_restante_pct}% restant).` }));
    const label = el("label", { style: "display:flex;align-items:center;gap:8px;margin-top:6px" }, [
      el("input", { type: "checkbox", id: "completion-apply-capacity" }),
      el("span", { text: "Utiliser ma disponibilité actuellement enregistrée pour cette révision" }),
    ]);
    box.append(label);
    capacityEl.append(box);
  }

  function needsProviderConfirmation() {
    return Object.values(inputs).some((r) => r.need.action === "declare_prestataire");
  }

  function render(data) {
    state = data;
    clear(needsEl);
    Object.keys(inputs).forEach((k) => delete inputs[k]);
    const declarable = data.needs.filter((n) => n.kind === "declarable");
    const conflicts = data.needs.filter((n) => n.kind === "conflict");
    const policy = data.needs.filter((n) => n.kind === "policy");
    const info = data.needs.filter((n) => n.kind === "informational");

    declarable.forEach((n) => needsEl.append(renderDeclarable(n)));
    conflicts.forEach((n) => needsEl.append(renderConflict(n)));
    policy.forEach((n) => needsEl.append(renderPolicy(n)));
    info.forEach((n) => needsEl.append(renderInformational(n)));

    if (needsProviderConfirmation()) {
      needsEl.append(el("label", { style: "display:flex;align-items:center;gap:8px;margin-top:6px", id: "completion-confirm-profile-row" }, [
        el("input", { type: "checkbox", id: "completion-confirm-profile" }),
        el("span", { text: "J'autorise l'enregistrement permanent de ces valeurs dans mon profil (Paramètres de scoring)." }),
      ]));
    }
    renderCapacity(data.capacity);

    const actionable = declarable.length > 0 || (data.capacity && data.capacity.changed);
    if (!actionable && conflicts.length === 0 && policy.length === 0 && info.length === 0) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    submitBtn.hidden = !actionable;
    if (data.reason === "revision_already_exists" || !data.can_complete) {
      submitBtn.hidden = true;
    }
  }

  async function load() {
    try {
      const res = await fetch(`/api/analyze/${jobId}/completion`);
      if (!res.ok) return;
      render(await res.json());
    } catch {
      // A network failure here must never break the already-shown result — the button simply stays hidden.
    }
  }

  if (openBtn) openBtn.addEventListener("click", () => { panel.hidden = false; openBtn.hidden = true; });
  if (closeBtn) closeBtn.addEventListener("click", () => { panel.hidden = true; openBtn.hidden = false; clearError(); });

  async function pollNewJob(newJobId) {
    for (let i = 0; i < 300; i += 1) {
      let body;
      try {
        const res = await fetch(`/api/analyze/${newJobId}/status`);
        body = await res.json();
      } catch {
        statusEl.textContent = "Impossible de contacter le serveur pour suivre le complément.";
        return;
      }
      if (body.status === "done") { window.location.href = `/app/resultats/${newJobId}`; return; }
      if (body.status === "error") {
        showError(window.wmErrorMessage(body, "Le complément n'a pas pu être calculé."));
        statusEl.textContent = "";
        return;
      }
      statusEl.textContent = body.message || "Recalcul en cours…";
      await new Promise((r) => setTimeout(r, 800));
    }
  }

  if (submitBtn) {
    submitBtn.addEventListener("click", async () => {
      clearError();
      const items = [];
      for (const { need, get, getSourceProposal } of Object.values(inputs)) {
        const value = get();
        if (value === undefined) continue;
        const item = { need_id: need.id, value };
        const proposal = getSourceProposal && getSourceProposal();
        if (proposal) item.source_proposal = proposal;
        items.push(item);
      }
      const applyCapEl = document.getElementById("completion-apply-capacity");
      const confirmProfileEl = document.getElementById("completion-confirm-profile");
      const confirmProfileWrite = !!(confirmProfileEl && confirmProfileEl.checked);
      const payload = {
        items,
        apply_current_capacity: !!(applyCapEl && applyCapEl.checked),
        confirm_profile_write: confirmProfileWrite,
        // Lot 49 bis: the profile version shown in THIS preview — the server refuses (409) instead of
        // writing anything if the profile changed since this was fetched, rather than silently merging into
        // a profile the account never actually saw.
        expected_profile_version: confirmProfileWrite ? (state && state.profile_version) : undefined,
      };
      if (items.length === 0 && !payload.apply_current_capacity) {
        showError("Renseignez au moins une information, ou cochez la mise à jour de disponibilité, avant de recalculer.");
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = "Envoi…";
      let res, body;
      try {
        res = await fetch(`/api/analyze/${jobId}/complete`, {
          method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": window.wmCsrfToken() },
          body: JSON.stringify(payload),
        });
        body = await res.json().catch(() => ({}));
      } catch {
        showError("Impossible de contacter le serveur WinMarket AI.");
        submitBtn.disabled = false;
        submitBtn.textContent = "Recalculer avec ces informations";
        return;
      }
      if (!res.ok) {
        showError(window.wmErrorMessage(body, "Le complément a été refusé."));
        if (body && (body.error_code === "PROFILE_CHANGED" || body.error_code === "PROFILE_VERSION_REQUIRED")) {
          // Lot 49 bis: nothing was written (refused before any write) — refresh the preview (fresh needs +
          // fresh profile_version) so a retry starts from what the account's profile actually is now.
          load();
        }
        if (body && body.error_code === "SOURCE_CHANGED") {
          // Lot 52: the accepted proposal's source changed/disappeared since it was proposed — nothing was
          // written; the panel is refreshed so the account re-searches or re-declares by hand.
          load();
        }
        submitBtn.disabled = false;
        submitBtn.textContent = "Recalculer avec ces informations";
        return;
      }
      submitBtn.textContent = "Recalcul en cours…";
      statusEl.textContent = "Recalcul en cours…";
      await pollNewJob(body.job_id);
      submitBtn.disabled = false;
      submitBtn.textContent = "Recalculer avec ces informations";
    });
  }

  load();
})();
