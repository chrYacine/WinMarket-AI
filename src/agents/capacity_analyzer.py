"""B08-T1 (DEFECT confirmed): team availability is computed from what the
account actually declared about its OWN team — never from what the AO
happens to ask for.

The old calculation added arbitrary surcharges keyed on the AO's requested
technologies: +10 points if any of a hardcoded list ["sap", "mainframe",
"cobol", "blockchain"] appeared, +8 more if at least 6 technologies were
listed, and `equipe_disponible` was additionally forced to False by that
same `heavy` flag. None of those numbers or lists came from the account;
an AO mentioning "blockchain" made a team look 10 points busier than it
is, and the resulting `charge_actuelle_pct` was then displayed and scored
as if it were the account's real load. All of it is deleted.

What is used instead is only real configured data (src/core/
capacity_plan.py::CapacityPlan, fed by the account's own
PrivateCapacityPlan row):
  - `charge_globale_pct`        — the declared global load, used as-is.
  - `disponibilite_minimum_pct` — the account's own minimum-remaining
    threshold, replacing a hardcoded `>= 10`.
  - `capacites_par_pole`        — remaining capacity per pole, previously
    loaded and then never read. Used as a SECOND, independent signal:
    the tightest configured pole is compared against the SAME threshold.
    It is never summed with, cross-checked against, or derived from
    `charge_globale_pct` (both describe the same team from two angles, so
    combining them would double-count), and no per-role split is ever
    invented — only poles the account actually configured are named.
"""
from src.core.capacity_plan import CapacityPlan
from src.core.models import AOContext, CapacityResult
from src.core.private_configuration import PrivateConfigurationRequired


class CapacityAnalyzer:
    def analyze(self, ao: AOContext, plan: CapacityPlan) -> CapacityResult:
        """`plan` is the caller's own private CapacityPlan
        (src/web/database/models.py::PrivateCapacityPlan) — required since
        lot 43: there is no global/demo capacity file to fall back on, and a
        missing plan raises PrivateConfigurationRequired instead of scoring a
        team on somebody else's numbers.

        `ao` is intentionally unread: since B08-T1, nothing about the AO
        (its technologies above all) may influence the account's own
        capacity. The parameter stays in the signature because every
        caller passes it positionally and because the result is stored
        alongside that AO's analysis.
        """
        if plan is None:
            raise PrivateConfigurationRequired("capacity_plan")

        # Defensive only — the DB column already constrains this to 0-100.
        # No adjustment is applied.
        charge = max(0, min(100, int(plan.charge_globale_pct)))
        remaining = 100 - charge
        seuil = int(plan.disponibilite_minimum_pct)

        poles = plan.capacites_par_pole or {}
        tightest_pole, tightest_pct = min(poles.items(), key=lambda item: item[1]) if poles else (None, None)

        global_ok = remaining >= seuil
        poles_ok = tightest_pct is None or tightest_pct >= seuil
        ok = global_ok and poles_ok

        projets = f"{plan.nombre_projets_en_cours} projet(s) en cours"
        if ok:
            commentaire = (
                f"Capacité suffisante pour constituer une équipe projet ({projets}) : "
                f"{remaining} % de capacité restante pour un minimum requis de {seuil} %."
            )
        elif not global_ok:
            commentaire = (
                f"Capacité sous tension ({projets}) : {remaining} % de capacité restante, "
                f"sous le minimum requis de {seuil} %."
            )
            if not poles_ok:
                commentaire += f" Pôle le plus contraint : {tightest_pole} à {tightest_pct} % de disponibilité."
        else:
            commentaire = (
                f"Capacité sous tension ({projets}) : {remaining} % de capacité globale restante, "
                f"mais le pôle {tightest_pole} n'a que {tightest_pct} % de disponibilité "
                f"pour un minimum requis de {seuil} %."
            )

        return CapacityResult(
            charge_actuelle_pct=charge,
            capacite_restante_pct=remaining,
            equipe_disponible=ok,
            commentaire=commentaire,
        )
