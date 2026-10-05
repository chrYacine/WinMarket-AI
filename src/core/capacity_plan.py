"""The plain capacity input `CapacityAnalyzer.analyze` consumes.

Lot 43: extracted from the former `src/core/capacity_repository.py`, whose
file-backed `CapacityRepository` (and demo values: 78 % load, six IT poles,
10 % minimum) served only the removed Streamlit demo. Every field describing
the team's real situation is now REQUIRED — there is no demo default any
more; the SaaS builds this from the account's own PrivateCapacityPlan row.
"""
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class CapacityPlan:
    charge_globale_pct: int
    # B08-T1: the explicit, private "minimum remaining capacity" threshold
    # of the account (PrivateCapacityPlan.disponibilite_minimum_pct).
    disponibilite_minimum_pct: int
    nombre_projets_en_cours: int = 0
    projets_en_cours: list[str] = field(default_factory=list)
    # Only the poles the account actually configured — never a default set.
    capacites_par_pole: Dict[str, int] = field(default_factory=dict)
