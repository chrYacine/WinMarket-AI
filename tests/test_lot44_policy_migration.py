"""Lot 44 — migration 0010 (explicit, versioned criteria) on a POPULATED database.

Proves, on a temporary SQLite database only:
- the back-fill materializes the historical rules of EXISTING policies
  faithfully (same weights, thresholds, decisions, notes, unknown-data
  handling) and keeps every status, owner and organization scope — no policy
  is activated or created;
- `analyses` (and their stored `result_data`) are byte-for-byte untouched;
- the golden results recorded BEFORE the lot-43 cleanup are reproduced from
  the criteria READ BACK FROM THE DATABASE (historical compatibility only —
  never a required score for a new configuration);
- the back-fill is idempotent, and the downgrade is refused (with a clear
  error) as soon as a policy was authored in the new format, otherwise it is
  lossless;
- the migration's frozen copy of the materialization equals the application's.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from src.agents import criteria_catalogue
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact, RAGEvidence
from tests.test_b02_migration import (
    REV_0001, REV_0002, REV_0003, REV_0004, REV_0005, REV_0006, REV_0007, REV_0008, REV_0009, MIGRATIONS_DIR, _load_revision,
    _run_migration,
)

REV_0010 = _load_revision("0010_lot44_policy_criteria.py")
GOLDEN = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "lot43_scoring_golden_before_cleanup.json").read_text(encoding="utf-8"))
DEMO = GOLDEN["demo_policy"]


def _hex() -> str:
    return uuid.uuid4().hex


def _run(db_path, fn):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                result = fn(conn)
            conn.commit()
            return result
    finally:
        engine.dispose()


@pytest.fixture()
def db_at_0009(tmp_path, monkeypatch):
    from src.core import config
    from src.web.database import session as db_session_module

    path = tmp_path / f"m44_{uuid.uuid4().hex}.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)
    _run_migration(path, REV_0001, REV_0002, REV_0003, REV_0004, REV_0005, REV_0006, REV_0007, REV_0008, REV_0009)
    yield path
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)


def _insert_policy(conn, *, org, owner, version, status, weights, rules, custom, go=88, sr=60):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(text(
        "INSERT INTO scoring_policies (id,organization_id,owner_user_id,version,status,weights,threshold_go,threshold_sous_reserve,"
        "business_rules,custom_criteria,created_by_user_id,created_at,updated_at) "
        "VALUES (:id,:org,:owner,:v,:st,:w,:go,:sr,:br,:cc,:owner,:now,:now)"
    ), {"id": _hex(), "org": org, "owner": owner, "v": version, "st": status, "w": json.dumps(weights), "go": go, "sr": sr,
        "br": json.dumps(rules), "cc": json.dumps(custom), "now": now})


CLEANING_CUSTOM = [
    {"id": "zone_couverte", "label": "Sites couverts", "fact_key": "zone_intervention", "operator": "list_coverage",
     "weight": 8, "blocking": True, "pass_score": 100, "fail_score": 0},
    {"id": "frequence_ok", "label": "Fréquence compatible", "fact_key": "frequence_nettoyage", "operator": "numeric_threshold",
     "comparison": "provider_gte_ao", "weight": 5, "blocking": False, "pass_score": 100, "fail_score": 20},
]
CLEANING_WEIGHTS = {
    "Adequation expertise": 0, "References similaires": 10, "Disponibilite equipe": 15, "Rentabilite estimee": 15,
    "Faisabilite delai": 10, "Certifications requises": 10, "Complexite technique": 0, "Connaissance secteur": 10,
    "Potentiel commercial": 5, "Risque contractuel": 5, "Solidite client": 5, "Valeur strategique": 0,
}
DEMO_RULES = {k: DEMO[k] for k in ("budget_minimum_eur", "max_charge_pct", "max_unmastered_technologies", "certification_penalty_score")}


def _populate(db_path):
    """Two owners: an active IT policy + an archived older one + a draft for
    owner A, an active cleaning policy with custom criteria and NO business
    rules (never configured) for owner B; plus a real persisted analysis."""
    from src.web.auth import service as auth_service
    from src.web.database import session as db_session_module
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    from src.web.database.repositories import users as users_repo

    with db_session_module.session_scope() as db:
        ids = {}
        for tag in ("a", "b"):
            user = users_repo.create_user(db, email=f"m44{tag}@example.com", password_hash=auth_service.hash_password("Sup3rSecret!"),
                                          first_name="M", last_name=tag.upper(), status="active")
            org = organizations_repo.create_organization(db, name=f"Org {tag}")
            memberships_repo.create_membership(db, user_id=user.id, organization_id=org.id, role="organization_admin", status="active")
            ids[tag] = (org.id.hex, user.id.hex, org.id, user.id)
        db.flush()
    # Lot 49 (backward compatibility note): this analysis row is inserted by RAW SQL, listing only the
    # columns that exist at revision 0009 — never through the ORM `Analysis` class, which now also declares
    # `parent_job_id` (added by migration 0012, additive but later): the ORM model always reflects the
    # CURRENT schema, while this fixture deliberately freezes the database at 0009 to test migration 0010's
    # OWN behavior on it. Using the ORM here would break every time a later, unrelated migration adds a
    # column — exactly like `_insert_policy` above already avoids it for `scoring_policies`.
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        stored = {"decision": "GO", "score_global": 90.5, "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 100.0,
                  "justification": "2/2 competences couvertes par l'ESN."}], "scoring_completeness": "complete"}
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(text(
            "INSERT INTO analyses (id, user_id, organization_id, job_id, title, client_name, sector, score, decision, "
            "budget, technologies, result_data, summary_data, created_at, updated_at) "
            "VALUES (:id,:user_id,:org_id,:job_id,:title,:client,:sector,:score,:decision,:budget,:technologies,:result_data,:summary_data,:now,:now)"
        ), {"id": _hex(), "user_id": ids["a"][1], "org_id": ids["a"][0], "job_id": "job-m44", "title": "AO historique",
            "client": "Client", "sector": "Public", "score": 90.5, "decision": "GO", "budget": "250000",
            "technologies": json.dumps(["python"]), "result_data": json.dumps(stored), "summary_data": None, "now": now})
    engine.dispose()
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        (org_a, user_a, _, _), (org_b, user_b, _, _) = ids["a"], ids["b"]
        _insert_policy(conn, org=org_a, owner=user_a, version=1, status="archived", weights=DEMO["weights"], rules={}, custom=[], go=85, sr=50)
        _insert_policy(conn, org=org_a, owner=user_a, version=2, status="active", weights=DEMO["weights"], rules=DEMO_RULES, custom=[],
                       go=DEMO["threshold_go"], sr=DEMO["threshold_sous_reserve"])
        _insert_policy(conn, org=org_a, owner=user_a, version=3, status="draft", weights=DEMO["weights"], rules=DEMO_RULES, custom=[])
        _insert_policy(conn, org=org_b, owner=user_b, version=1, status="active", weights=CLEANING_WEIGHTS, rules={}, custom=CLEANING_CUSTOM,
                       go=80, sr=55)
    engine.dispose()
    return ids


def _table_digests(db_path, exclude=("scoring_policies",)):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    out = {}
    with engine.connect() as conn:
        for (name,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")):
            if name in exclude:
                continue
            rows = conn.execute(text(f"SELECT * FROM {name} ORDER BY 1")).fetchall()
            out[name] = hashlib.sha256(json.dumps([[str(c) for c in r] for r in rows]).encode()).hexdigest()
    engine.dispose()
    return out


def _policy_rows(db_path, cols):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {cols} FROM scoring_policies ORDER BY owner_user_id, version")).fetchall()
    engine.dispose()
    return rows


def test_backfill_materializes_historical_policies_faithfully_and_touches_nothing_else(db_at_0009):
    ids = _populate(db_at_0009)
    legacy_cols = "id,organization_id,owner_user_id,version,status,threshold_go,threshold_sous_reserve,weights,business_rules,custom_criteria,created_at,activated_at"
    before_tables = _table_digests(db_at_0009)
    before_policies = _policy_rows(db_at_0009, legacy_cols)
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.connect() as conn:
        analysis_before = conn.execute(text("SELECT result_data FROM analyses")).scalar()
    engine.dispose()

    _run_migration(db_at_0009, REV_0010)

    # nothing outside scoring_policies changed, and the stored analysis JSON is byte-identical
    assert _table_digests(db_at_0009) == before_tables
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT result_data FROM analyses")).scalar() == analysis_before
    engine.dispose()
    # every historical column, status, owner, organization and version preserved
    assert _policy_rows(db_at_0009, legacy_cols) == before_policies
    rows = _policy_rows(db_at_0009, "owner_user_id,version,status,criteria_version,origin,criteria,settings,weights,business_rules,custom_criteria")
    assert len(rows) == 4
    statuses = [(r.version, r.status) for r in rows if r.owner_user_id == ids["a"][1]]
    assert statuses == [(1, "archived"), (2, "active"), (3, "draft")], "statuses preserved, no policy activated"
    assert sum(1 for r in rows if r.status == "active") == 2, "one active policy per owner, unchanged"
    for r in rows:
        assert r.criteria_version == 1 and r.origin == "legacy"
        expected, expected_settings = criteria_catalogue.materialize_legacy(
            weights=json.loads(r.weights), business_rules=json.loads(r.business_rules), custom_criteria=json.loads(r.custom_criteria))
        assert json.loads(r.criteria) == expected and json.loads(r.settings) == expected_settings
    # the never-configured rules stay "never configured" (still INCOMPLET), not silently filled
    archived = next(r for r in rows if r.version == 1 and r.owner_user_id == ids["a"][1])
    assert sorted(json.loads(archived.settings)["legacy_unconfigured_rules"]) == [
        "budget_minimum_eur", "certification_penalty_score", "max_charge_pct", "max_unmastered_technologies"]
    cleaning = next(r for r in rows if r.owner_user_id == ids["b"][1])
    assert [c["id"] for c in json.loads(cleaning.criteria)][-2:] == ["zone_couverte", "frequence_ok"]


def _snapshot_from_row(row) -> ScoringPolicySnapshot:
    return ScoringPolicySnapshot(
        criteria=json.loads(row.criteria), settings=json.loads(row.settings), threshold_go=row.threshold_go,
        threshold_sous_reserve=row.threshold_sous_reserve, mastered_technologies=frozenset(DEMO["mastered_technologies"]),
        certifications_held=frozenset(DEMO["certifications_held"]), origin=row.origin, criteria_version=row.criteria_version,
    )


def test_the_criteria_read_back_from_the_database_reproduce_the_recorded_historical_results(db_at_0009):
    """Historical compatibility only: the IT policy's criteria, as stored by
    the migration, give the results recorded before the cleanup (the wording
    of the budget blocker aside, see tests/test_lot43_cleanup.py)."""
    from tests.test_lot43_cleanup import _stable

    _populate(db_at_0009)
    _run_migration(db_at_0009, REV_0010)
    it_row = next(r for r in _policy_rows(db_at_0009, "version,status,criteria,settings,threshold_go,threshold_sous_reserve,origin,criteria_version,owner_user_id")
                  if r.status == "active" and r.threshold_go == DEMO["threshold_go"])
    snapshot = _snapshot_from_row(it_row)
    for name, case in GOLDEN["legacy_cases"].items():
        inp = case["inputs"]
        result = ScoringEngine().score(
            AOContext(**inp["ao"]), CompanyProfile(**inp["company"]), [RAGEvidence(**e) for e in inp["evidences"]],
            CapacityResult(**inp["capacity"]), policy=snapshot)
        expected = case["expected"]
        assert (result.decision, result.score_global) == (expected["decision"], expected["score_global"]), name
        assert [[c.nom, c.poids, c.score] for c in result.criteres] == expected["criteres"], name
        assert _stable(list(result.criteres_bloquants)) == _stable(expected["blockers"]), name


def test_backfill_is_idempotent(db_at_0009):
    _populate(db_at_0009)
    _run_migration(db_at_0009, REV_0010)
    first = _policy_rows(db_at_0009, "id,criteria_version,origin,criteria,settings")
    touched = _run(db_at_0009, lambda conn: REV_0010.backfill(conn))
    assert touched == 0, "only rows still at criteria_version = 0 are ever processed"
    assert _policy_rows(db_at_0009, "id,criteria_version,origin,criteria,settings") == first


def test_backfill_leaves_a_user_authored_row_alone(db_at_0009):
    _populate(db_at_0009)
    _run_migration(db_at_0009, REV_0010)
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.begin() as conn:
        conn.execute(text("UPDATE scoring_policies SET origin='user', criteria='[{\"id\": \"mien\"}]', weights='{}' WHERE version=3"))
    engine.dispose()
    assert _run(db_at_0009, lambda conn: REV_0010.backfill(conn)) == 0
    row = next(r for r in _policy_rows(db_at_0009, "version,origin,criteria") if r.version == 3 and r.origin == "user")
    assert json.loads(row.criteria) == [{"id": "mien"}]


def test_downgrade_is_lossless_when_every_policy_is_still_historical(db_at_0009):
    _populate(db_at_0009)
    legacy_cols = "id,version,status,weights,business_rules,custom_criteria,threshold_go"
    before = _policy_rows(db_at_0009, legacy_cols)
    _run_migration(db_at_0009, REV_0010)
    _run(db_at_0009, lambda conn: REV_0010.downgrade())
    assert _policy_rows(db_at_0009, legacy_cols) == before
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.connect() as conn:
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(scoring_policies)"))}
    engine.dispose()
    assert not {"criteria", "criteria_version", "settings", "origin"} & columns
    _run_migration(db_at_0009, REV_0010)  # and it can be applied again


def test_downgrade_is_refused_when_a_policy_was_authored_in_the_new_format(db_at_0009):
    _populate(db_at_0009)
    _run_migration(db_at_0009, REV_0010)
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.begin() as conn:
        conn.execute(text("UPDATE scoring_policies SET origin='user' WHERE version=3"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="Downgrade of 0010 refused"):
        _run(db_at_0009, lambda conn: REV_0010.downgrade())
    engine = create_engine(f"sqlite:///{db_at_0009}", future=True)
    with engine.connect() as conn:
        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(scoring_policies)"))}
    engine.dispose()
    assert {"criteria", "origin"} <= columns, "a refused downgrade drops nothing"


@pytest.mark.parametrize("weights,rules,custom", [
    (DEMO["weights"], DEMO_RULES, []),
    (DEMO["weights"], {}, []),
    (DEMO["weights"], {"budget_minimum_eur": 0, "max_charge_pct": 100}, CLEANING_CUSTOM),
    (CLEANING_WEIGHTS, DEMO_RULES, CLEANING_CUSTOM + ["not-a-dict", {"id": "x", "operator": "nope"}]),
    ({"Adequation expertise": 100, "Inconnu": 0}, {}, {"id": "not-a-list"}),
    ("not-a-dict", None, None),
])
def test_the_migrations_frozen_materialization_equals_the_applications(weights, rules, custom):
    """A migration must keep meaning what it meant when written; while both
    are at schema version 1 they must agree (a divergence is a decision to
    take consciously, not a drift)."""
    assert REV_0010._materialize_legacy(weights, rules, custom) == criteria_catalogue.materialize_legacy(
        weights=weights, business_rules=rules, custom_criteria=custom)


def test_the_migration_is_the_next_free_revision_and_chains_from_0009():
    assert REV_0010.revision == "0010" and REV_0010.down_revision == "0009"
    # later revisions chain on top of it (0011: lot 47 bis, additive); 0010 stays exactly where lot 44 put it
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("00*.py"))
    assert "0010_lot44_policy_criteria.py" in names and names.index("0010_lot44_policy_criteria.py") == 9
