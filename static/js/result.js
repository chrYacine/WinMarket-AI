// WinMarket AI — "Résultats" page: per-document (PDF/DOCX) availability and
// regeneration. Never recomputes score/decision here — this only calls
// GET /api/analyze/{job_id}/documents/status and POST .../regenerate and
// reflects exactly what the server returns (B19-T2's contract: a document
// snapshot regeneration never rescoring, never re-enriching).
(function () {
  "use strict";

  const jobId = window.WM_JOB_ID;
  if (!jobId) return;

  const KINDS = ["pdf", "docx"];

  function els(kind) {
    return {
      status: document.getElementById(`doc-${kind}-status`),
      error: document.getElementById(`doc-${kind}-error`),
      download: document.getElementById(`doc-${kind}-download`),
      regenerate: document.getElementById(`doc-${kind}-regenerate`),
    };
  }

  function applyStatus(kind, status) {
    const { status: statusEl, download, regenerate } = els(kind);
    if (status === "available") {
      if (statusEl) statusEl.hidden = true;
      if (download) download.hidden = false;
    } else {
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = "Document indisponible pour le moment — régénérez-le depuis l'analyse déjà enregistrée, sans recalcul.";
      }
      if (download) download.hidden = true;
    }
    // Regeneration stays offered either way: even an already-available
    // document may need to be rebuilt (e.g. after a template update) —
    // never blocked, but it never itself changes the score/decision shown
    // above (B19-T2: rebuilt from the persisted snapshot alone).
    if (regenerate) regenerate.hidden = false;
  }

  async function refreshStatus() {
    try {
      const res = await fetch(`/api/analyze/${jobId}/documents/status`);
      if (!res.ok) {
        KINDS.forEach((k) => applyStatus(k, "unavailable"));
        return;
      }
      const data = await res.json();
      KINDS.forEach((k) => applyStatus(k, data[k]));
    } catch {
      // A network failure must never look like a successful check, and
      // must never hide the already-saved result above it — only the
      // document panel degrades.
      KINDS.forEach((k) => applyStatus(k, "unavailable"));
    }
  }

  KINDS.forEach((kind) => {
    const { regenerate, error } = els(kind);
    if (!regenerate) return;
    regenerate.addEventListener("click", async () => {
      const original = regenerate.textContent;
      regenerate.disabled = true;
      regenerate.textContent = "Régénération…";
      if (error) error.hidden = true;
      try {
        const res = await fetch(`/api/analyze/${jobId}/documents/${kind}/regenerate`, {
          method: "POST",
          headers: { "X-CSRF-Token": window.wmCsrfToken() },
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          if (error) {
            error.hidden = false;
            error.textContent = window.wmErrorMessage(body, "La régénération a échoué. Réessayez ou contactez le support.");
          }
        } else {
          await refreshStatus();
        }
      } catch {
        if (error) {
          error.hidden = false;
          error.textContent = "Impossible de contacter le serveur WinMarket AI.";
        }
      } finally {
        regenerate.disabled = false;
        regenerate.textContent = original;
      }
    });
  });

  refreshStatus();
})();
