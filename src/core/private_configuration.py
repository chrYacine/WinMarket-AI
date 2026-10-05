"""Lot 43 — the explicit refusal raised when a NEW analysis/simulation is
asked for without the private configuration it needs.

The engine and the capacity analyzer used to fall back silently on
process-wide demo values (a global scoring policy, an assumed skills/
certifications set, demo thresholds, a demo capacity file) whenever no
private configuration was passed. Those fallbacks are removed: a caller that
has not resolved the account's own configuration gets this error, never a
default computed on somebody else's numbers. The SaaS routes refuse earlier
with their own 409 (SCORING_NOT_CONFIGURED / CAPACITY_NOT_CONFIGURED); this
is the last line of defense inside the shared services."""
from __future__ import annotations


class PrivateConfigurationRequired(ValueError):
    """`missing` names the configuration that was not provided
    (`"scoring_policy"` or `"capacity_plan"`)."""

    def __init__(self, missing: str):
        self.missing = missing
        super().__init__(
            f"Configuration privée requise : {missing}. Aucune valeur par défaut n'est appliquée — "
            "résolvez la configuration du compte (organisation + propriétaire) avant d'analyser ou de simuler."
        )
