"""Lot 44 — explicit, versioned criteria on scoring policies.

Adds four columns to `scoring_policies` and back-fills the EXISTING rows with
a faithful, explicit representation of the historical scoring rules they were
already using:

- `criteria_version` (integer): the schema version of the `criteria` payload.
  DISTINCT from the business `version` of the policy (each activation of a
  new draft still allocates the next business version). 0 = "not
  materialized yet" (every pre-0010 row, only until the back-fill below
  runs), 1 = the first explicit-criteria schema.
- `criteria` (JSON list): the criteria the engine evaluates.
- `settings` (JSON object): policy-level settings (how a criterion found "not
  applicable" during an analysis is weighted, strength/weakness display
  thresholds, historical business rules that were never configured).
- `origin` ('legacy' | 'user'): 'legacy' = the criteria are the materialized
  historical rules and the pre-0010 columns (`weights`, `business_rules`,
  `custom_criteria`) remain authoritative and consistent; 'user' = the
  criteria were authored in the new format and those columns are empty.

The back-fill is a REPRESENTATION migration, not an approval by the account of
its historical values: same weights, thresholds, notes, unknown-data handling
and blocking rules, every status (active / draft / archived) and every owner
scope preserved, no policy activated or created. It is idempotent (it only
touches rows still at `criteria_version = 0`). `analyses` and their stored
`result_data` are NOT touched: historical results are never rewritten.

`_materialize_legacy` below is a FROZEN copy of
src.agents.criteria_catalogue.materialize_legacy as of schema version 1 (a
migration must keep meaning what it meant when it was written);
tests/test_lot44_policy_migration.py asserts the two agree.

Downgrade: refused when any policy was created or edited in the new format
(`origin = 'user'`), because dropping the columns would lose it — restore a
verified backup instead. Only when every row is still `origin = 'legacy'`
(whose legacy columns are untouched) are the columns dropped, without loss.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-19
"""
import json
import math

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_JSON = JSONB().with_variant(sa.JSON(), "sqlite")

_LABELS = {
    "Adequation expertise": "Adéquation expertise", "References similaires": "Références similaires",
    "Disponibilite equipe": "Disponibilité équipe", "Rentabilite estimee": "Rentabilité estimée",
    "Faisabilite delai": "Faisabilité délai", "Certifications requises": "Certifications requises",
    "Complexite technique": "Complexité technique", "Connaissance secteur": "Connaissance secteur",
    "Potentiel commercial": "Potentiel commercial", "Risque contractuel": "Risque contractuel",
    "Solidite client": "Solidité client", "Valeur strategique": "Valeur stratégique",
}
_IDS = {
    "Adequation expertise": "adequation_expertise", "References similaires": "references_similaires",
    "Disponibilite equipe": "disponibilite_equipe", "Rentabilite estimee": "rentabilite_estimee",
    "Faisabilite delai": "faisabilite_delai", "Certifications requises": "certifications_requises",
    "Complexite technique": "complexite_technique", "Connaissance secteur": "connaissance_secteur",
    "Potentiel commercial": "potentiel_commercial", "Risque contractuel": "risque_contractuel",
    "Solidite client": "solidite_client", "Valeur strategique": "valeur_strategique",
}
_FACT_EVALUATORS = ("list_coverage", "numeric_threshold", "equality")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _base(spec_id, label, evaluator, params, weight, *, blocking=False, on_missing=None, legacy_key=None):
    out = {"id": spec_id, "label": label, "evaluator": evaluator, "params": params, "weight": weight,
           "blocking": blocking, "on_missing": on_missing or {"mode": "incomplete"}, "enabled": True, "disabled_reason": None}
    if legacy_key:
        out["legacy_key"] = legacy_key
    return out


