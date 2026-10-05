// WinMarket AI — "Paramètres de scoring" page (B27-T1 / B27-T2).
//
// The server is the only authority for validation, calculation and rights:
// this file describes a configuration (profile, business facts, custom
// criteria, weights) and calls the existing /api/scoring-config/* routes,
// then shows exactly what they return. It never scores, never normalizes a
// weight, never fills a business value in on the user's behalf, and never
// treats 0 / false / an empty list as "missing" (absent means null).
//
// Every server-supplied string is written with textContent, never innerHTML.
(function () {
  "use strict";

  const profileForm = document.getElementById("profile-form");
  if (!profileForm) return; // not on this page

  const ORG = window.WM_ORGANIZATION_ID || "";
  const CATALOGUE = window.WM_CATALOGUE || { fact_types: [], operators: [], numeric_comparisons: [], operator_fact_types: {}, unit_fact_types: [] };
  const profile = window.WM_PROFILE || {};
  let activePolicy = window.WM_ACTIVE_POLICY;
  let draftPolicy = window.WM_DRAFT_POLICY;
  const canConfigure = !document.getElementById("add-fact").disabled;

  const $ = (id) => document.getElementById(id);
  const saveFeedback = $("save-feedback");
  const saveError = $("save-error");

  const FACT_TYPE_LABELS = { number: "Nombre", list: "Liste", boolean: "Oui / non", text: "Texte" };
  const GROUP_LABELS = {
    weights: "Pondérations", thresholds: "Seuils", profile: "Profil", business_rules: "Règles métier",
    business_facts: "Faits métier", custom_criteria: "Critères personnalisés", criteria: "Critères", settings: "Réglages",
  };

  /* ── DOM helper (no innerHTML anywhere) ── */
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

  function format(n) { return String(Math.round(n * 1000) / 1000); }

  /* ── Feedback ── */
  let feedbackTimer = null;
  function showFeedback(message) {
    saveError.hidden = true;
    saveFeedback.hidden = false;
    saveFeedback.textContent = message;
    clearTimeout(feedbackTimer);
    feedbackTimer = setTimeout(() => (saveFeedback.hidden = true), 6000);
  }
  function showError(message, withLoginLink) {
    saveFeedback.hidden = true;
    saveError.hidden = false;
    clear(saveError);
    saveError.append(document.createTextNode(message));
    if (withLoginLink) {
      saveError.append(document.createTextNode(" "), el("a", { href: "/login?next=/app/parametres", text: "Se reconnecter" }));
    }
  }

  /* ── Dirty state ── */
  let dirty = false;
  function setDirty(value) {
    dirty = value;
    $("dirty-banner").hidden = !value;
  }

  /* ── Organization scope: every request names its organization explicitly,
     and any response that belongs to a previous scope is ignored. ── */
  let epoch = 0;

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  // False when the selected organization changed (another tab or the
  // switcher) since this page was rendered: nothing may then be written
  // under the wrong organization.
  function scopeIsCurrent() {
    const current = cookie("wm_org_id");
    if (current && ORG && current !== ORG) {
      showError("L'organisation active a changé depuis l'affichage de cette page — rechargez la page avant de continuer.");
      return false;
    }
    return true;
  }

  async function api(method, path, options) {
    const myEpoch = epoch;
    const headers = { "X-CSRF-Token": window.wmCsrfToken() };
    const init = { method, headers };
    if (options && options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.json);
    } else if (options && options.form) {
      init.body = options.form; // multipart: the browser sets the boundary
    }
    const url = path + (path.includes("?") ? "&" : "?") + "organization_id=" + encodeURIComponent(ORG);
    let res;
    try {
      res = await fetch(url, init);
    } catch {
      return myEpoch === epoch ? { networkError: true } : { stale: true };
    }
    const body = await res.json().catch(() => ({}));
    if (myEpoch !== epoch) return { stale: true };
    return { res, status: res.status, ok: res.ok, body };
  }

  // A short, safe explanation and the useful next action for an HTTP error
  // (never a path, a secret or an exception).
  function explain(r, fallback) {
    if (r.networkError) return { text: "Impossible de contacter le serveur WinMarket AI — réessayez." };
    if (r.status === 401) return { text: "Votre session a expiré.", login: true };
    if (r.status === 403) return { text: window.wmErrorMessage(r.body, "Action non autorisée pour votre rôle ou votre organisation.") };
    if (r.status === 409) return { text: window.wmErrorMessage(r.body, "Conflit : l'état a changé — rechargez la page.") };
    if (r.status === 413) return { text: window.wmErrorMessage(r.body, "Le contenu envoyé est trop volumineux.") };
    if (r.status === 429) return { text: window.wmErrorMessage(r.body, "Trop de requêtes — patientez un instant avant de réessayer.") };
    if (r.status === 503) return { text: "Service momentanément indisponible — réessayez dans un instant." };
    return { text: window.wmErrorMessage(r.body, fallback) };
  }
  function showFailure(r, fallback) {
    const { text, login } = explain(r, fallback);
    showError(text, login);
  }

  /* ── Double-submission guard ── */
  function guarded(button, busyLabel, fn) {
    return async (...args) => {
      if (button.disabled) return;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = busyLabel;
      try {
        await fn(...args);
      } finally {
        button.disabled = !canConfigure;
        button.textContent = original;
      }
    };
  }

  /* ── Slugs: a stable identifier proposed from a label (editable until saved) ── */
  function slugify(text) {
    let s = String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
    if (s && !/^[a-z]/.test(s)) s = "f_" + s;
    return s;
  }
  function uniqueIdentifier(base, taken) {
    if (!base) return "";
    let candidate = base;
    let n = 2;
    while (taken.has(candidate)) candidate = base.slice(0, 58) + "_" + n++;
    return candidate;
  }

  /* ════════════════ Certifications ════════════════ */
  const certList = $("certifications-list");
  function certRow(cert) {
    cert = cert || { nom: "", statut: "declaree", preuve_reference: "" };
    const nom = el("input", { className: "input", type: "text", placeholder: "Nom de la certification", disabled: !canConfigure, value: cert.nom || "" });
    nom.dataset.cert = "nom";
    const statut = el("select", { className: "select", disabled: !canConfigure }, [
      el("option", { value: "declaree", text: "Déclarée" }), el("option", { value: "verifiee", text: "Vérifiée" }),
    ]);
    statut.dataset.cert = "statut";
    statut.value = cert.statut === "verifiee" ? "verifiee" : "declaree";
    const preuve = el("input", { className: "input", type: "text", placeholder: "Référence de preuve (id document, facultatif)", disabled: !canConfigure, value: cert.preuve_reference || "" });
    preuve.dataset.cert = "preuve_reference";
    const row = el("div", { className: "field", style: "display:grid;grid-template-columns:2fr 1fr 2fr auto;gap:8px;align-items:center" });
    const remove = el("button", { type: "button", className: "btn btn-secondary", text: "✕", disabled: !canConfigure, "aria-label": "Retirer cette certification", on: { click: () => row.remove() } });
    row.append(nom, statut, preuve, remove);
    return row;
  }
  (profile.certifications || []).forEach((c) => certList.appendChild(certRow(c)));
  $("add-certification").addEventListener("click", () => certList.appendChild(certRow()));
  function readCertifications() {
    return Array.from(certList.children).map((row) => ({
      nom: row.querySelector('[data-cert="nom"]').value.trim(),
      statut: row.querySelector('[data-cert="statut"]').value,
      preuve_reference: row.querySelector('[data-cert="preuve_reference"]').value.trim() || null,
    })).filter((c) => c.nom);
  }

  /* ════════════════ Business facts editor ════════════════ */
  const factsList = $("facts-list");

  // The value control depends on the fact type. An empty control means "not
  // declared" (null); 0 and false are real values and are kept. A saved
  // value that does not fit its type (e.g. the text "inf" in a number fact)
  // is shown as-is, flagged, and sent back unchanged so the server reports it
  // — it is never silently dropped or coerced.
  function valueControl(type, value) {
    const disabled = !canConfigure;
    const hasValue = value !== null && value !== undefined;
    if (type === "number" && (!hasValue || (typeof value === "number" && Number.isFinite(value)))) {
      const input = el("input", { className: "input", type: "number", step: "any", placeholder: "Valeur", disabled, value: hasValue ? String(value) : "" });
      input.readValue = () => (input.value.trim() === "" ? null : Number(input.value));
      return input;
    }
    if (type === "list" && (!hasValue || (Array.isArray(value) && value.every((v) => typeof v === "string")))) {
      const area = el("textarea", { className: "textarea", placeholder: "Une valeur par ligne", style: "min-height:60px", disabled, value: hasValue ? value.join("\n") : "" });
      area.readValue = () => {
        const items = area.value.split("\n").map((s) => s.trim()).filter(Boolean);
        return items.length ? items : null;
      };
      return area;
    }
    if (type === "boolean" && (!hasValue || typeof value === "boolean")) {
      const select = el("select", { className: "select", disabled }, [
        el("option", { value: "", text: "Non renseigné" }), el("option", { value: "true", text: "Oui" }), el("option", { value: "false", text: "Non" }),
      ]);
      select.value = hasValue ? String(value) : "";
      select.readValue = () => (select.value === "" ? null : select.value === "true");
      return select;
    }
    if (type === "text" && (!hasValue || typeof value === "string")) {
      const input = el("input", { className: "input", type: "text", placeholder: "Valeur", disabled, value: hasValue ? value : "" });
      input.readValue = () => (input.value.trim() === "" ? null : input.value);
      return input;
    }
    // Invalid or type-less: keep the raw value visible and unchanged.
    const raw = el("input", { className: "input", type: "text", disabled, value: hasValue ? (typeof value === "string" ? value : JSON.stringify(value)) : "", title: "Valeur invalide pour ce type — corrigez-la" });
    raw.style.borderColor = "var(--nogo)";
    raw.readValue = () => (hasValue && typeof value !== "string" ? value : (raw.value === "" ? null : raw.value));
    return raw;
  }

  function factRows() { return Array.from(factsList.children); }
  function factKeysTaken(exceptRow) {
    return new Set(factRows().filter((r) => r !== exceptRow).map((r) => r.querySelector('[data-f="key"]').value.trim()).filter(Boolean));
  }

  function factRow(fact) {
    fact = fact || {};
    const saved = Boolean(fact.key) && Boolean(profile.business_facts && profile.business_facts[fact.key]);
    const disabled = !canConfigure;
    const label = el("input", { className: "input", type: "text", placeholder: "Libellé (ex. Zone d'intervention)", disabled, value: fact.label || "" });
    label.dataset.f = "label";
    const key = el("input", { className: "input font-mono", type: "text", placeholder: "identifiant_stable", disabled, value: fact.key || "", readOnly: saved, title: saved ? "Identifiant verrouillé : il est référencé par vos critères" : "Identifiant stable, non modifiable une fois enregistré" });
    key.dataset.f = "key";
    const typeSelect = el("select", { className: "select", disabled }, (CATALOGUE.fact_types || []).map((t) => el("option", { value: t, text: FACT_TYPE_LABELS[t] || t })));
    typeSelect.dataset.f = "type";
    const knownType = (CATALOGUE.fact_types || []).includes(fact.type);
    if (fact.type && !knownType) typeSelect.prepend(el("option", { value: String(fact.type), text: "Type non pris en charge : " + String(fact.type) }));
    typeSelect.value = fact.type ? String(fact.type) : (CATALOGUE.fact_types || [])[0] || "";
    const unit = el("input", { className: "input", type: "text", placeholder: "Unité (ex. par_semaine)", disabled, value: fact.unit || "" });
    unit.dataset.f = "unit";
    const valueBox = el("div");
    valueBox.dataset.f = "value";
    const row = el("div", { className: "card card-tight", style: "margin-bottom:8px" });
    if (saved) row.dataset.savedKey = fact.key;

    const remove = el("button", { type: "button", className: "btn btn-secondary", text: "✕", disabled, "aria-label": "Retirer ce fait", on: { click: () => { row.remove(); refreshCriteriaFacts(); updateTotal(); setDirty(true); } } });

    function syncType(initialValue) {
      const type = typeSelect.value;
      const takesUnit = (CATALOGUE.unit_fact_types || []).includes(type);
      unit.disabled = disabled || !takesUnit;
      if (!takesUnit) unit.value = "";
      clear(valueBox);
      const control = valueControl(type, initialValue);
      valueBox.append(control);
      row.valueControl = control;
    }
    typeSelect.addEventListener("change", () => { syncType(undefined); refreshCriteriaFacts(); });
    label.addEventListener("input", () => {
      if (!key.readOnly && !key.dataset.touched) {
        key.value = uniqueIdentifier(slugify(label.value), factKeysTaken(row));
      }
      refreshCriteriaFacts();
    });
    key.addEventListener("input", () => { key.dataset.touched = "1"; refreshCriteriaFacts(); });

    row.append(
      el("div", { style: "display:grid;grid-template-columns:1.4fr 1.2fr 1fr auto;gap:8px;align-items:start" }, [label, key, typeSelect, remove]),
      el("div", { style: "display:grid;grid-template-columns:1fr 2fr;gap:8px;margin-top:8px;align-items:start" }, [unit, valueBox]),
    );
    syncType(fact.value);
    return row;
  }

  // The declared catalogue as the form currently describes it.
  function readFacts() {
    const facts = {};
    const duplicates = [];
    factRows().forEach((row) => {
      const labelText = row.querySelector('[data-f="label"]').value.trim();
      let key = row.querySelector('[data-f="key"]').value.trim();
      const type = row.querySelector('[data-f="type"]').value;
      const unitText = row.querySelector('[data-f="unit"]').value.trim();
      const value = row.valueControl ? row.valueControl.readValue() : null;
      if (!key && !labelText && value === null && !unitText) return; // an untouched row is not a fact
      if (!key) key = uniqueIdentifier(slugify(labelText), new Set(Object.keys(facts)));
      if (Object.prototype.hasOwnProperty.call(facts, key)) duplicates.push(key);
      facts[key] = { key, label: labelText, type, unit: unitText || null, value };
    });
    return { facts, duplicates };
  }

  // JSON objects carry no reliable order (the template serializer sorts
  // keys, PostgreSQL JSONB reorders them): show a stable, predictable order.
  Object.values(profile.business_facts || {})
    .sort((a, b) => String((a && a.label) || (a && a.key) || "").localeCompare(String((b && b.label) || (b && b.key) || ""), "fr"))
    .forEach((f) => factsList.appendChild(factRow(f)));
  $("add-fact").addEventListener("click", () => { factsList.appendChild(factRow()); setDirty(true); });

  /* ════════════════ Criteria editor (explicit criteria, schema v1) ════════════════
     A new policy starts EMPTY. Each criterion = an evaluator from the server's
     closed catalogue + its parameters + weight + blocking + unknown-data rule.
     Nothing is prefilled; the server validates and calculates. */
  const CAT = window.WM_CRITERIA_CATALOGUE || { evaluators: {}, unavailable: [] };
  const EVALUATORS = CAT.evaluators || {};
  const TEMPLATES = window.WM_CRITERIA_TEMPLATES || [];
  // With no draft, the form starts from the account's OWN active policy (saving then creates a new draft version) — never from defaults.
  const basePolicy = draftPolicy || activePolicy;
  $("base-notice").hidden = !(!draftPolicy && activePolicy);
  const criteriaList = $("criteria-list");
  const ON_MISSING_LABELS = {
    incomplete: "Non évalué : l'analyse est incomplète (recommandé)",
    explicit_score: "Appliquer une note que je choisis (hypothèse visible)",
    not_applicable: "Déclarer non applicable pour cette analyse",
  };
  const FAMILY_LABELS = { fact: "fait métier", structural: "donnée de l'AO", legacy: "règle historique" };

  const originNotice = $("origin-notice");
  if (basePolicy && basePolicy.origin === "legacy") {
    originNotice.hidden = false;
    originNotice.textContent = "Politique historique migrée : ses critères, notes et règles sont des valeurs de compatibilité reprises telles quelles — "
      + "ce n'est pas un choix que vous avez fait. Vous pouvez les revoir ici ; enregistrer crée un brouillon au nouveau format, "
      + "et la politique active ne change qu'à l'activation.";
  }

  function criterionRows() { return Array.from(criteriaList.children); }
  function factOptions() { return Object.values(readFacts().facts).filter((f) => f.key); }

  function fillFactSelect(select, selectedKey) {
    clear(select);
    select.append(el("option", { value: "", text: "— choisir un fait —" }));
    let found = false;
    factOptions().forEach((f) => {
      select.append(el("option", { value: f.key, text: (f.label || f.key) + " (" + f.key + ")" }));
      if (f.key === selectedKey) found = true;
    });
    if (selectedKey && !found) select.append(el("option", { value: selectedKey, text: "Fait introuvable : " + selectedKey }));
    select.value = selectedKey || "";
  }

  // One control per parameter type; `read()` returns the JSON value (null = not provided).
  function paramControl(pspec, value) {
    const disabled = !canConfigure;
    const has = value !== null && value !== undefined;
    const numeric = (input) => () => (input.value.trim() === "" ? null : Number(input.value));
    if (["score", "pct", "amount", "count"].includes(pspec.type)) {
      const input = el("input", { className: "input", type: "number", step: pspec.type === "count" ? "1" : "any", min: "0", disabled, value: has ? String(value) : "" });
      return { node: input, read: numeric(input) };
    }
    if (pspec.type === "bool") {
      const select = el("select", { className: "select", disabled }, [
        el("option", { value: "", text: "— choisir —" }), el("option", { value: "true", text: "Oui" }), el("option", { value: "false", text: "Non" }),
      ]);
      select.value = has ? String(value) : "";
      return { node: select, read: () => (select.value === "" ? null : select.value === "true") };
    }
    if (pspec.type === "choice") {
      const select = el("select", { className: "select", disabled }, [el("option", { value: "", text: "— choisir —" })]
        .concat((pspec.choices || []).map((c) => el("option", { value: c, text: c }))));
      select.value = has ? String(value) : "";
      select.dataset.choice = "1";
      return { node: select, read: () => (select.value === "" ? null : select.value), select };
    }
    if (pspec.type === "fact_key") {
      const select = el("select", { className: "select", disabled });
      fillFactSelect(select, has ? String(value) : "");
      select.dataset.factSelect = "1";
      return { node: select, read: () => (select.value === "" ? null : select.value), select };
    }
    if (pspec.type === "text_list") {
      const area = el("textarea", { className: "textarea", style: "min-height:60px", placeholder: "Un mot par ligne", disabled, value: has && Array.isArray(value) ? value.join("\n") : "" });
      return { node: area, read: () => { const items = area.value.split("\n").map((s) => s.trim()).filter(Boolean); return items.length ? items : null; } };
    }
    if (pspec.type === "tiers_at_least" || pspec.type === "tiers_at_most") {
      const key = pspec.type === "tiers_at_least" ? "at_least" : "at_most";
      const box = el("div");
      const addBtn = el("button", { type: "button", className: "btn btn-secondary btn-sm", text: "+ palier", disabled, on: { click: () => { addTier(); setDirty(true); } } });
      const addTier = (tier) => {
        const threshold = el("input", { className: "input", type: "number", step: "any", min: "0", placeholder: key === "at_least" ? "à partir de" : "jusqu'à", disabled, value: tier && tier[key] !== undefined && tier[key] !== null ? String(tier[key]) : "" });
        const note = el("input", { className: "input", type: "number", step: "any", min: "0", max: "100", placeholder: "note (0-100)", disabled, value: tier && tier.score !== undefined && tier.score !== null ? String(tier.score) : "" });
        const line = el("div", { style: "display:grid;grid-template-columns:1fr 1fr auto;gap:6px;margin-bottom:4px" }, [threshold, note,
          el("button", { type: "button", className: "btn btn-secondary", text: "✕", disabled, "aria-label": "Retirer ce palier", on: { click: () => { line.remove(); updateTotal(); setDirty(true); } } })]);
        line.readTier = () => ({ [key]: threshold.value.trim() === "" ? null : Number(threshold.value), score: note.value.trim() === "" ? null : Number(note.value) });
        box.insertBefore(line, addBtn);
      };
      box.append(addBtn);
      (has && Array.isArray(value) ? value : []).forEach(addTier);
      return { node: box, read: () => {
        const tiers = Array.from(box.children).filter((n) => n.readTier).map((n) => n.readTier()).filter((t) => t[key] !== null || t.score !== null);
        return tiers.length ? tiers : null;
      } };
    }
    const fallback = el("input", { className: "input", type: "text", disabled, value: has ? JSON.stringify(value) : "" });
    return { node: fallback, read: () => (fallback.value === "" ? null : fallback.value) };
  }

  function criterionRow(c) {
    c = c || {};
    const disabled = !canConfigure;
    const evaluator = typeof c.evaluator === "string" ? c.evaluator : "";
    const def = EVALUATORS[evaluator];
    const saved = Boolean(c.id) && (((basePolicy && basePolicy.criteria) || []).some((x) => x && x.id === c.id));
    const row = el("div", { className: "card card-tight", style: "margin-bottom:10px", dataset: { evaluator } });
    row.original = c;
    row.controls = {};

    const label = el("input", { className: "input", type: "text", placeholder: "Libellé du critère", disabled, value: typeof c.label === "string" ? c.label : "" });
    label.dataset.c = "label";
    const id = el("input", { className: "input font-mono", type: "text", placeholder: "identifiant_critere", disabled, value: typeof c.id === "string" ? c.id : "", readOnly: saved, title: saved ? "Identifiant verrouillé" : "Identifiant stable" });
    id.dataset.c = "id";
    const weight = el("input", { className: "input", type: "number", step: "any", min: "0", max: "100", placeholder: "Poids (0-100)", disabled, value: c.weight === null || c.weight === undefined ? "" : String(c.weight) });
    weight.dataset.c = "weight";
    weight.addEventListener("input", updateTotal);
    const remove = el("button", { type: "button", className: "btn btn-secondary", text: "✕", disabled, "aria-label": "Retirer ce critère", on: { click: () => { row.remove(); updateTotal(); setDirty(true); } } });
    const small = (text, node) => el("label", { style: "display:flex;flex-direction:column;gap:2px;font-size:.78rem" }, [document.createTextNode(text), node]);

    label.addEventListener("input", () => {
      if (!id.readOnly && !id.dataset.touched) {
        const taken = new Set(criterionRows().filter((r) => r !== row).map((r) => r.querySelector('[data-c="id"]').value.trim()).filter(Boolean));
        id.value = uniqueIdentifier(slugify(label.value), taken);
      }
    });
    id.addEventListener("input", () => { id.dataset.touched = "1"; });

    const title = el("div", { className: "text-tertiary", style: "font-size:.78rem;margin:4px 0" },
      [document.createTextNode((def ? def.label : "Évaluateur non pris en charge : " + evaluator) + (def ? " — " + (FAMILY_LABELS[def.family] || "") : ""))]);
    row.append(el("div", { style: "display:grid;grid-template-columns:1.6fr 1.2fr .8fr auto;gap:8px;align-items:end" }, [small("Libellé", label), small("Identifiant", id), small("Poids", weight), remove]), title);
    if (def && def.description) row.append(el("div", { className: "field-hint", style: "font-size:.76rem;margin-bottom:6px", text: def.description }));
    if (def && def.requires && def.requires.length) row.append(el("div", { className: "field-hint", style: "font-size:.74rem;margin-bottom:6px", text: "Données utilisées : " + def.requires.join(" ; ") }));
    if (def && def.family === "legacy") row.append(el("div", { className: "callout callout-info", style: "font-size:.78rem", text: "Règle historique conservée pour compatibilité : elle détecte des mots dans le texte de l'AO et ne compare pas de faits. Vous pouvez modifier ses notes ou la retirer." }));

    // parameters (generic, from the catalogue)
    const params = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px" });
    ((def && def.params) || []).forEach((p) => {
      const control = paramControl(p, (c.params || {})[p.name]);
      row.controls[p.name] = control;
      control.node.dataset.param = p.name;
      params.append(small(p.label + (p.required ? "" : " (facultatif)"), control.node));
    });
    row.append(params);

    // blocking / enabled / on_missing
    const supportsBlocking = Boolean(def && def.supports_blocking);
    const blocking = el("input", { type: "checkbox", disabled: disabled || !supportsBlocking, checked: supportsBlocking && c.blocking === true });
    blocking.dataset.c = "blocking";
    const enabled = el("input", { type: "checkbox", disabled, checked: c.enabled !== false });
    enabled.dataset.c = "enabled";
    const disabledReason = el("input", { className: "input", type: "text", placeholder: "Motif de désactivation (obligatoire)", disabled, value: typeof c.disabled_reason === "string" ? c.disabled_reason : "" });
    disabledReason.dataset.c = "disabled_reason";
    const onMissing = c.on_missing && typeof c.on_missing === "object" ? c.on_missing : { mode: "incomplete" };
    const mode = el("select", { className: "select", disabled: disabled || !(def && def.missing_ao_data_is_recoverable) }, ["incomplete", "explicit_score", "not_applicable"].map((m) => el("option", { value: m, text: ON_MISSING_LABELS[m] })));
    mode.dataset.c = "on_missing_mode";
    mode.value = ["incomplete", "explicit_score", "not_applicable"].includes(onMissing.mode) ? onMissing.mode : "incomplete";
    const missingScore = el("input", { className: "input", type: "number", step: "any", min: "0", max: "100", placeholder: "Note (0-100)", disabled, value: onMissing.score === undefined || onMissing.score === null ? "" : String(onMissing.score) });
    missingScore.dataset.c = "on_missing_score";
    const sync = () => {
      missingScore.hidden = mode.value !== "explicit_score";
      disabledReason.hidden = enabled.checked;
      if (blocking.checked && mode.value !== "incomplete") { mode.value = "incomplete"; missingScore.hidden = true; }
      mode.disabled = disabled || !(def && def.missing_ao_data_is_recoverable) || blocking.checked;
      updateTotal();
    };
    mode.addEventListener("change", sync); enabled.addEventListener("change", sync); blocking.addEventListener("change", sync);
    row.append(el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;align-items:end" }, [
      small("Si la donnée de l'AO est absente ou ambiguë", mode), small("", missingScore),
      el("label", { style: "display:flex;gap:6px;align-items:center;font-size:.85rem" }, [blocking, document.createTextNode(supportsBlocking ? "Bloquant (non satisfait → NO-GO)" : "Bloquant : voir les paramètres de blocage ci-dessus")]),
      el("label", { style: "display:flex;gap:6px;align-items:center;font-size:.85rem" }, [enabled, document.createTextNode("Critère actif")]),
    ]), disabledReason);
    sync();
    return row;
  }

  function refreshCriteriaFacts() {
    criterionRows().forEach((row) => {
      row.querySelectorAll("select[data-fact-select]").forEach((select) => fillFactSelect(select, select.value));
    });
  }

  function readCriteria() {
    return criterionRows().map((row) => {
      const get = (name) => row.querySelector('[data-c="' + name + '"]');
      const params = {};
      Object.entries(row.controls).forEach(([name, control]) => { params[name] = control.read(); });
      const mode = get("on_missing_mode").value;
      const onMissing = { mode };
      if (mode === "explicit_score") onMissing.score = get("on_missing_score").value.trim() === "" ? null : Number(get("on_missing_score").value);
      const enabled = get("enabled").checked;
      const weightRaw = get("weight").value.trim();
      // Extra keys the server stored (e.g. `legacy_key`) are carried back unchanged.
      return Object.assign({}, row.original, {
        id: get("id").value.trim(), label: get("label").value.trim(), evaluator: row.dataset.evaluator, params,
        weight: weightRaw === "" ? null : Number(weightRaw), blocking: get("blocking").checked, on_missing: onMissing,
        enabled, disabled_reason: enabled ? null : (get("disabled_reason").value.trim() || null),
      });
    });
  }

  // Evaluators a user may ADD (historical keyword rules are never offered for a new criterion).
  const addSelect = $("new-criterion-evaluator");
  addSelect.append(el("option", { value: "", text: "— choisir un type de critère —" }));
  Object.entries(EVALUATORS).filter(([, d]) => d.family !== "legacy")
    .forEach(([key, d]) => addSelect.append(el("option", { value: key, text: d.label })));
  $("add-criterion").addEventListener("click", () => {
    if (!addSelect.value) { showError("Choisissez d'abord le type de critère à ajouter."); return; }
    criteriaList.appendChild(criterionRow({ evaluator: addSelect.value, enabled: true, blocking: false, on_missing: { mode: "incomplete" } }));
    addSelect.value = "";
    updateTotal();
    setDirty(true);
  });

  ((basePolicy && basePolicy.criteria) || []).forEach((c) => criteriaList.appendChild(criterionRow(c)));

  // Proposed drafts: loaded into the FORM only (nothing is saved, activated or preselected).
  const templateSelect = $("template-select");
  templateSelect.append(el("option", { value: "", text: "— aucun —" }));
  TEMPLATES.forEach((t) => templateSelect.append(el("option", { value: t.id, text: t.label })));
  templateSelect.addEventListener("change", () => {
    const t = TEMPLATES.find((x) => x.id === templateSelect.value);
    $("template-hint").hidden = !t;
    $("template-hint").textContent = t ? t.description : "";
  });
  $("load-template").addEventListener("click", () => {
    const t = TEMPLATES.find((x) => x.id === templateSelect.value);
    if (!t) { showError("Choisissez un brouillon proposé à charger."); return; }
    if (criterionRows().length && !window.confirm("Remplacer les critères actuellement affichés par ce brouillon proposé ? (rien n'est enregistré tant que vous n'enregistrez pas)")) return;
    clear(criteriaList);
    t.criteria.forEach((c) => criteriaList.appendChild(criterionRow(c)));
    updateTotal();
    setDirty(true);
  });

  const unavailable = $("unavailable-list");
  (CAT.unavailable || []).forEach((u) => unavailable.append(el("li", {}, [el("strong", { text: u.label + " : " }), document.createTextNode(u.reason)])));

  function fill(id, value) { if (value !== null && value !== undefined) $(id).value = String(value); }
  fill("pol-threshold-go", basePolicy && basePolicy.threshold_go);
  fill("pol-threshold-reserve", basePolicy && basePolicy.threshold_sous_reserve);
  const baseSettings = (basePolicy && basePolicy.settings) || {};
  $("set-na-rule").value = baseSettings.not_applicable_rule === "renormalize" ? "renormalize" : "incomplete";
  $("set-na-confirm").checked = baseSettings.not_applicable_rule_confirmed === true;
  fill("set-strengths", baseSettings.strengths_at_least);
  fill("set-weaknesses", baseSettings.weaknesses_below);

  function numOrNull(id) {
    const raw = $(id).value;
    return raw === "" ? null : Number(raw);
  }

  /* ── Total of the ACTIVE criteria's weights (display only; the server decides, nothing is normalized) ── */
  function updateTotal() {
    let total = 0;
    criterionRows().forEach((r) => {
      if (!r.querySelector('[data-c="enabled"]').checked) return;
      const v = r.querySelector('[data-c="weight"]').value;
      if (v !== "" && Number.isFinite(Number(v))) total += Number(v);
    });
    const exact = Math.abs(total - 100) < 1e-6;
    $("weight-total").textContent = format(total) + " / 100";
    $("weight-total").style.color = exact ? "var(--go)" : "var(--reserve)";
    $("weight-total-hint").textContent = criterionRows().length === 0 ? "Aucun critère : la politique est vide."
      : exact ? "Somme exacte." : total < 100 ? "Reste à répartir : " + format(100 - total) + "." : "Dépassement de " + format(total - 100) + ".";
  }
  updateTotal();

  /* ════════════════ Payloads ════════════════ */
  function profilePayload() {
    const { facts } = readFacts();
    return {
      raison_sociale: $("prof-raison-sociale").value.trim() || null,
      effectif: $("prof-effectif").value.trim() || null,
      competences: $("prof-competences").value.split("\n").map((s) => s.trim()).filter(Boolean),
      certifications: readCertifications(),
      external_enrichment_enabled: $("prof-external-enrichment").checked,
      business_facts: facts, // an explicit {} clears the catalogue
    };
  }

  function policyPayload() {
    return {
      criteria: readCriteria(),
      settings: Object.assign({}, baseSettings, {
        not_applicable_rule: $("set-na-rule").value, not_applicable_rule_confirmed: $("set-na-confirm").checked,
        strengths_at_least: numOrNull("set-strengths"), weaknesses_below: numOrNull("set-weaknesses"),
      }),
      threshold_go: numOrNull("pol-threshold-go"),
      threshold_sous_reserve: numOrNull("pol-threshold-reserve"),
    };
  }

  function duplicateIdentifierError() {
    const { duplicates } = readFacts();
    if (duplicates.length) return "Identifiant de fait dupliqué : " + duplicates.join(", ") + ".";
    const ids = readCriteria().map((c) => c.id).filter(Boolean);
    const dup = ids.find((id, i) => ids.indexOf(id) !== i);
    return dup ? "Identifiant de critère dupliqué : " + dup + "." : "";
  }

  /* ════════════════ Save (profile and policy draft, reported separately) ════════════════ */
  const saveStatus = $("save-status");
  function statusLine(text, ok) {
    saveStatus.append(el("li", { style: "color:" + (ok ? "var(--go)" : "var(--nogo)"), text: (ok ? "✓ " : "✗ ") + text }));
  }

  function lockSavedIdentifiers() {
    factRows().forEach((row) => {
      const key = row.querySelector('[data-f="key"]');
      if (key.value.trim()) { key.readOnly = true; row.dataset.savedKey = key.value.trim(); }
    });
    criterionRows().forEach((row) => {
      const id = row.querySelector('[data-c="id"]');
      if (id.value.trim()) id.readOnly = true;
    });
  }

  async function saveProfile() {
    const r = await api("PUT", "/api/scoring-config/profile", { json: profilePayload() });
    if (r.stale) return { stale: true };
    if (!r.ok) return { ok: false, error: explain(r, "Le profil n'a pas pu être enregistré.").text, raw: r };
    profile.business_facts = r.body.business_facts || {};
    return { ok: true };
  }

  async function savePolicy() {
    const r = await api("PUT", "/api/scoring-config/policy", { json: policyPayload() });
    if (r.stale) return { stale: true };
    if (!r.ok) return { ok: false, error: explain(r, "Le brouillon n'a pas pu être enregistré.").text, raw: r };
    draftPolicy = r.body;
    return { ok: true };
  }

  async function saveDraftAll() {
    if (!scopeIsCurrent()) return;
    const duplicate = duplicateIdentifierError();
    if (duplicate) { showError(duplicate); return; }
    clear(saveStatus);
    const profileResult = await saveProfile();
    if (profileResult.stale) return;
    const policyResult = await savePolicy();
    if (policyResult.stale) return;
    statusLine(profileResult.ok ? "Profil et faits métier : enregistrés." : "Profil et faits métier : NON enregistrés — " + profileResult.error, profileResult.ok);
    statusLine(policyResult.ok ? "Brouillon de politique (v" + draftPolicy.version + ") : enregistré." : "Brouillon de politique : NON enregistré — " + policyResult.error, policyResult.ok);
    if (profileResult.ok && policyResult.ok) {
      setDirty(false);
      lockSavedIdentifiers();
      showFeedback("Profil et brouillon enregistrés.");
    } else {
      const failed = !profileResult.ok ? profileResult : policyResult;
      showFailure(failed.raw, "Enregistrement incomplet.");
      if (profileResult.ok !== policyResult.ok) {
        saveError.append(document.createTextNode(" Sauvegarde partielle : " + (profileResult.ok ? "le profil est enregistré, le brouillon ne l'est pas." : "le brouillon est enregistré, le profil ne l'est pas.")));
      }
    }
  }

  profileForm.addEventListener("submit", guarded($("save-profile"), "Enregistrement…", async (e) => {
    e.preventDefault();
    if (!scopeIsCurrent()) return;
    const duplicate = duplicateIdentifierError();
    if (duplicate) { showError(duplicate); return; }
    clear(saveStatus);
    const result = await saveProfile();
    if (result.stale) return;
    if (!result.ok) { showFailure(result.raw, "Le profil n'a pas pu être enregistré."); statusLine("Profil et faits métier : NON enregistrés — " + result.error, false); return; }
    statusLine("Profil et faits métier : enregistrés.", true);
    lockSavedIdentifiers();
    showFeedback("Profil et faits métier enregistrés.");
  }));

  $("policy-form").addEventListener("submit", guarded($("save-draft"), "Enregistrement…", async (e) => {
    e.preventDefault();
    await saveDraftAll();
  }));

  /* ════════════════ Validation display ════════════════ */
  const validationResult = $("validation-result");
  function renderValidation(result) {
    clear(validationResult);
    if (!result) return;
    if (result.valid) {
      validationResult.append(el("div", { className: "callout callout-success", text: "Le brouillon enregistré est valide et peut être activé." }));
      return;
    }
    const box = el("div", { className: "callout callout-warning" });
    const entries = Object.entries(result.errors || {});
    if (!entries.length) box.append(document.createTextNode("Configuration incomplète."));
    entries.forEach(([group, messages]) => {
      box.append(el("strong", { text: GROUP_LABELS[group] || group }));
      const list = el("ul", { style: "margin:4px 0 10px 18px" });
      (Array.isArray(messages) ? messages : [messages]).forEach((m) => list.append(el("li", { text: String(m) })));
      box.append(list);
    });
    validationResult.append(box);
  }
  renderValidation(window.WM_DRAFT_VALIDATION);

  function requireSavedDraft(actionLabel) {
    if (dirty) { showError("Enregistrez d'abord vos modifications : " + actionLabel + " porte sur le brouillon enregistré."); return false; }
    if (!draftPolicy) { showError("Aucun brouillon enregistré — enregistrez-en un avant de " + actionLabel + "."); return false; }
    return true;
  }

  $("validate-draft").addEventListener("click", guarded($("validate-draft"), "Validation…", async () => {
    if (!scopeIsCurrent() || !requireSavedDraft("valider")) return;
    const r = await api("POST", "/api/scoring-config/policy/validate");
    if (r.stale) return;
    if (!r.ok) { showFailure(r, "Impossible de valider le brouillon."); return; }
    renderValidation(r.body);
  }));

  /* ════════════════ Simulation (never activates, never writes history) ════════════════ */
  $("open-simulation").addEventListener("click", () => {
    $("simulation-card").hidden = false;
    $("sim-text").focus();
  });

  function simulationRow(cells, header) {
    return el("tr", {}, cells.map((c) => el(header ? "th" : "td", { text: c, style: "text-align:left;padding:4px 8px;vertical-align:top" })));
  }

  function renderSimulation(body) {
    const box = $("simulation-result");
    clear(box);
    const decision = body.decision || "";
    const cls = decision === "GO" ? "go" : decision.includes("RESERVE") ? "reserve" : decision === "INCOMPLET" ? "incomplete" : "nogo";
    const label = decision === "GO SOUS RESERVE" ? "GO SOUS RÉSERVE" : decision;
    box.append(el("div", { className: "callout callout-info", style: "font-size:.82rem", text: "SIMULATION" + (body.policy_version ? " — brouillon v" + body.policy_version : "") + " — non enregistrée." }));
    box.append(el("div", { style: "margin:8px 0" }, [
      el("span", { className: "badge badge-" + cls, text: label }),
      el("span", { className: "font-mono", style: "margin-left:12px;font-size:1.2rem;font-weight:700", text: Number(body.score_global).toFixed(1) + " / 100" }),
      body.score_provisoire ? el("span", { style: "margin-left:8px;font-size:.85rem", text: "(score provisoire — critères non évalués)" }) : null,
    ]));
    if (decision === "INCOMPLET") {
      const missing = el("div", { className: "callout callout-info" });
      missing.append(document.createTextNode("Décision incomplète (ce n'est pas un rejet) : aucune décision GO / NO-GO fiable n'est possible avec les données disponibles. Éléments non calculables : "));
      // The technical codes stay in `scoring_missing`; the screen names the criterion
      // (label frozen by the engine with this very simulation — never a newer policy).
      const names = [];
      (body.scoring_missing || []).forEach((code) => {
        const name = (body.scoring_missing_labels || {})[code] || code;
        if (!names.includes(name)) names.push(name);
      });
      missing.append(document.createTextNode(names.join(", ") || "règle non précisée"));
      missing.append(document.createTextNode(". L'extraction locale de la simulation peut être plus limitée que l'analyse réelle."));
      box.append(missing);
    }
    (body.criteres_bloquants || []).forEach((b) => box.append(el("div", { className: "callout callout-danger", text: "Bloquant : " + b })));
    const table = el("table", { style: "width:100%;font-size:.82rem;border-collapse:collapse;margin-top:8px" });
    table.append(simulationRow(["Critère", "Poids", "Score", "Justification"], true));
    (body.criteres || []).forEach((c) => {
      const scoreText = c.etat === "manquant" ? "NON ÉVALUÉ" : c.etat === "non_applicable" ? "NON APPLICABLE"
        : format(c.score) + (c.etat === "hypothese" ? " (hypothèse)" : "");
      table.append(simulationRow([c.nom, format(c.poids), scoreText, c.justification]));
    });
    box.append(table);
    if (body.note) box.append(el("p", { className: "text-tertiary", style: "font-size:.78rem;margin-top:8px", text: body.note }));
  }

  $("run-simulation").addEventListener("click", guarded($("run-simulation"), "Simulation…", async () => {
    if (!scopeIsCurrent() || !requireSavedDraft("simuler")) return;
    const text = $("sim-text").value;
    if (!text.trim()) { showError("Collez le texte d'un appel d'offres à simuler."); return; }
    const form = new FormData();
    form.append("mode", "paste");
    form.append("text", text);
    clear($("simulation-result"));
    const r = await api("POST", "/api/scoring-config/simulate", { form });
    if (r.stale) return;
    if (!r.ok) {
      const errors = r.body && r.body.detail && r.body.detail.errors;
      if (r.status === 422 && errors) renderValidation({ valid: false, errors });
      showFailure(r, "La simulation a échoué.");
      return;
    }
    saveError.hidden = true;
    renderSimulation(r.body);
  }));

  /* ════════════════ Activation (explicit, confirmed, version-checked) ════════════════ */
  const confirmBox = $("activation-confirm");
  let lastKnownActiveVersion = activePolicy ? activePolicy.version : null;

  function renderActive() {
    const card = $("active-policy-card");
    const summary = $("active-policy-summary");
    clear(summary);
    if (!activePolicy) { card.hidden = true; return; }
    card.hidden = false;
    summary.append(
      el("div", { text: "Version " + activePolicy.version + " — activée le " + (activePolicy.activated_at ? new Date(activePolicy.activated_at).toLocaleString("fr-FR") : "—") + " — " + (activePolicy.origin_label || "") }),
      el("div", { style: "margin-top:8px", text: "Seuil GO : " + activePolicy.threshold_go + " · Seuil sous réserve : " + activePolicy.threshold_sous_reserve }),
    );
    const activeCriteria = activePolicy.criteria || [];
    summary.append(el("div", { style: "margin-top:8px", text: activeCriteria.length
      ? "Critères : " + activeCriteria.map((c) => c.label + " (poids " + c.weight + (c.blocking ? ", bloquant" : "") + (c.enabled === false ? ", désactivé" : "") + ")").join(" · ")
      : "Aucun critère." }));
    $("state-badge").textContent = "Politique activée";
    $("state-badge").className = "badge badge-go";
    $("active-version-label").textContent = "Version active : v" + activePolicy.version;
    $("active-origin-label").textContent = (activePolicy.origin_label || "") + " — schéma de critères v" + activePolicy.criteria_version;
  }
  renderActive();

  $("activate-draft").addEventListener("click", () => {
    if (!scopeIsCurrent() || !requireSavedDraft("activer")) return;
    $("activation-confirm-text").textContent = "Vous allez activer le brouillon v" + draftPolicy.version
      + (activePolicy ? " à la place de la version active v" + activePolicy.version : " (aucune version n'est active pour l'instant)")
      + ". Les analyses lancées ensuite l'utiliseront ; les résultats déjà enregistrés ne sont pas recalculés.";
    confirmBox.hidden = false;
  });
  $("activation-confirm-no").addEventListener("click", () => { confirmBox.hidden = true; });

  $("activation-confirm-yes").addEventListener("click", guarded($("activation-confirm-yes"), "Activation…", async () => {
    if (!scopeIsCurrent()) return;
    const r = await api("POST", "/api/scoring-config/policy/activate", { json: { expected_active_version: lastKnownActiveVersion } });
    if (r.stale) return;
    confirmBox.hidden = true;
    if (!r.ok) {
      if (r.status === 409 && r.body && r.body.detail && r.body.detail.error_code === "SCORING_POLICY_ACTIVATION_CONFLICT") {
        showError("La politique active a changé depuis votre dernière lecture (version active actuelle : v" + r.body.detail.current_active_version + "). Rien n'a été activé — rechargez la page pour voir l'état réel avant de réessayer.");
        return;
      }
      if (r.status === 422) {
        const errors = r.body && r.body.detail && r.body.detail.errors;
        renderValidation({ valid: false, errors: errors || {} });
        showError("Le brouillon n'est pas valide : rien n'a été activé. Corrigez les erreurs affichées.");
        return;
      }
      showFailure(r, "Impossible d'activer cette politique.");
      return;
    }
    activePolicy = r.body;
    lastKnownActiveVersion = r.body.version;
    draftPolicy = null;
    renderValidation(null);
    renderActive();
    showFeedback("Politique v" + r.body.version + " activée.");
  }));

  /* ════════════════ Dirty tracking, scope changes, bfcache ════════════════ */
  [profileForm, $("policy-form")].forEach((form) => {
    form.addEventListener("input", () => setDirty(true));
    form.addEventListener("change", () => setDirty(true));
  });

  function lockPage(message) {
    epoch++; // any response still in flight belongs to the previous scope
    document.querySelectorAll("main input, main select, main textarea, main button").forEach((n) => { n.disabled = true; });
    showError(message);
  }
  const orgSwitcher = document.getElementById("org-switcher-select");
  if (orgSwitcher) {
    orgSwitcher.addEventListener("change", () => lockPage("Changement d'organisation — rechargement de la configuration…"), true);
  }
  window.addEventListener("pageshow", (e) => { if (e.persisted) window.location.reload(); });
})();
