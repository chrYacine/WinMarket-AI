// WinMarket AI — "Ajouter les pièces restantes" (lot 50 bis §3): when this page is opened as
// /app/analyser?add_pieces_from=<job_id>, lists the ORIGINAL dossier's own pieces (each still individually
// includable/excludable) and switches the dossier submit flow (static/js/analyze.js) to the dedicated
// add-pieces preview route instead of the plain one. A NEW documentary re-analysis, never the declarative
// revision of lot 49 bis. Every name/label is written with textContent, never innerHTML.
(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  const jobId = params.get("add_pieces_from");
  window.WMAddPieces = { active: false };
  if (!jobId) return;

  const banner = document.getElementById("add-pieces-banner");
  const originalBox = document.getElementById("add-pieces-original");
  const originalList = document.getElementById("add-pieces-original-list");
  if (!banner || !originalBox || !originalList) return;

  const keepIds = new Set();

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "on") Object.entries(v).forEach(([ev, fn]) => node.addEventListener(ev, fn));
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => { if (c) node.append(c); });
    return node;
  }

  window.WMAddPieces = { active: true, jobId, keepPieceIds: () => Array.from(keepIds) };

  async function load() {
    banner.hidden = false;
    banner.textContent = "Ajout de pièces à une analyse existante — chargement du dossier d'origine…";
    let res, body;
    try {
      res = await fetch(`/api/analyze/${encodeURIComponent(jobId)}/dossier`);
      body = await res.json().catch(() => ({}));
    } catch {
      banner.textContent = "Impossible de contacter le serveur pour charger le dossier d'origine.";
      return;
    }
    if (!res.ok) {
      banner.textContent = "Dossier d'origine introuvable ou inaccessible : impossible d'ajouter des pièces à cette analyse.";
      return;
    }
    // Lot 50 ter — the previous wording never said WHICH policy/profile/capacity would be used, nor that
    // the reference selection could differ from the original analysis; a field visible only AFTER the
    // calculation (job_id/decision) does not substitute for announcing this BEFORE confirmation.
    banner.textContent = "Nouvelle analyse documentaire : les pièces ci-dessous sont reprises de l'analyse d'origine (sauf décochées), combinées à celles ajoutées ci-dessous. Le calcul sera entièrement refait avec votre politique, votre profil et votre capacité ACTUELS (pas ceux de l'analyse d'origine, qui peuvent avoir changé depuis) et pourra sélectionner des références différentes. L'analyse d'origine et ses documents restent inchangés.";
    originalBox.hidden = false;
    (body.pieces || []).forEach((p) => {
      keepIds.add(p.id);
      const checkbox = el("input", {
        type: "checkbox", checked: true,
        on: { change: (e) => { if (e.target.checked) keepIds.add(p.id); else keepIds.delete(p.id); } },
      });
      originalList.append(el("li", { className: "dossier-item" }, [
        checkbox,
        el("span", { className: "dossier-name", text: " " + p.nom + " (" + (p.categorie_libelle || p.categorie) + ")" }),
      ]));
    });
    const dossierRadio = document.querySelector('input[name="mode"][value="dossier"]');
    if (dossierRadio) { dossierRadio.checked = true; dossierRadio.dispatchEvent(new Event("change", { bubbles: true })); }
  }

  load();
})();