def _materialize_legacy(weights, business_rules, custom_criteria):
    rules = business_rules if isinstance(business_rules, dict) else {}

    def rule(name):
        value = rules.get(name)
        return value if _finite(value) else None

    settings = {"not_applicable_rule": "incomplete", "not_applicable_rule_confirmed": False,
                "strengths_at_least": 78, "weaknesses_below": 60,
                "legacy_unconfigured_rules": [n for n in ("budget_minimum_eur", "max_charge_pct", "max_unmastered_technologies",
                                                          "certification_penalty_score") if rule(n) is None]}
    budget_min, max_charge = rule("budget_minimum_eur"), rule("max_charge_pct")
    max_unmastered, cert_penalty = rule("max_unmastered_technologies"), rule("certification_penalty_score")
    B = lambda k, ev, params, w, **kw: _base(_IDS[k], _LABELS[k], ev, params, w, legacy_key=k, **kw)  # noqa: E731
    builders = {
        "Adequation expertise": lambda w, k: B(k, "technology_coverage", {"none_requested_score": 80, "max_unmastered_blocking": max_unmastered}, w),
        "References similaires": lambda w, k: B(k, "reference_evidence", {"base_score": 30, "per_reference": 6, "similarity_weight": 40}, w),
        "Disponibilite equipe": lambda w, k: B(k, "capacity_availability", {"available_score": 90, "unavailable_score": 45, "max_charge_blocking": max_charge}, w),
        "Rentabilite estimee": lambda w, k: B(k, "numeric_tiers", {
            "source": "budget", "fact_key": None,
            "tiers": [{"at_least": 200000, "score": 90}, {"at_least": 100000, "score": 80}, {"at_least": 60000, "score": 55}],
            "below_score": 40, "zero_score": 65, "minimum_blocking": budget_min}, w, on_missing={"mode": "explicit_score", "score": 65}),
        "Faisabilite delai": lambda w, k: B(k, "legacy_tight_deadline_v1", {"tight_score": 45, "otherwise_score": 75}, w),
        "Certifications requises": lambda w, k: B(k, "certifications_required", {
            "covered_score": 100, "missing_score": cert_penalty, "block_when_missing": True, "when_extraction_unknown": "treat_as_none"}, w),
        "Complexite technique": lambda w, k: B(k, "technology_count_tiers", {
            "tiers": [{"at_most": 4, "score": 80}, {"at_most": 7, "score": 60}], "above_score": 40, "empty_is_unknown": False}, w),
        "Connaissance secteur": lambda w, k: B(k, "legacy_sector_known_v1", {"known_score": 80}, w, on_missing={"mode": "explicit_score", "score": 65}),
        "Potentiel commercial": lambda w, k: B(k, "numeric_tiers", {
            "source": "budget", "fact_key": None, "tiers": [{"at_least": 150000, "score": 85}, {"at_least": 70000, "score": 60}],
            "below_score": 40, "zero_score": None, "minimum_blocking": None}, w, on_missing={"mode": "explicit_score", "score": 40}),
        "Risque contractuel": lambda w, k: B(k, "legacy_contract_clauses_v1", {"clauses_score": 60, "otherwise_score": 80}, w),
        "Solidite client": lambda w, k: B(k, "legacy_client_solvency_v1", {"favorable_score": 85, "otherwise_score": 60}, w),
        "Valeur strategique": lambda w, k: B(k, "keyword_set_match", {
            "any_of": ["ia", "rag", "llm", "cloud", "azure", "aws", "data"], "match_score": 90, "no_match_score": 70,
            "empty_is_unknown": False}, w),
    }
    criteria = []
    if isinstance(weights, dict):
        for key, weight in weights.items():
            builder = builders.get(key)
            criteria.append(builder(weight, key) if builder else _base(f"invalide_{len(criteria)}", str(key), "invalid", {}, weight))
    if isinstance(custom_criteria, list):
        for index, spec in enumerate(custom_criteria):
            if not isinstance(spec, dict):
                criteria.append(_base(f"invalide_{index}", f"invalide_{index}", "invalid", {}, 0))
                continue
            operator = spec.get("operator")
            params = {"fact_key": spec.get("fact_key"), "pass_score": spec.get("pass_score"), "fail_score": spec.get("fail_score")}
            if operator == "numeric_threshold" or "comparison" in spec:
                params["comparison"] = spec.get("comparison")
            spec_id = spec.get("id") if isinstance(spec.get("id"), str) and spec.get("id") else f"invalide_{index}"
            label = spec.get("label") if isinstance(spec.get("label"), str) and spec.get("label").strip() else spec_id
            evaluator = operator if isinstance(operator, str) and operator in _FACT_EVALUATORS else "invalid"
            criteria.append(_base(spec_id, label, evaluator, params if evaluator != "invalid" else {}, spec.get("weight"),
                                  blocking=spec.get("blocking") if isinstance(spec.get("blocking"), bool) else False))
    elif custom_criteria not in (None, []):
        criteria.append(_base("configuration", "configuration", "invalid", {}, 0))
    return criteria, settings


def _as_json(value, default):
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def backfill(bind) -> int:
    """Materialize every row still at criteria_version = 0. Idempotent."""
    table = sa.table(
        "scoring_policies", sa.column("id"), sa.column("weights", _JSON), sa.column("business_rules", _JSON),
        sa.column("custom_criteria", _JSON), sa.column("criteria_version", sa.Integer), sa.column("criteria", _JSON),
        sa.column("settings", _JSON), sa.column("origin", sa.String),
    )
    rows = bind.execute(sa.select(table.c.id, table.c.weights, table.c.business_rules, table.c.custom_criteria)
                        .where(table.c.criteria_version == 0)).fetchall()
    for row in rows:
        criteria, settings = _materialize_legacy(_as_json(row.weights, {}), _as_json(row.business_rules, {}),
                                                 _as_json(row.custom_criteria, []))
        bind.execute(table.update().where(table.c.id == row.id).values(
            criteria=criteria, settings=settings, criteria_version=1, origin="legacy"))
    return len(rows)


def upgrade() -> None:
    op.add_column("scoring_policies", sa.Column("criteria_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scoring_policies", sa.Column("criteria", _JSON, nullable=False, server_default="[]"))
    op.add_column("scoring_policies", sa.Column("settings", _JSON, nullable=False, server_default="{}"))
    op.add_column("scoring_policies", sa.Column("origin", sa.String(20), nullable=False, server_default="user"))
    backfill(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    lost = bind.execute(sa.text("SELECT COUNT(*) FROM scoring_policies WHERE origin <> 'legacy'")).scalar()
    if lost:
        raise RuntimeError(
            f"Downgrade of 0010 refused: {lost} scoring policy(ies) were created or edited in the explicit-criteria "
            "format and would be lost. Restore a verified backup taken before 0010 instead."
        )
    op.drop_column("scoring_policies", "origin")
    op.drop_column("scoring_policies", "settings")
    op.drop_column("scoring_policies", "criteria")
    op.drop_column("scoring_policies", "criteria_version")
