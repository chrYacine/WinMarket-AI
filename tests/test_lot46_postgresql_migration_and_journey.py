"""Lot 46 — what only a REAL PostgreSQL can qualify, beyond tests/test_b25_t1_postgresql_qualification.py:

A. the chain reaches Alembic head from an empty database and the resulting schema is the models' schema
   (tables, columns, nullability, JSONB, the named constraints/indexes);
B. migration 0010 on a POPULATED pre-0010 schema (several spaces, active / draft / archived policies,
   analyses, a private document): history untouched (compared canonically — key order is not a change),
   legacy columns preserved, back-fill faithful, idempotent, downgrade lossless or refused, criteria read back
   from PostgreSQL still reproduce the recorded historical results;
C. the private-scope constraints are really enforced by PostgreSQL (one active policy, organization/analysis/
   document coherence, uniqueness, checks) and the repository activation keeps exactly one active policy;
D. a short server journey on PostgreSQL: private configuration -> private document + search -> analysis (AI
   simulated at the LLM adapter) -> result page / history / deliverables, with history and documents limited
   to the right user and the selected organization.

Same guard as the B25 file (tests/pg_support.py): disposable database only, skipped without
WM_POSTGRES_TEST_URL, FAILS without it when WM_REQUIRE_POSTGRES=1. Sequential operations here say NOTHING about
multi-process concurrency, which stays unqualified.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from src.agents import criteria_catalogue
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)
from tests.test_lot44_policy_migration import CLEANING_CUSTOM, CLEANING_WEIGHTS, DEMO, DEMO_RULES, GOLDEN, REV_0010

LEGACY_COLUMNS = ("id,organization_id,owner_user_id,version,status,threshold_go,threshold_sous_reserve,weights,business_rules,"
                  "custom_criteria,created_by_user_id,created_at,updated_at,activated_at")
NEW_COLUMNS = {"criteria_version", "criteria", "settings", "origin"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def canon(value) -> str:
    """Canonical JSON: key order and number spelling are not changes of content."""
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def alembic_version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def table_digests(engine, exclude=()) -> dict[str, str]:
    out = {}
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"))]
        for name in names:
            if name == "alembic_version" or name in exclude:
                continue
            rows = conn.execute(text(f'SELECT * FROM "{name}" ORDER BY 1')).fetchall()
            out[name] = hashlib.sha256(canon([list(r) for r in rows]).encode()).hexdigest()
    return out


def policy_rows(engine, columns: str):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT {columns} FROM scoring_policies ORDER BY owner_user_id, organization_id, version")).fetchall()


def insert_policy(engine, *, org, owner, version, status, weights, rules, custom, go=88.0, sr=60.0):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO scoring_policies (id,organization_id,owner_user_id,version,status,weights,threshold_go,threshold_sous_reserve,"
            "business_rules,custom_criteria,created_by_user_id,created_at,updated_at,activated_at) "
            "VALUES (:id,:org,:owner,:v,:st,CAST(:w AS JSONB),:go,:sr,CAST(:br AS JSONB),CAST(:cc AS JSONB),:owner,:now,:now,:act)"
        ), {"id": uuid.uuid4(), "org": org, "owner": owner, "v": version, "st": status, "w": json.dumps(weights), "go": go, "sr": sr,
            "br": json.dumps(rules), "cc": json.dumps(custom), "now": now, "act": now if status != "draft" else None})


STORED = {
    "a": {"score_global": 90.5, "decision": "GO", "id": "pg44-job-a", "scoring_completeness": "complete",
          "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 100.0, "justification": "2/2 compétences « couvertes » — 90,5 %."}],
          "risques": [], "note": None, "nested": {"z": 1, "a": [3, 2, 1], "unicode": "é ü — 日本"}},
    "b": {"decision": "NO-GO", "score_global": 12.0, "criteres": [], "extra": {"k": [1.5, None, True]}},
    "c": {"decision": "INCOMPLET", "score_global": 40.0, "scoring_missing": ["budget_minimum_eur"], "criteres": [], "score_provisoire": None},
}


def insert_analysis_pre_0010(engine, *, user_id, organization_id, job_id, title, client_name, sector, score, decision,
                              budget, technologies, result_data, summary_data) -> None:
    """Raw SQL, the EXACT 0009-era `analyses` schema (verified by inspecting a real database upgraded only to
    0009: id, user_id, organization_id, job_id, title, client_name, sector, score, decision, budget,
    technologies, result_data, summary_data, created_at, updated_at — no parent_job_id [0012], no
    origin_job_id [0014]). `analyses_repo.create_analysis` (the ORM repository) cannot be used here: SQLAlchemy
    always includes EVERY column the CURRENT `Analysis` model maps in its generated INSERT, regardless of
    what actually exists in the database being written to — reproduced for real once lot 51 made this file's
    PostgreSQL tests actually execute (they used to always SKIP, no real server available): `column
    "parent_job_id" of relation "analyses" does not exist`. Same reasoning as `insert_policy` below already
    documented for `scoring_policies` ("the models already know the 0010 columns")."""
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO analyses (id,user_id,organization_id,job_id,title,client_name,sector,score,decision,budget,"
            "technologies,result_data,summary_data,created_at,updated_at) VALUES "
            "(:id,:user_id,:org,:job_id,:title,:client_name,:sector,:score,:decision,:budget,"
            "CAST(:technologies AS JSONB),CAST(:result_data AS JSONB),CAST(:summary_data AS JSONB),:now,:now)"
        ), {"id": uuid.uuid4(), "user_id": user_id, "org": organization_id, "job_id": job_id, "title": title,
            "client_name": client_name, "sector": sector, "score": score, "decision": decision, "budget": budget,
            "technologies": json.dumps(technologies), "result_data": json.dumps(result_data),
            "summary_data": json.dumps(summary_data), "now": now})


def insert_knowledge_document_pre_0010(engine, *, organization_id, owner_user_id, filename: str, paragraphs: list[str]) -> None:
    """Raw SQL, the EXACT 0009-era knowledge_* schema (verified by inspecting a real database upgraded only
    to 0009) — `documents_service.upload_document` cannot be used here for the SAME reason as
    `insert_analysis_pre_0010` above: the ORM's `KnowledgeDocumentVersion` model maps columns added by
    later migrations (content_category_* [0014], embedding_* [0015]) that do not exist yet at 0009. Builds
    one corpus/document/ready-version/chunks-per-paragraph, `active_version_id` published, matching what a
    real upload would have produced structurally (not byte-for-byte — nothing compares against that)."""
    import hashlib
    from pathlib import Path

    now = datetime.now(timezone.utc)
    corpus_id, document_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    raw = "\n\n".join(paragraphs).encode("utf-8")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO knowledge_corpora (id,organization_id,owner_user_id,status,generation,created_at,updated_at) "
            "VALUES (:id,:org,:owner,'active',0,:now,:now)"
        ), {"id": corpus_id, "org": organization_id, "owner": owner_user_id, "now": now})
        conn.execute(text(
            "INSERT INTO knowledge_documents (id,corpus_id,organization_id,owner_user_id,original_filename,status,"
            "active_version_id,created_at,updated_at) VALUES (:id,:corpus,:org,:owner,:filename,'active',NULL,:now,:now)"
        ), {"id": document_id, "corpus": corpus_id, "org": organization_id, "owner": owner_user_id, "filename": filename, "now": now})
        conn.execute(text(
            "INSERT INTO knowledge_document_versions (id,document_id,organization_id,owner_user_id,version_number,"
            "storage_key,content_type_detected,file_size,content_hash,extraction_status,extractor_version,created_at) "
            "VALUES (:id,:doc,:org,:owner,1,'',:ctype,:size,:hash,'ready','b03-v1',:now)"
        ), {"id": version_id, "doc": document_id, "org": organization_id, "owner": owner_user_id,
            "ctype": Path(filename).suffix, "size": len(raw), "hash": hashlib.sha256(raw).hexdigest(), "now": now})
        for i, paragraph in enumerate(paragraphs):
            conn.execute(text(
                "INSERT INTO knowledge_chunks (id,document_version_id,organization_id,owner_user_id,order_index,content) "
                "VALUES (:id,:version,:org,:owner,:i,:content)"
            ), {"id": uuid.uuid4(), "version": version_id, "org": organization_id, "owner": owner_user_id, "i": i, "content": paragraph})
        # published LAST — matches the real ingestion pipeline (a version only ever becomes
        # `active_version_id` once its chunks already exist, ticket B03 section 6).
        conn.execute(text("UPDATE knowledge_documents SET active_version_id = :version WHERE id = :id"),
                     {"version": version_id, "id": document_id})


def populate_pre_0010(engine) -> dict:
    """Two owners in two organizations + one of them in a third space; policies in every status; analyses in
    every space; one private document. Built with the repositories on the 0009 schema (no 0010 column is
    touched by them) and raw SQL for the policies AND the analyses (the models already know later columns —
    see insert_analysis_pre_0010's own docstring for the analyses case, found and fixed this lot)."""
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    from tests.conftest import default_org_id, make_active_starter_user

    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    # Seed the actual historical schema, not the current Subscription ORM.
    from src.web.auth import service as auth_service
    from src.web.database.repositories import users as users_repo
    def historical_user(email):
        user = users_repo.create_user(session, email=email,
            password_hash=auth_service.hash_password("Sup3rSecret!"),
            first_name="Test", last_name="User", status="active")
        auth_service.create_private_organization_for_user(session, user)
        session.execute(text("INSERT INTO subscriptions "
            "(id,user_id,plan,status,started_at,created_at,updated_at) "
            "VALUES (:id,:user,'starter','active',:now,:now,:now)"),
            {"id": uuid.uuid4(), "user": user.id, "now": datetime.now(timezone.utc)})
        session.commit()
        return user
    a = historical_user("pg44-a@example.com")
    b = historical_user("pg44-b@example.com")
    org_a, org_b = default_org_id(session, a), default_org_id(session, b)
    org_c = organizations_repo.create_organization(session, name="Espace C (lot 46)").id
    memberships_repo.create_membership(session, user_id=a.id, organization_id=org_c, role="organization_admin", status="active")
    session.commit()
    for tag, user, org in (("a", a, org_a), ("b", b, org_b), ("c", a, org_c)):
        insert_analysis_pre_0010(
            engine, user_id=user.id, organization_id=org, result_data=STORED[tag], job_id=f"pg44-job-{tag}", title=f"AO {tag}",
            client_name="Client", sector="Public", score=STORED[tag]["score_global"], decision=STORED[tag]["decision"], budget="250000",
            technologies=["python", "é"], summary_data={"tag": tag, "n": 1})
    insert_knowledge_document_pre_0010(engine, organization_id=org_a, owner_user_id=a.id, filename="ref.md",
                                        paragraphs=["# Ref", "MARQUEUR_PG44 reference privee."])
    session.commit()
    session.close()
    insert_policy(engine, org=org_a, owner=a.id, version=1, status="archived", weights=DEMO["weights"], rules={}, custom=[], go=85.0, sr=50.0)
    insert_policy(engine, org=org_a, owner=a.id, version=2, status="active", weights=DEMO["weights"], rules=DEMO_RULES, custom=[],
                  go=DEMO["threshold_go"], sr=DEMO["threshold_sous_reserve"])
    insert_policy(engine, org=org_a, owner=a.id, version=3, status="draft", weights=DEMO["weights"], rules=DEMO_RULES, custom=[])
    insert_policy(engine, org=org_b, owner=b.id, version=1, status="active", weights=CLEANING_WEIGHTS, rules={}, custom=CLEANING_CUSTOM, go=80.0, sr=55.0)
    insert_policy(engine, org=org_c, owner=a.id, version=1, status="active", weights=DEMO["weights"], rules=DEMO_RULES, custom=[])
    return {"a": a.id, "b": b.id, "org_a": org_a, "org_b": org_b, "org_c": org_c}


# ---------------------------------------------------------------------------
# A — empty database -> head; schema == models
# ---------------------------------------------------------------------------

def test_an_empty_database_reaches_head_and_its_schema_is_the_models_schema(pg_engine, pg_url):
    from src.web.database.models import Base

    pg_support.upgrade(pg_url, "head")
    assert alembic_version(pg_engine) == "0017"  # lot 56 adds manual access on top of 0016 — bump per lot, known maintenance pattern
    inspector = inspect(pg_engine)
    db_tables = set(inspector.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)
    assert db_tables == model_tables, (sorted(model_tables - db_tables), sorted(db_tables - model_tables))

    # Lot 51: `knowledge_passages.embedding` is PostgreSQL-only and deliberately NOT declared on the ORM model
    # (src/web/database/models.py::KnowledgePassage's own docstring) — managed exclusively via raw,
    # dialect-checked SQL in src/rag/hybrid_index.py/hybrid_search.py, so a SQLite deployment never needs
    # pgvector at all. This is the one intentional exception to "DB columns == model columns" below.
    EXPECTED_UNMAPPED_COLUMNS = {("knowledge_passages", "embedding")}

    drift = []
    for name, table in Base.metadata.tables.items():
        db_columns = {c["name"]: c for c in inspector.get_columns(name)}
        extra_in_db = {(name, c) for c in set(db_columns) - set(table.columns.keys())}
        missing_in_db = set(table.columns.keys()) - set(db_columns)
        if missing_in_db or (extra_in_db - EXPECTED_UNMAPPED_COLUMNS):
            drift.append((name, "columns", sorted(missing_in_db), sorted(c for _, c in extra_in_db - EXPECTED_UNMAPPED_COLUMNS)))
            continue
        for column in table.columns:
            if db_columns[column.name]["nullable"] != column.nullable and not column.primary_key:
                drift.append((name, column.name, "nullable", column.nullable, db_columns[column.name]["nullable"]))
    assert not drift, drift

    # the JSON columns are real JSONB on PostgreSQL (not text), including the four added by 0010
    with pg_engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='scoring_policies'")).all())
        assert types["criteria"] == "jsonb" and types["settings"] == "jsonb" and types["weights"] == "jsonb"
        assert types["criteria_version"] == "integer" and types["origin"] in ("character varying", "text")
        assert conn.execute(text("SELECT data_type FROM information_schema.columns WHERE table_name='analyses' AND column_name='result_data'")).scalar() == "jsonb"
        # every constraint / index the models NAME EXPLICITLY exists under that name (the `ix_…` names SQLAlchemy
        # generates from `index=True` are not part of this comparison)
        present = {r[0] for r in conn.execute(text("SELECT conname FROM pg_constraint UNION SELECT indexname FROM pg_indexes WHERE schemaname='public'"))}
        named = set()
        for table in Base.metadata.tables.values():
            named |= {c.name for c in table.constraints if c.name}
            named |= {i.name for i in table.indexes if i.name and not i.name.startswith("ix_")}
        assert named - present == set(), sorted(named - present)
        # `password_reset_tokens.token_digest` is declared `unique=True, index=True` (generated name `ix_…`); migration
        # 0008 created the SAME unique index as `uq_password_reset_tokens_token_digest`: a name difference, not a functional one
        assert conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename='password_reset_tokens' AND indexdef ILIKE '%UNIQUE%(token_digest)%'")).first()
        partial = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_scoring_policies_one_active'")).scalar()
        assert "UNIQUE" in partial and "active" in partial, "the one-active-policy guarantee is a PARTIAL unique index"


# ---------------------------------------------------------------------------
# B — migration 0010 on a populated pre-0010 schema
# ---------------------------------------------------------------------------

def _read_policies_at_head(engine):
    return policy_rows(engine, LEGACY_COLUMNS + ",criteria_version,criteria,settings,origin")


def _snapshot_from_pg_row(row) -> ScoringPolicySnapshot:
    return ScoringPolicySnapshot(
        criteria=row.criteria, settings=row.settings, threshold_go=row.threshold_go, threshold_sous_reserve=row.threshold_sous_reserve,
        mastered_technologies=frozenset(DEMO["mastered_technologies"]), certifications_held=frozenset(DEMO["certifications_held"]),
        origin=row.origin, criteria_version=row.criteria_version)


def test_a_populated_pre_0010_schema_reaches_head_without_touching_history(pg_engine, pg_url):
    from tests.test_lot43_cleanup import _stable

    pg_support.upgrade(pg_url, "0009")
    assert alembic_version(pg_engine) == "0009" and not NEW_COLUMNS & column_names(pg_engine, "scoring_policies")
    ids = populate_pre_0010(pg_engine)
    other_tables_before = table_digests(pg_engine, exclude=("scoring_policies",))
    legacy_before = policy_rows(pg_engine, LEGACY_COLUMNS)
    with pg_engine.connect() as conn:
        results_before = {r.job_id: canon(r.result_data) for r in conn.execute(text("SELECT job_id, result_data FROM analyses"))}
    assert results_before == {f"pg44-job-{t}": canon(STORED[t]) for t in "abc"}, "sanity: what PostgreSQL holds is what was inserted"
    assert len(legacy_before) == 5

    pg_support.upgrade(pg_url, "0010")  # this test is about 0010 itself: later revisions are exercised elsewhere

    assert alembic_version(pg_engine) == "0010" and NEW_COLUMNS <= column_names(pg_engine, "scoring_policies")
    # nothing outside scoring_policies changed at all (users, spaces, memberships, analyses, documents, chunks...)
    assert table_digests(pg_engine, exclude=("scoring_policies",)) == other_tables_before
    with pg_engine.connect() as conn:
        assert {r.job_id: canon(r.result_data) for r in conn.execute(text("SELECT job_id, result_data FROM analyses"))} == results_before, (
            "historical results are byte-for-byte (canonically) the ones stored before 0010 — no recompute, no rewrite")
    # every historical policy column, status, owner, organization, version, timestamp preserved
    assert canon([list(r) for r in policy_rows(pg_engine, LEGACY_COLUMNS)]) == canon([list(r) for r in legacy_before])

    rows = _read_policies_at_head(pg_engine)
    assert len(rows) == 5
    assert sorted(r.status for r in rows) == ["active", "active", "active", "archived", "draft"], "nothing activated, created or archived"
    for owner, org in ((ids["a"], ids["org_a"]), (ids["b"], ids["org_b"]), (ids["a"], ids["org_c"])):
        assert sum(1 for r in rows if r.owner_user_id == owner and r.organization_id == org and r.status == "active") == 1
    for r in rows:
        assert r.origin == "legacy" and r.criteria_version == 1
        expected, expected_settings = criteria_catalogue.materialize_legacy(
            weights=r.weights, business_rules=r.business_rules, custom_criteria=r.custom_criteria)
        assert canon(r.criteria) == canon(expected) and canon(r.settings) == canon(expected_settings)
    archived = next(r for r in rows if r.status == "archived")
    assert sorted(archived.settings["legacy_unconfigured_rules"]) == [
        "budget_minimum_eur", "certification_penalty_score", "max_charge_pct", "max_unmastered_technologies"], "never-configured rules stay unconfigured"
    cleaning = next(r for r in rows if r.owner_user_id == ids["b"])
    assert [c["id"] for c in cleaning.criteria][-2:] == ["zone_couverte", "frequence_ok"]

    # historical compatibility, from the criteria READ BACK from PostgreSQL (JSONB round trip changes no note or threshold)
    it_policy = next(r for r in rows if r.status == "active" and r.owner_user_id == ids["a"] and r.organization_id == ids["org_a"])
    # PostgreSQL JSONB does NOT keep the key order of an object (shortest keys first, then alphabetical): the order of a
    # migrated policy's criteria is the order the application ALREADY read `weights` in before 0010, then frozen in the
    # `criteria` array. Same weights, notes and decisions; only the listing order differs from a SQLite-era result.
    assert [c["legacy_key"] for c in it_policy.criteria] == list(it_policy.weights)
    assert list(it_policy.weights) != list(DEMO["weights"]), "sanity: PostgreSQL really reordered the keys"
    snapshot = _snapshot_from_pg_row(it_policy)
    for name, case in GOLDEN["legacy_cases"].items():
        inp = case["inputs"]
        result = ScoringEngine().score(AOContext(**inp["ao"]), CompanyProfile(**inp["company"]), [RAGEvidence(**e) for e in inp["evidences"]],
                                       CapacityResult(**inp["capacity"]), policy=snapshot)
        expected = case["expected"]
        assert (result.decision, result.score_global) == (expected["decision"], expected["score_global"]), name
        assert {c.nom: [c.poids, c.score] for c in result.criteres} == {nom: [poids, score] for nom, poids, score in expected["criteres"]}, name
        assert _stable(list(result.criteres_bloquants)) == _stable(expected["blockers"]), name


def test_the_backfill_is_idempotent_and_the_downgrade_is_lossless_or_refused_on_postgresql(pg_engine, pg_url):
    pg_support.upgrade(pg_url, "0009")
    populate_pre_0010(pg_engine)
    legacy_before = canon([list(r) for r in policy_rows(pg_engine, LEGACY_COLUMNS)])
    tables_before = table_digests(pg_engine, exclude=("scoring_policies",))
    pg_support.upgrade(pg_url, "0010")
    first = canon([list(r) for r in _read_policies_at_head(pg_engine)])

    with pg_engine.begin() as conn:
        assert REV_0010.backfill(conn) == 0, "only rows still at criteria_version = 0 are ever processed"
    pg_support.upgrade(pg_url, "0010")  # no-op
    assert canon([list(r) for r in _read_policies_at_head(pg_engine)]) == first

    # lossless while every policy is still historical, and re-appliable
    pg_support.downgrade(pg_url, "0009")
    assert alembic_version(pg_engine) == "0009" and not NEW_COLUMNS & column_names(pg_engine, "scoring_policies")
    assert canon([list(r) for r in policy_rows(pg_engine, LEGACY_COLUMNS)]) == legacy_before
    assert table_digests(pg_engine, exclude=("scoring_policies",)) == tables_before
    pg_support.upgrade(pg_url, "0010")
    assert canon([list(r) for r in _read_policies_at_head(pg_engine)]) == first, "downgrade then upgrade is a round trip"

    # refused (nothing dropped, DDL rolled back) as soon as a policy exists in the new format
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE scoring_policies SET origin='user' WHERE status='draft'"))
    with pytest.raises(RuntimeError, match="Downgrade of 0010 refused"):
        pg_support.downgrade(pg_url, "0009")
    assert alembic_version(pg_engine) == "0010" and NEW_COLUMNS <= column_names(pg_engine, "scoring_policies")
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM scoring_policies WHERE origin='user'")).scalar() == 1


# ---------------------------------------------------------------------------
# C — constraints really enforced by PostgreSQL
# ---------------------------------------------------------------------------

UNIQUE_VIOLATION, FOREIGN_KEY_VIOLATION, CHECK_VIOLATION = "23505", "23503", "23514"


def sqlstate(exc) -> str | None:
    """SQLSTATE of a DBAPI error, whichever driver: psycopg exposes `pgcode`, pg8000 a dict in args[0].
    NOTE (measured in lot 46): SQLAlchemy/pg8000 raise `IntegrityError` ONLY for 23505 (unique); a foreign-key (23503) or
    check (23514) violation surfaces as `ProgrammingError` — code must never rely on `except IntegrityError` for those."""
    orig = getattr(exc, "orig", exc)
    if getattr(orig, "pgcode", None):
        return orig.pgcode
    return orig.args[0].get("C") if orig.args and isinstance(orig.args[0], dict) else None


def _violation(session, statement, params, constraint: str, state: str):
    with pytest.raises(DBAPIError) as caught:
        session.execute(text(statement), params)
        session.flush()
    assert constraint in str(caught.value), (constraint, str(caught.value)[:300])
    assert sqlstate(caught.value) == state, (constraint, sqlstate(caught.value))
    session.rollback()


def _add_expecting(session, obj, constraint: str, state: str):
    with pytest.raises(DBAPIError) as caught:
        session.add(obj)
        session.flush()
    assert constraint in str(caught.value), (constraint, str(caught.value)[:300])
    assert sqlstate(caught.value) == state, (constraint, sqlstate(caught.value))
    session.rollback()


def test_postgresql_enforces_the_private_scope_and_activation_constraints(pg_engine, pg_url):
    from src.web.database.models import (
        Analysis, KnowledgeChunk, KnowledgeCorpus, KnowledgeDocument, KnowledgeDocumentVersion)
    from src.web.database.repositories import analyses as analyses_repo
    from tests.conftest import default_org_id, make_active_starter_user

    pg_support.upgrade(pg_url, "head")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    a = make_active_starter_user(session, "pgc-a@example.com")   # active synthetic policy v1 (scoring=True)
    b = make_active_starter_user(session, "pgc-b@example.com")
    org_a, org_b = default_org_id(session, a), default_org_id(session, b)
    analysis = analyses_repo.create_analysis(session, user_id=a.id, organization_id=org_a, result_data={"k": 1}, job_id="pgc-job")
    session.commit()
    now = datetime.now(timezone.utc)

    def policy(owner, org, version, status):
        return {"id": uuid.uuid4(), "org": org, "owner": owner, "v": version, "st": status, "now": now}

    insert = ("INSERT INTO scoring_policies (id,organization_id,owner_user_id,version,status,weights,business_rules,custom_criteria,"
              "criteria_version,criteria,settings,origin,created_by_user_id,created_at,updated_at) VALUES "
              "(:id,:org,:owner,:v,:st,'{}','{}','[]',1,'[]','{}','user',:owner,:now,:now)")
    # activation: exactly one ACTIVE policy per (organization, owner) — a partial unique index, not application logic
    _violation(session, insert, policy(a.id, org_a, 2, "active"), "uq_scoring_policies_one_active", UNIQUE_VIOLATION)
    _violation(session, insert, policy(a.id, org_a, 1, "archived"), "uq_scoring_policies_org_owner_version", UNIQUE_VIOLATION)
    _violation(session, insert, policy(a.id, org_a, 9, "retired"), "ck_scoring_policies_status", CHECK_VIOLATION)
    session.execute(text(insert), policy(a.id, org_a, 2, "archived"))
    session.execute(text(insert), policy(a.id, org_a, 3, "draft"))
    session.execute(text(insert), policy(a.id, org_b, 1, "active"))  # same owner, ANOTHER space: its own single active policy
    session.commit()

    # organization / analysis / document coherence: the composite FK refuses a document whose organization is not its analysis's
    doc = "INSERT INTO analysis_documents (id,analysis_id,user_id,organization_id,created_at) VALUES (:id,:an,:u,:org,:now)"
    _violation(session, doc, {"id": uuid.uuid4(), "an": analysis.id, "u": a.id, "org": org_b, "now": now},
               "fk_analysis_documents_analysis_org", FOREIGN_KEY_VIOLATION)
    session.execute(text(doc), {"id": uuid.uuid4(), "an": analysis.id, "u": a.id, "org": org_a, "now": now})
    session.commit()
    # a document of an analysis that does not exist: refused too (by whichever of the analysis FKs PostgreSQL checks first)
    _violation(session, doc, {"id": uuid.uuid4(), "an": uuid.uuid4(), "u": a.id, "org": org_a, "now": now},
               "violates foreign key constraint", FOREIGN_KEY_VIOLATION)
    # an organization that has analyses cannot be deleted (RESTRICT); job ids are unique
    _violation(session, "DELETE FROM organizations WHERE id = :o", {"o": org_a}, "violates foreign key constraint", FOREIGN_KEY_VIOLATION)
    _add_expecting(session, Analysis(user_id=a.id, organization_id=org_a, job_id="pgc-job", result_data={}), "uq_analyses_job_id", UNIQUE_VIOLATION)

    # membership: one row per (user, organization), known roles only
    member = ("INSERT INTO memberships (id,user_id,organization_id,role,status,created_at,updated_at) "
              "VALUES (:id,:u,:o,:role,'active',:now,:now)")
    _violation(session, member, {"id": uuid.uuid4(), "u": a.id, "o": org_a, "role": "analyst", "now": now}, "uq_memberships_user_org", UNIQUE_VIOLATION)
    _violation(session, member, {"id": uuid.uuid4(), "u": b.id, "o": org_a, "role": "root", "now": now}, "ck_memberships_role", CHECK_VIOLATION)

    # private knowledge chain: corpus / document / version / chunk scopes are foreign keys, versions unique per document
    corpus_a = KnowledgeCorpus(organization_id=org_a, owner_user_id=a.id)
    session.add(corpus_a)
    session.commit()
    _add_expecting(session, KnowledgeDocument(corpus_id=corpus_a.id, organization_id=org_a, owner_user_id=b.id, original_filename="x.md"),
                   "fk_knowledge_documents_corpus_scope", FOREIGN_KEY_VIOLATION)  # a document claiming another owner than its corpus's
    document = KnowledgeDocument(corpus_id=corpus_a.id, organization_id=org_a, owner_user_id=a.id, original_filename="ok.md")
    session.add(document)
    session.commit()

    def version(number, status="ready"):
        return KnowledgeDocumentVersion(document_id=document.id, organization_id=org_a, owner_user_id=a.id, version_number=number, storage_key="k",
                                        file_size=1, content_hash="h" * 64, extraction_status=status, extractor_version="1")

    first_version = version(1)
    session.add(first_version)
    session.commit()
    _add_expecting(session, version(1), "uq_knowledge_document_versions_number", UNIQUE_VIOLATION)
    _add_expecting(session, version(2, "vanished"), "ck_knowledge_document_versions_status", CHECK_VIOLATION)
    _add_expecting(session, KnowledgeChunk(document_version_id=first_version.id, organization_id=org_a, owner_user_id=b.id, order_index=0, content="x"),
                   "fk_knowledge_chunks_version_scope", FOREIGN_KEY_VIOLATION)  # a chunk claiming another owner than its version's
    session.close()


def test_repository_activation_keeps_exactly_one_active_policy_per_space_on_postgresql(pg_engine, pg_url):
    from src.web.database.repositories import scoring_policy as policy_repo
    from tests.conftest import default_org_id, make_active_starter_user

    pg_support.upgrade(pg_url, "head")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    user = make_active_starter_user(session, "pgact@example.com")  # synthetic active v1
    org = default_org_id(session, user)
    scope = {"organization_id": org, "owner_user_id": user.id}
    draft = policy_repo.save_draft(session, **scope, created_by_user_id=user.id, weights={"Adequation expertise": 100.0},
                                   threshold_go=80.0, threshold_sous_reserve=50.0)
    session.commit()
    with pytest.raises(policy_repo.ActivationConflict):  # stale expectation: nothing is written
        policy_repo.activate_draft(session, **scope, expected_active_version=None)
    session.rollback()
    assert policy_repo.get_active(session, **scope).version == 1 and policy_repo.get_draft(session, **scope).id == draft.id
    activated = policy_repo.activate_draft(session, **scope, expected_active_version=1)
    session.commit()
    with pg_engine.connect() as conn:
        rows = conn.execute(text("SELECT version, status FROM scoring_policies WHERE organization_id=:o AND owner_user_id=:u ORDER BY version"),
                            {"o": org, "u": user.id}).all()
    assert [(r.version, r.status) for r in rows] == [(1, "archived"), (2, "active")] and activated.version == 2
    session.close()


# ---------------------------------------------------------------------------
# D — short server journey on PostgreSQL
# ---------------------------------------------------------------------------

def test_a_short_server_journey_runs_on_postgresql_and_stays_in_its_space(pg_engine, pg_url, monkeypatch):
    """Configuration -> private document/search -> analysis (LLM simulated at the adapter, zero network) ->
    result / history / deliverables -> isolation between organizations and colleagues. The schema is built by
    the real Alembic chain (not create_all)."""
    from fastapi.testclient import TestClient

    import main
    from src.core import config
    from src.web import jobs
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    from tests.conftest import default_org_id, make_active_starter_user
    from tests.test_b02_organizations import _add_member
    from tests.test_context_budget_job_path import _install_fake_llm
    from tests.test_lot44_criteria_contract import NEW_CRITERIA, NEW_PROFILE, _login

    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-not-for-production")
    pg_support.upgrade(pg_url, "head")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    owner = make_active_starter_user(session, "pgj-owner@example.com", scoring=False, capacity=False)
    org_x = default_org_id(session, owner)
    org_y = organizations_repo.create_organization(session, name="Espace Y (lot 46)").id
    memberships_repo.create_membership(session, user_id=owner.id, organization_id=org_y, role="organization_admin", status="active")
    _add_member(session, organization_id=org_x, email="pgj-colleague@example.com", role="analyst")
    session.commit()
    X, Y = str(org_x), str(org_y)
    ao = ("Appel d'offres - Prestations de nettoyage. Acheteur : Collectivité Exemple. Site de Lyon, 4 fois par semaine. "
          "Budget : 120 000 €. Durée 24 mois.")
    provider = _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Référence retenue."})

    with TestClient(main.app) as client:
        csrf = _login(client, "pgj-owner@example.com")
        h = {"X-CSRF-Token": csrf}

        def call(method, path, org=X, **kw):
            return client.request(method, f"{path}{'&' if '?' in path else '?'}organization_id={org}", headers=h, **kw)

        # private configuration, through the API, on PostgreSQL
        assert call("POST", "/api/capacity", json={"charge_globale_pct": 40, "nombre_projets_en_cours": 1, "projets_en_cours": ["P"],
                                                   "capacites_par_pole": {"Pôle": 60}}).status_code == 200
        assert call("PUT", "/api/scoring-config/profile", json=NEW_PROFILE).status_code == 200
        assert call("PUT", "/api/scoring-config/policy", json={"criteria": NEW_CRITERIA, "threshold_go": 80, "threshold_sous_reserve": 55}).status_code == 200
        assert call("POST", "/api/scoring-config/policy/validate").json() == {"valid": True, "errors": {}}
        simulation = call("POST", "/api/scoring-config/simulate", data={"mode": "paste", "text": ao})
        assert simulation.status_code == 200 and simulation.json()["simulation"] is True
        assert call("POST", "/api/scoring-config/policy/activate", json={"expected_active_version": None}).status_code == 200

        # private document + search
        up = call("POST", "/api/knowledge/documents", files={"file": ("ref_lyon.md", b"# Reference\n\nMARQUEUR_PGJ nettoyage de sites a Lyon, collectivite.", "text/plain")})
        assert up.status_code == 201, up.text
        assert [r["source"] for r in call("GET", "/api/knowledge/search?q=MARQUEUR_PGJ").json()["results"]] == ["ref_lyon.md"]

        # analysis with the AI simulated at the adapter
        started = call("POST", "/api/analyze", data={"mode": "paste", "text": ao})
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        for _ in range(300):
            job = jobs.get_job(job_id)
            if job.status != "running":
                break
            time.sleep(0.1)
        assert job.status == "done", (job.error, job.error_code)
        assert provider.total_calls >= 1, "the simulated AI adapter was really exercised"
        assert "ref_lyon.md" in [e.source for e in job.result.evidence_pack], "the account's own reference fed the analysis"

        # result page, history and deliverables, read back through the server
        assert call("GET", f"/app/resultats/{job_id}").status_code == 200
        history = call("GET", "/api/history").json()
        assert history["total"] == 1 and history["items"][0]["job_id"] == job_id
        assert call("GET", f"/api/download/{job_id}/pdf").content[:4] == b"%PDF"
        assert call("GET", f"/api/download/{job_id}/docx").content[:2] == b"PK"

        # what PostgreSQL itself holds
        with pg_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT user_id, organization_id, result_data->'result'->>'decision' AS decision, "
                "jsonb_typeof(result_data->'result'->'criteres') AS kind FROM analyses WHERE job_id = :j"), {"j": job_id}).one()
            assert (row.user_id, row.organization_id, row.kind) == (owner.id, org_x, "array") and row.decision == job.result.decision
            assert conn.execute(text("SELECT count(*) FROM scoring_policies WHERE organization_id=:o AND owner_user_id=:u AND status='active' AND origin='user'"),
                                {"o": org_x, "u": owner.id}).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM knowledge_document_versions WHERE organization_id=:o AND owner_user_id=:u AND extraction_status='ready'"),
                                {"o": org_x, "u": owner.id}).scalar() == 1
            counts_before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("analyses", "knowledge_documents", "scoring_policies")}

        # the OTHER selected organization of the same person: nothing leaks
        assert call("GET", "/api/history", org=Y).json()["total"] == 0
        assert call("GET", "/api/knowledge/documents", org=Y).json()["documents"] == []
        assert call("GET", "/api/knowledge/search?q=MARQUEUR_PGJ", org=Y).json()["results"] == []

        # a colleague of the same organization sees none of it
        csrf_c = _login(client, "pgj-colleague@example.com")
        hc = {"X-CSRF-Token": csrf_c}
        assert client.get(f"/api/history?organization_id={X}").json()["total"] == 0
        assert client.get(f"/api/knowledge/documents?organization_id={X}").json()["documents"] == []
        assert client.get(f"/api/download/{job_id}/pdf?organization_id={X}").status_code in (403, 404)
        page = client.get(f"/app/resultats/{job_id}?organization_id={X}")
        assert page.status_code != 200 or "Prestations de nettoyage" not in page.text, "a colleague gets no result content"

        # readiness with a real PostgreSQL behind it
        ready = client.get("/readyz")
        assert ready.status_code == 200 and ready.json()["checks"] == {"database": "ok"}

    with pg_engine.connect() as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in counts_before} == counts_before, "reads by others wrote nothing"
    del jobs._JOBS[job_id]
