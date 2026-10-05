"""B08-T1: capacity comes from the account's own declared numbers only —
never from what the AO happens to ask for.

No DB, no file I/O. Lot 43: `CapacityAnalyzer` has no file-backed repository
and no default plan any more — the caller always passes the account's own
`CapacityPlan`, and a missing one is refused.
"""
import pytest

from src.agents.capacity_analyzer import CapacityAnalyzer
from src.core.capacity_plan import CapacityPlan
from src.core.models import AOContext
from src.core.private_configuration import PrivateConfigurationRequired

# The exact inputs that used to trigger the deleted surcharges: a "heavy"
# technology (+10 points and a forced equipe_disponible=False) and >= 6
# technologies (+8 points).
_OLD_SURCHARGE_TRIGGERS = ["sap", "mainframe", "cobol", "blockchain", "x", "y"]


def _plan(**overrides) -> CapacityPlan:
    base = dict(
        charge_globale_pct=70,
        nombre_projets_en_cours=3,
        projets_en_cours=["Projet A", "Projet B", "Projet C"],
        capacites_par_pole={"Software Engineering": 40, "Data & IA": 25},
        disponibilite_minimum_pct=10,
    )
    base.update(overrides)
    return CapacityPlan(**base)


def test_ao_technologies_have_zero_influence():
    plan = _plan()
    heavy_ao = AOContext(titre="AO", technologies_demandees=list(_OLD_SURCHARGE_TRIGGERS))
    empty_ao = AOContext(titre="AO", technologies_demandees=[])

    heavy = CapacityAnalyzer().analyze(heavy_ao, plan=plan)
    empty = CapacityAnalyzer().analyze(empty_ao, plan=plan)

    assert heavy.model_dump() == empty.model_dump()
    # And the numbers are the account's own, with no surcharge applied.
    assert heavy.charge_actuelle_pct == 70
    assert heavy.capacite_restante_pct == 30
    assert heavy.equipe_disponible is True


def test_commentaire_never_mentions_technology_reasoning():
    plan = _plan(charge_globale_pct=95)  # remaining 5 < 10 -> sous tension
    ao = AOContext(titre="AO", technologies_demandees=list(_OLD_SURCHARGE_TRIGGERS))

    result = CapacityAnalyzer().analyze(ao, plan=plan)

    assert result.equipe_disponible is False
    assert "compétence critique" not in result.commentaire
    for technology in _OLD_SURCHARGE_TRIGGERS:
        assert technology not in result.commentaire.lower()
    # Grounded in the real numbers instead.
    assert "5 %" in result.commentaire and "10 %" in result.commentaire


def test_disponibilite_minimum_pct_gates_equipe_disponible():
    ao = AOContext(titre="AO")
    permissive = _plan(charge_globale_pct=85, disponibilite_minimum_pct=10)   # remaining 15 >= 10
    strict = _plan(charge_globale_pct=85, disponibilite_minimum_pct=20)       # remaining 15 <  20

    lenient_result = CapacityAnalyzer().analyze(ao, plan=permissive)
    strict_result = CapacityAnalyzer().analyze(ao, plan=strict)

    assert lenient_result.capacite_restante_pct == strict_result.capacite_restante_pct == 15
    assert lenient_result.equipe_disponible is True
    assert strict_result.equipe_disponible is False


def test_capacites_par_pole_is_an_additional_real_signal():
    ao = AOContext(titre="AO")
    # Global capacity alone would pass (remaining 40 >= 15), but one real,
    # configured pole is below the account's own threshold.
    plan = _plan(
        charge_globale_pct=60,
        disponibilite_minimum_pct=15,
        capacites_par_pole={"Software Engineering": 40, "Cloud/DevOps": 8},
    )

    result = CapacityAnalyzer().analyze(ao, plan=plan)

    assert result.capacite_restante_pct == 40
    assert result.equipe_disponible is False
    # The tightest pole is named with its real name and its real value —
    # never a pole invented from the global percentage.
    assert "Cloud/DevOps" in result.commentaire
    assert "8 %" in result.commentaire
    assert "Software Engineering" not in result.commentaire


def test_capacites_par_pole_never_changes_the_percentages():
    ao = AOContext(titre="AO")
    generous = _plan(charge_globale_pct=60, capacites_par_pole={"Software Engineering": 90, "Data & IA": 80})
    tight = _plan(charge_globale_pct=60, capacites_par_pole={"Software Engineering": 5, "Data & IA": 4})

    a = CapacityAnalyzer().analyze(ao, plan=generous)
    b = CapacityAnalyzer().analyze(ao, plan=tight)

    # Same charge_globale_pct -> identical percentages; capacites_par_pole
    # only gates the boolean and the wording, it is never added in.
    assert a.charge_actuelle_pct == b.charge_actuelle_pct == 60
    assert a.capacite_restante_pct == b.capacite_restante_pct == 40
    assert a.equipe_disponible is True
    assert b.equipe_disponible is False


def test_empty_capacites_par_pole_is_not_treated_as_a_failure():
    ao = AOContext(titre="AO")
    plan = _plan(charge_globale_pct=60, capacites_par_pole={}, disponibilite_minimum_pct=15)

    result = CapacityAnalyzer().analyze(ao, plan=plan)

    assert result.equipe_disponible is True


def test_the_former_demo_thresholds_reproduce_the_old_hardcoded_behaviour_only_when_passed_explicitly():
    """Lot 43 (replaces the test of the removed file-backed default plan): the
    old default (charge 78, minimum 10 -> remaining 22, available) is not
    applied by anything any more — a plan carrying those SAME numbers, passed
    explicitly, gives the same result."""
    ao = AOContext(titre="AO")

    result = CapacityAnalyzer().analyze(ao, plan=CapacityPlan(charge_globale_pct=78, disponibilite_minimum_pct=10))

    assert result.charge_actuelle_pct == 78
    assert result.capacite_restante_pct == 22
    assert result.equipe_disponible is True
    assert "3 projet" not in result.commentaire  # uses the plan's own count (0), nothing invented


def test_a_missing_plan_is_refused_never_replaced_by_a_default():
    with pytest.raises(PrivateConfigurationRequired) as excinfo:
        CapacityAnalyzer().analyze(AOContext(titre="AO"), plan=None)
    assert excinfo.value.missing == "capacity_plan"


def test_a_capacity_plan_has_no_demo_default_for_the_real_numbers():
    with pytest.raises(TypeError):
        CapacityPlan()  # type: ignore[call-arg] — load and minimum are required
    with pytest.raises(TypeError):
        CapacityPlan(charge_globale_pct=50)  # type: ignore[call-arg] — the account's own minimum is required too
