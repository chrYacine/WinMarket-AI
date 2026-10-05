// WinMarket AI — shared UI behaviour (header scroll state, mobile nav, tabs)
(function () {
  "use strict";

  // B14-T1: shared CSRF-token accessor for every JS-driven fetch() that
  // mutates state (static/js/analyze.js, static/js/knowledge.js). The
  // underlying cookie (src/web/security/csrf.py) is httponly on purpose —
  // JS cannot read it directly — so the server also embeds the same token
  // in a <meta name="csrf-token"> tag (templates/layout.html) on every page
  // that has a valid session/csrf cookie. Callers send it back as the
  // X-CSRF-Token header, which src/web/security/csrf.py::require_csrf reads
  // via request.headers.get("X-CSRF-Token") for every JSON/multipart route
  // this ticket protects. Defined on `window` (not module-scoped) since
  // main.js, analyze.js and knowledge.js are three separate <script> tags
  // with no bundler/module system tying them together.
  window.wmCsrfToken = function () {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  };

  // B27-T1: shared, safe extraction of a user-facing message from a
  // FastAPI JSON error body. `detail` is a plain string for most
  // HTTPException calls, but several routes (e.g. /api/analyze's
  // CAPACITY_NOT_CONFIGURED / SCORING_NOT_CONFIGURED 409s,
  // /api/scoring-config/policy/activate's 422) pass a `{error_code,
  // message}` object instead — rendering that object directly (as
  // analyze.js used to) produced a literal "[object Object]" in the UI.
  // Never falls through to res.statusText or any other transport-level
  // detail that could echo a path/exception — only ever the server's own
  // deliberately-written message, or the one generic fallback supplied by
  // the caller.
  window.wmErrorMessage = function (body, fallback) {
    const detail = body && body.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      if (typeof detail.message === "string" && detail.message) return detail.message;
      if (typeof detail.errors === "object" && detail.errors) {
        const parts = Object.values(detail.errors).flat().filter(Boolean);
        if (parts.length) return parts.join(" ");
      }
    }
    return fallback || "Une erreur est survenue.";
  };

  // The `error_code` alongside a JSON error body, if any — lets a caller
  // branch on a specific, documented code (e.g. offer a link to
  // /app/parametres for SCORING_NOT_CONFIGURED) without re-parsing detail
  // itself.
  window.wmErrorCode = function (body) {
    const detail = body && body.detail;
    return (detail && typeof detail === "object" && detail.error_code) || null;
  };

  const header = document.querySelector(".site-header");
  if (header) {
    const onScroll = () => header.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  const navToggle = document.querySelector(".nav-toggle");
  const mainNav = document.querySelector(".main-nav");
  if (navToggle && mainNav) {
    navToggle.addEventListener("click", () => {
      const open = mainNav.classList.toggle("nav-open");
      navToggle.setAttribute("aria-expanded", String(open));
    });
  }

  // "Accédez à WinMarket AI" modal — shown instead of navigating straight to
  // /app for anyone who isn't an active Starter user (see site_header.html).
  const accessModal = document.getElementById("access-modal");
  if (accessModal) {
    document.querySelectorAll("[data-access-modal-trigger]").forEach((btn) => {
      btn.addEventListener("click", () => (accessModal.hidden = false));
    });
    document.getElementById("access-modal-close")?.addEventListener("click", () => (accessModal.hidden = true));
    accessModal.addEventListener("click", (e) => {
      if (e.target === accessModal) accessModal.hidden = true;
    });
  }

  // B27-T1: organization switcher (templates/app_shell.html's sidebar
  // selector, shown only when the account has more than one active
  // membership). Sets a plain, non-httponly `wm_org_id` cookie the server
  // (src/web/auth/access_context.py::get_access_context) reads as a
  // REMEMBERED convenience only — never a grant by itself, and a
  // stale/foreign value is always silently ignored server-side — then
  // reloads so every already-rendered link/page picks it up without each
  // one needing to carry an explicit ?organization_id= query param.
  // Same mechanism for the "choose an organization" page shown to an
  // account with several organizations and no selection yet (409).
  document.querySelectorAll("[data-org-choice]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const oneYear = 60 * 60 * 24 * 365;
      document.cookie = `wm_org_id=${encodeURIComponent(btn.dataset.orgChoice)}; path=/; max-age=${oneYear}; samesite=lax`;
      window.location.reload();
    });
  });

  const orgSwitcher = document.getElementById("org-switcher-select");
  if (orgSwitcher) {
    orgSwitcher.addEventListener("change", () => {
      const oneYear = 60 * 60 * 24 * 365;
      document.cookie = `wm_org_id=${encodeURIComponent(orgSwitcher.value)}; path=/; max-age=${oneYear}; samesite=lax`;
      window.location.reload();
    });
  }

  // Generic tabs: any .tabs with [data-tab] buttons controlling sibling .tab-panel[data-tab-panel]
  document.querySelectorAll("[data-tabs]").forEach((wrapper) => {
    const buttons = wrapper.querySelectorAll(".tab-btn");
    const panels = wrapper.querySelectorAll(".tab-panel");
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        buttons.forEach((b) => b.classList.remove("active"));
        panels.forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const target = wrapper.querySelector(`.tab-panel[data-tab-panel="${btn.dataset.tab}"]`);
        if (target) target.classList.add("active");
      });
    });
  });
})();
