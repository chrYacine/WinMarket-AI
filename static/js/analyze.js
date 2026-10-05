// WinMarket AI — "Nouvelle analyse" page
// Handles source selection, capacity panel and analysis progress polling.
// No business rule lives here: this file only calls /api/analyze and
// /api/capacity and reflects the JSON the server returns.
(function () {
  "use strict";

  const form = document.getElementById("analyze-form");
  if (!form) return;

  const STEP_LABELS = window.WM_STEPS || [];

  /* ── Element lookups (all upfront, so functions below never hit a
     temporal-dead-zone reference before the rest of the script runs) ── */
  const modeInputs = form.querySelectorAll('input[name="mode"]');
  const panels = {
    stock: document.getElementById("panel-stock"),
    upload: document.getElementById("panel-upload"),
    paste: document.getElementById("panel-paste"),
    dossier: document.getElementById("panel-dossier"),
  };
  const exampleSelect = document.getElementById("example-select");
  const examplePreview = document.getElementById("example-preview");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const fileNameLabel = document.getElementById("file-name");
  const pasteArea = document.getElementById("paste-textarea");
  const submitBtn = document.getElementById("submit-analyze");

  const capacityBtn = document.getElementById("capacity-open");
  const capacityModal = document.getElementById("capacity-modal");
  const capacityClose = document.getElementById("capacity-close");
  const capacityForm = document.getElementById("capacity-form");
  const capacityPoles = document.getElementById("capacity-poles");
  const chargeRange = document.getElementById("cap-charge");

  const formPanel = document.getElementById("analyze-form-panel");
  const progressPanel = document.getElementById("progress-panel");
  const errorPanel = document.getElementById("analyze-error");
  const progressMessage = document.getElementById("progress-message");
  const progressSteps = document.getElementById("progress-steps");
  const progressBarFill = document.getElementById("progress-bar-fill");

  /* ── Source mode switching ── */
  function applyMode(mode) {
    Object.entries(panels).forEach(([key, el]) => {
      if (!el) return;
      el.hidden = key !== mode;
    });
    form.querySelectorAll(".option-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.mode === mode);
    });
    updateSubmitState();
  }

  function currentMode() {
    const checked = form.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : null;
  }

  function updateSubmitState() {
    const mode = currentMode();
    let ready = false;
    if (mode === "stock") ready = !!(exampleSelect && exampleSelect.value);
    if (mode === "upload") ready = !!(fileInput && fileInput.files && fileInput.files.length);
    if (mode === "paste") ready = !!(pasteArea && pasteArea.value.trim().length > 0);
    if (mode === "dossier") ready = !!(window.WMDossier && window.WMDossier.isReady());
    if (submitBtn) submitBtn.disabled = !ready;
  }

  function reflectFileName() {
    if (fileInput.files && fileInput.files[0]) {
      fileNameLabel.textContent = fileInput.files[0].name;
      fileNameLabel.hidden = false;
    } else {
      fileNameLabel.hidden = true;
    }
    updateSubmitState();
  }

  modeInputs.forEach((input) => {
    input.addEventListener("change", () => applyMode(input.value));
  });

  /* ── Stock example preview ── */
  if (exampleSelect) {
    exampleSelect.addEventListener("change", async () => {
      if (!exampleSelect.value) {
        examplePreview.hidden = true;
        updateSubmitState();
        return;
      }
      try {
        const res = await fetch(`/api/examples/${encodeURIComponent(exampleSelect.value)}`);
        if (!res.ok) throw new Error("fetch failed");
        const data = await res.json();
        examplePreview.textContent = data.text.slice(0, 2500) + (data.text.length > 2500 ? "…" : "");
        examplePreview.hidden = false;
      } catch {
        examplePreview.hidden = true;
      }
      updateSubmitState();
    });
  }

  /* ── Upload dropzone ── */
  if (dropzone && fileInput) {
    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });
    ["dragenter", "dragover"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach((evt) =>
      dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
    );
    dropzone.addEventListener("drop", (e) => {
      const dropped = e.dataTransfer.files;
      if (dropped && dropped.length) {
        fileInput.files = dropped;
        reflectFileName();
      }
    });
    fileInput.addEventListener("change", reflectFileName);
  }

  /* ── Paste textarea ── */
  if (pasteArea) pasteArea.addEventListener("input", updateSubmitState);

  /* ── AO dossier (lot 47 bis): the picker lives in dossier.js; this file only sends it and follows the job ── */
  if (window.WMDossier) window.WMDossier.onChange(updateSubmitState);

  /* ── Capacity panel ── */
  async function loadCapacityIntoStrip() {
    try {
      const res = await fetch("/api/capacity");
      const data = await res.json();
      const chargeEl = document.getElementById("capacity-charge-value");
      const dispoEl = document.getElementById("capacity-dispo-value");
      const projEl = document.getElementById("capacity-projects-value");
      if (chargeEl) chargeEl.textContent = `${data.charge_globale_pct}%`;
      if (dispoEl) dispoEl.textContent = `${data.disponibilite_pct}%`;
      if (projEl) projEl.textContent = data.nombre_projets_en_cours;
      return data;
    } catch {
      return null;
    }
  }

  function renderCapacityForm(data) {
    document.getElementById("cap-charge").value = data.charge_globale_pct;
    document.getElementById("cap-charge-out").textContent = data.charge_globale_pct + " %";
    document.getElementById("cap-projects-count").value = data.nombre_projets_en_cours;
    document.getElementById("cap-projects-list").value = (data.projets_en_cours || []).join("\n");
    document.getElementById("cap-min-dispo").value = data.disponibilite_minimum_pct;
    capacityPoles.innerHTML = "";
    Object.entries(data.capacites_par_pole || {}).forEach(([pole, value]) => {
      const row = document.createElement("div");
      row.className = "field";
      row.innerHTML = `
        <label>${pole}</label>
        <div class="range-row">
          <input type="range" min="0" max="100" value="${value}" data-pole="${pole}">
          <span class="range-value">${value}%</span>
        </div>`;
      const range = row.querySelector("input[type=range]");
      const out = row.querySelector(".range-value");
      range.addEventListener("input", () => (out.textContent = range.value + "%"));
      capacityPoles.appendChild(row);
    });
  }

  if (capacityBtn && capacityModal) {
    capacityBtn.addEventListener("click", async () => {
      capacityModal.hidden = false;
      const data = await loadCapacityIntoStrip();
      if (data) renderCapacityForm(data);
    });
    capacityClose.addEventListener("click", () => (capacityModal.hidden = true));
    capacityModal.addEventListener("click", (e) => {
      if (e.target === capacityModal) capacityModal.hidden = true;
    });
  }

  if (chargeRange) {
    chargeRange.addEventListener("input", () => {
      document.getElementById("cap-charge-out").textContent = chargeRange.value + " %";
    });
  }

  if (capacityForm) {
    capacityForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const poles = {};
      capacityPoles.querySelectorAll("input[type=range]").forEach((r) => {
        poles[r.dataset.pole] = Number(r.value);
      });
      const payload = {
        charge_globale_pct: Number(document.getElementById("cap-charge").value),
        nombre_projets_en_cours: Number(document.getElementById("cap-projects-count").value),
        projets_en_cours: document.getElementById("cap-projects-list").value.split("\n").map((s) => s.trim()).filter(Boolean),
        capacites_par_pole: poles,
        // Lot 48: this threshold used to have no field at all — every account silently got the schema's
        // default (10 %) with no way to see or change it. It decides "Disponibilité de l'équipe" (GO/INCOMPLET).
        disponibilite_minimum_pct: Number(document.getElementById("cap-min-dispo").value),
      };
      const saveBtn = capacityForm.querySelector('button[type="submit"]');
      saveBtn.disabled = true;
      saveBtn.textContent = "Enregistrement...";
      try {
        await fetch("/api/capacity", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": window.wmCsrfToken() },
          body: JSON.stringify(payload),
        });
        await loadCapacityIntoStrip();
        capacityModal.hidden = true;
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = "Enregistrer les disponibilités";
      }
    });
  }

  /* ── Submit analysis ── */
  function renderSteps(activeIndex) {
    if (!progressSteps) return;
    progressSteps.innerHTML = STEP_LABELS.map((label, i) => {
      const cls = i < activeIndex ? "done" : i === activeIndex ? "active" : "";
      return `<div class="step-pill ${cls}">${i < activeIndex ? "✓ " : ""}${label}</div>`;
    }).join("");
  }

  const MAX_POLL_FAILURES = 15; // ~30s of transient network errors before giving up

  async function pollStatus(jobId, failureCount = 0) {
    let res;
    try {
      res = await fetch(`/api/analyze/${jobId}/status`);
    } catch {
      if (failureCount >= MAX_POLL_FAILURES) {
        showError("Impossible de contacter le serveur WinMarket AI.");
        return;
      }
      setTimeout(() => pollStatus(jobId, failureCount + 1), 2000);
      return;
    }

    if (!res.ok) {
      // A non-2xx response (404 = job unknown to the server, e.g. after a
      // restart) will never resolve itself — stop polling instead of
      // retrying forever.
      const body = await res.json().catch(() => ({}));
      showError(window.wmErrorMessage(body, "Cette analyse n'est plus disponible sur le serveur."));
      return;
    }

    const data = await res.json();
    if (data.status === "error") {
      showError(data.error || "Une erreur est survenue pendant l'analyse.");
      return;
    }
    renderSteps(data.step_index);
    progressMessage.textContent = data.message;
    progressBarFill.style.width = `${((data.step_index + 1) / data.total_steps) * 100}%`;
    if (data.status === "done" && data.redirect_url) {
      window.location.href = data.redirect_url;
      return;
    }
    setTimeout(() => pollStatus(jobId), 1100);
  }

  function showError(message, allowHtml) {
    progressPanel.hidden = true;
    errorPanel.hidden = false;
    const el = document.getElementById("analyze-error-message");
    // `allowHtml` is only ever set for this file's own hardcoded literals
    // above (never for server-supplied text) — a real server message
    // always goes through textContent, never innerHTML, so nothing an
    // account/organization could ever set (a title, an error message) can
    // inject markup.
    if (allowHtml) el.innerHTML = message;
    else el.textContent = message;
  }

  document.getElementById("analyze-retry")?.addEventListener("click", () => {
    errorPanel.hidden = true;
    formPanel.hidden = false;
  });

  // Lot 50 §3: dossier mode now goes through an admission PREVIEW first — the server vets every piece
  // (security/classification/relevance) and nothing is analysed until the user confirms the table
  // (window.WMDossierPreview, static/js/dossier_preview.js). A structural refusal (wrong slot, too many
  // files, byte budget) still names the piece(s) next to the fields, exactly as before; the selection is
  // never lost either way.
  function startProgress(jobId) {
    formPanel.hidden = true;
    errorPanel.hidden = true;
    progressPanel.hidden = false;
    renderSteps(0);
    progressMessage.textContent = "Initialisation...";
    progressBarFill.style.width = "4%";
    pollStatus(jobId);
  }

  async function submitDossier() {
    const D = window.WMDossier;
    if (!D || !D.scopeIsCurrent()) return;
    D.clearMessage();
    const startEpoch = D.epoch();
    const label = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = "Envoi et vérification du dossier…";
    D.setStatus("Envoi et vérification des pièces en cours…"); // no percentage: the server checks every piece before it answers
    // Lot 50 bis §3: "Ajouter les pièces restantes" (static/js/add_pieces.js) posts the SAME multipart shape
    // to a DEDICATED route that also carries which of the original dossier's pieces to keep — never the plain
    // preview route, which knows nothing about an origin job.
    const addPieces = window.WMAddPieces && window.WMAddPieces.active;
    const url = addPieces
      ? `/api/analyze/${encodeURIComponent(window.WMAddPieces.jobId)}/add-pieces/preview?organization_id=${encodeURIComponent(D.org)}`
      : "/api/analyze/dossier-preview?organization_id=" + encodeURIComponent(D.org);
    const formData = D.formData();
    if (addPieces) formData.append("keep_piece_ids", JSON.stringify(window.WMAddPieces.keepPieceIds()));
    let res, body;
    try {
      res = await fetch(url, {
        method: "POST", headers: { "X-CSRF-Token": window.wmCsrfToken() }, body: formData,
      });
      body = await res.json().catch(() => ({}));
    } catch {
      if (D.epoch() === startEpoch) {
        D.showMessage("Impossible de contacter le serveur WinMarket AI : rien n'a été analysé, votre sélection est conservée.");
        submitBtn.textContent = label; D.setStatus(""); updateSubmitState();
      }
      return;
    }
    if (D.epoch() !== startEpoch) {
      // The selected organization changed while the request was in flight: it carried ITS OWN explicit organization,
      // so nothing landed in the new one — but this page must not act on the answer.
      D.showMessage("L'organisation active a changé pendant l'envoi : le dossier a été traité dans l'organisation d'origine (voir son historique). Rechargez la page.");
      return;
    }
    submitBtn.textContent = label;
    D.setStatus("");
    updateSubmitState();
    if (!res.ok) { D.showFailure(res.status, body); return; }
    if (window.WMDossierPreview) window.WMDossierPreview.show(body, startProgress);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mode = currentMode();
    if (mode === "dossier") { await submitDossier(); return; }
    const fd = new FormData();
    fd.append("mode", mode);
    if (mode === "stock") fd.append("example_id", exampleSelect.value);
    if (mode === "upload") fd.append("file", fileInput.files[0]);
    if (mode === "paste") fd.append("text", pasteArea.value);

    formPanel.hidden = true;
    errorPanel.hidden = true;
    progressPanel.hidden = false;
    renderSteps(0);
    progressMessage.textContent = "Initialisation...";
    progressBarFill.style.width = "4%";

    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "X-CSRF-Token": window.wmCsrfToken() },
        body: fd,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const code = window.wmErrorCode(err);
        if (code === "SCORING_NOT_CONFIGURED") {
          showError('Configurez et activez votre politique de scoring avant de lancer une analyse — rendez-vous dans <a href="/app/parametres">Paramètres de scoring</a>.', true);
        } else if (code === "CAPACITY_NOT_CONFIGURED") {
          showError('Configurez la disponibilité de votre équipe avant de lancer une analyse (bouton "Modifier les disponibilités" ci-dessus).');
        } else if (res.status === 429) {
          showError(window.wmErrorMessage(err, "Trop de tentatives — patientez un instant avant de relancer une analyse."));
        } else if (res.status === 413) {
          showError(window.wmErrorMessage(err, "Le fichier envoyé est trop volumineux."));
        } else {
          showError(window.wmErrorMessage(err, "Impossible de démarrer l'analyse."));
        }
        return;
      }
      const data = await res.json();
      pollStatus(data.job_id);
    } catch {
      showError("Impossible de contacter le serveur WinMarket AI.");
    }
  });

  /* ── Initial state ── */
  const initialMode = form.querySelector('input[name="mode"]:checked');
  if (initialMode) applyMode(initialMode.value);
  updateSubmitState();
  loadCapacityIntoStrip();
})();
