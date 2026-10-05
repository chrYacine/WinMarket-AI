// WinMarket AI — the AO dossier picker (lot 47 bis): four named slots of ONE file each + up to three annexes,
// one analysis. The limits come from the server (window.WM_DOSSIER); the browser only helps — every rule is
// re-checked by the server, and a refused addition never removes what is already selected.
//
// Sizes are DECIMAL (Ko / Mo, 1 Mo = 1 000 000 octets) — never mixed with the Mio of the knowledge base.
// Every name and message is written with textContent (never innerHTML). No upload percentage is invented.
(function () {
  "use strict";
  const cfg = window.WM_DOSSIER;
  const panel = document.getElementById("panel-dossier");
  if (!cfg || !panel) return;

  const ORG = window.WM_ORG || "";
  const SLOTS = cfg.slots.map((s) => s.field);
  const state = Object.fromEntries(SLOTS.map((f) => [f, []]));
  const listeners = [];
  let epoch = 0;          // bumped when the selected organization changes: an older answer is never displayed
  let scopeLost = false;

  const $ = (id) => document.getElementById(id);
  const totalEl = $("dossier-total");
  const errorEl = $("dossier-error");
  const statusEl = $("dossier-status");

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

  function fmt(n) {
    return n < 1000000 ? (n / 1000).toFixed(1).replace(".", ",") + " Ko" : (n / 1000000).toFixed(1).replace(".", ",") + " Mo";
  }
  // The cumulative counter is always in Mo (decimal), as announced: "X Mo / 100 Mo". The exact byte count is in data-bytes.
  function fmtTotalMo(n) {
    if (n === 0) return "0,0 Mo";
    return (n / 1000000).toFixed(n < 1000000 ? 2 : 1).replace(".", ",") + " Mo";
  }
  const sum = (files) => files.reduce((t, f) => t + f.size, 0);
  const totalBytes = () => SLOTS.reduce((t, f) => t + sum(state[f]), 0);
  const fileCount = () => SLOTS.reduce((t, f) => t + state[f].length, 0);
  const slotLabel = (field) => cfg.slots.find((s) => s.field === field).label;
  function changed() { render(); listeners.forEach((fn) => fn()); }

  /* ── messages ── */
  function showMessage(text, items) {
    clear(errorEl);
    errorEl.append(el("div", { text }));
    if (items && items.length) errorEl.append(el("ul", { style: "margin:8px 0 0 18px" }, items.map((t) => el("li", { text: t }))));
    errorEl.hidden = false;
  }
  function clearMessage() { errorEl.hidden = true; clear(errorEl); }

  /* ── selection: an addition is atomic — refused as a whole, and the current selection is kept ── */
  function addFiles(field, incoming) {
    if (!incoming.length || scopeLost) return;
    const single = field !== "annexes" && field !== "autres"; // lot 50: "autres" is also a multi-file, free-form slot
    const next = single ? [incoming[0]] : state[field].concat(incoming);
    if (field === "annexes" && next.length > cfg.max_annexes) {
      showMessage("Annexes : " + cfg.max_annexes + " fichiers au maximum — aucun fichier n'a été ajouté, votre sélection est conservée.");
      return;
    }
    const slotMax = (cfg.slots.find((s) => s.field === field) || {}).max_files;
    if (!single && slotMax != null && next.length > slotMax) {
      showMessage(slotLabel(field) + " : " + slotMax + " fichiers au maximum (limite globale du dossier) — aucun fichier n'a été ajouté, votre sélection est conservée.");
      return;
    }
    const others = totalBytes() - sum(state[field]);
    if (others + sum(next) > cfg.max_total_bytes) {
      showMessage("Le dossier dépasserait " + cfg.max_total_label + " (" + fmt(others + sum(next)) + ") — ajout refusé, votre sélection est conservée.");
      return;
    }
    clearMessage();
    state[field] = next;
    changed();
  }
  function removeFile(field, index) {
    state[field].splice(index, 1);
    clearMessage();
    changed();
    $("dossier-input-" + field).focus();
  }

  function render() {
    SLOTS.forEach((field) => {
      const list = $("dossier-list-" + field);
      clear(list);
      state[field].forEach((file, index) => {
        const li = el("li", { className: "dossier-item" }, [
          el("span", { className: "dossier-name", text: file.name }),
          el("span", { className: "dossier-size text-tertiary", text: fmt(file.size) }),
        ]);
        if (field !== "annexes" && field !== "autres") {
          li.append(el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Remplacer", "aria-label": "Remplacer " + file.name + " — " + slotLabel(field),
            on: { click: () => $("dossier-input-" + field).click() } }));
        }
        li.append(el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Retirer", "aria-label": "Retirer " + file.name + " — " + slotLabel(field),
          on: { click: () => removeFile(field, index) } }));
        list.append(li);
      });
    });
    const total = totalBytes();
    totalEl.textContent = fmtTotalMo(total) + " / " + cfg.max_total_label;
    totalEl.dataset.bytes = String(total);
    totalEl.classList.toggle("dossier-total-over", total > cfg.max_total_bytes);
  }

  SLOTS.forEach((field) => {
    const input = $("dossier-input-" + field);
    input.addEventListener("change", () => {
      addFiles(field, Array.from(input.files || []));
      input.value = ""; // the same file can be chosen again; our own list is the selection
    });
  });

  /* ── organization scope: the page belongs to ONE organization ── */
  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function scopeIsCurrent() {
    if (scopeLost) return false;
    const current = cookie("wm_org_id");
    if (current && ORG && current !== ORG) {
      scopeLost = true;
      epoch += 1;
      panel.querySelectorAll("input, button").forEach((n) => { n.disabled = true; });
      showMessage("L'organisation active a changé depuis l'affichage de cette page : plus rien n'est envoyé. Rechargez la page pour continuer dans la nouvelle organisation.");
      listeners.forEach((fn) => fn());
      return false;
    }
    return true;
  }
  window.addEventListener("focus", scopeIsCurrent);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) scopeIsCurrent(); });

  /* ── what the server answers ── */
  function describeFailure(status, body) {
    const detail = body && body.detail;
    if (detail && typeof detail === "object" && Array.isArray(detail.pieces) && detail.pieces.length) {
      return { text: "Le dossier est refusé : rien n'a été analysé. Corrigez ou retirez les pièces suivantes, puis relancez.",
               items: detail.pieces.map((p) => (p.category_label || "Pièce") + " — « " + p.piece + " » : " + p.message) };
    }
    const code = window.wmErrorCode ? window.wmErrorCode(body) : null;
    if (status === 413) return { text: window.wmErrorMessage(body, "Le dossier est trop volumineux : limite de " + cfg.max_total_label + " (somme des fichiers).") };
    if (status === 401) return { text: "Votre session a expiré : reconnectez-vous puis recommencez (votre sélection est conservée tant que la page reste ouverte)." };
    if (code === "SCORING_NOT_CONFIGURED") return { text: "Configurez et activez votre politique de scoring avant de lancer une analyse (Paramètres de scoring)." };
    if (code === "CAPACITY_NOT_CONFIGURED") return { text: "Configurez la disponibilité de votre équipe avant de lancer une analyse (bouton « Modifier les disponibilités »)." };
    if (status === 429) return { text: window.wmErrorMessage(body, "Trop de tentatives — patientez un instant avant de relancer.") };
    return { text: window.wmErrorMessage(body, "Impossible de démarrer l'analyse du dossier.") };
  }

  window.WMDossier = {
    org: ORG,
    // Lot 50 bis §3: "Ajouter les pièces restantes" may legitimately submit zero NEW files (only dropping
    // some original ones) as long as at least one original piece is still kept.
    isReady: () => !scopeLost && fileCount() <= cfg.max_files && totalBytes() <= cfg.max_total_bytes
      && (fileCount() > 0 || (window.WMAddPieces && window.WMAddPieces.active && window.WMAddPieces.keepPieceIds().length > 0)),
    onChange: (fn) => listeners.push(fn),
    scopeIsCurrent,
    epoch: () => epoch,
    formData() {
      const fd = new FormData();
      fd.append("mode", "dossier");
      SLOTS.forEach((field) => state[field].forEach((file) => fd.append(field, file, file.name)));
      return fd;
    },
    showFailure(status, body) {
      const { text, items } = describeFailure(status, body);
      showMessage(text, items);
      errorEl.focus();
    },
    showMessage,
    clearMessage,
    setStatus(text) { statusEl.textContent = text; },
  };
  render();
})();
