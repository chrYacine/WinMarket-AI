// WinMarket AI — Contact form
// Submits to /api/contact, which persists the message server-side
// (data/contact/contact_requests.json). No email/CRM integration exists
// yet, so this only stores the request rather than pretending to send it.
(function () {
  "use strict";

  const form = document.getElementById("contact-form");
  if (!form) return;

  const fields = document.getElementById("contact-fields");
  const successBox = document.getElementById("contact-success");
  const errorBox = document.getElementById("contact-error");
  const errorMsg = document.getElementById("contact-error-message");
  const submitBtn = document.getElementById("contact-submit");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = "Envoi...";

    const employeeCountRaw = document.getElementById("contact-employee-count").value.trim();
    const payload = {
      first_name: document.getElementById("contact-first-name").value.trim(),
      last_name: document.getElementById("contact-last-name").value.trim(),
      email: document.getElementById("contact-email").value.trim(),
      company: document.getElementById("contact-company").value.trim(),
      job_title: document.getElementById("contact-job-title").value.trim(),
      employee_count: employeeCountRaw ? Number(employeeCountRaw) : null,
      plan: document.getElementById("contact-plan").value,
      message: document.getElementById("contact-message").value.trim(),
      csrf_token: document.getElementById("contact-csrf").value,
    };

    try {
      const res = await fetch("/api/contact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        errorMsg.textContent = err.detail || "Impossible d'envoyer le message. Réessayez.";
        errorBox.hidden = false;
        submitBtn.disabled = false;
        submitBtn.textContent = "Envoyer le message";
        return;
      }
      fields.hidden = true;
      successBox.hidden = false;
    } catch {
      errorMsg.textContent = "Impossible de contacter le serveur WinMarket AI.";
      errorBox.hidden = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "Envoyer le message";
    }
  });
})();
