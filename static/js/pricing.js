// WinMarket AI — Pricing page
// Purely presentational: toggles the displayed price (monthly/annual).
// CTAs are plain links (Starter -> /register, Business/Enterprise -> /contact).
(function () {
  "use strict";

  const toggleButtons = document.querySelectorAll(".billing-option");
  const monthlyEls = document.querySelectorAll(".price-monthly");
  const annualEls = document.querySelectorAll(".price-annual");

  function setBilling(mode) {
    toggleButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.billing === mode));
    monthlyEls.forEach((el) => (el.hidden = mode !== "monthly"));
    annualEls.forEach((el) => (el.hidden = mode !== "annual"));
  }

  toggleButtons.forEach((btn) => {
    btn.addEventListener("click", () => setBilling(btn.dataset.billing));
  });
})();
