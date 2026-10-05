"""B02 data migration — 0001 -> 0002 (additive) -> backfill script -> 0003
(finalize), exercised against synthetic "pre-B02" legacy data rather than a
freshly created (already-final-shape) test database.

Base.metadata.create_all() (used by tests/conftest.py's `test_db` fixture)
always builds the *current*, final ORM shape — it can never reproduce a
database that predates organization_id existing at all. This file instead
runs the real Alembic revision modules programmatically (via
alembic.operations.Operations bound to a raw connection — the standard way
to invoke migrations without a full alembic.ini/env.py context) against a
throwaway SQLite file, so scripts/backfill_b02_organizations.py and revision
0003 are exercised against genuinely pre-organization_id rows.

Limitation: this validates migration *logic* and the constraints' shape.
SQLite (via batch_alter_table) stands in for Postgres, which is what
production actually runs — dialect-specific behavior (real concurrent DDL,
lock semantics, timing) is not covered here. See the B02 delivery note.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load_revision(filename: str):
    spec = importlib.util.spec_from_file_location(filename, MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REV_0001 = _load_revision("0001_initial_schema.py")
REV_0002 = _load_revision("0002_b02_organizations.py")
REV_0003 = _load_revision("0003_b02_finalize_constraints.py")
REV_0004 = _load_revision("0004_b03_private_knowledge.py")
REV_0005 = _load_revision("0005_b06_scoring_policy.py")
REV_0006 = _load_revision("0006_b06t4_b07t1_b08t1_business_rules.py")
REV_0007 = _load_revision("0007_b12t1_analysis_jobs.py")
REV_0008 = _load_revision("0008_b21t1_password_reset_hardening.py")
REV_0009 = _load_revision("0009_b06t5_business_facts.py")


def _run_migration(db_path, *revisions) -> None:
    """Run Alembic revision upgrade() functions against a *plain* engine —
    deliberately not src.web.database.session.get_engine(), which attaches
    a PRAGMA foreign_keys=ON connect-listener for normal app/ORM use. Real
    `alembic upgrade` (migrations/env.py) also uses its own plain engine,
    entirely separate from the app's — mirroring that here matters because
    SQLite's batch-mode table recreation (used by revisions 0002/0003 to
    add columns/constraints) toggles that same pragma internally, and having
    two independent listeners fight over it silently corrupts the copy step
    (rows go missing with no error) rather than raising anything — a
    Alembic/SQLite interaction, not an application bug. The ORM-driven
    backfill script below still runs through the app's real engine, so FK
    enforcement is genuinely exercised where it matters (see
    test_legacy_analysis_and_document_keep_owner_and_become_accessible's
    composite-FK check)."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                for revision in revisions:
                    revision.upgrade()
            conn.commit()
    finally:
        engine.dispose()


@pytest.fixture()
def pre_b02_db(tmp_path, monkeypatch):
    """A database at the 0001+0002 schema (organizations/memberships exist,
    organization_id is nullable, no legacy row has it set) — i.e. exactly
    the shape a real B01 production database would be in right after
    deploying B02's additive migration, before the backfill script runs."""
    from src.core import config
    from src.web.database import session as db_session_module

    db_path = tmp_path / f"pre_b02_{uuid.uuid4().hex}.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    # 0007/0008 applied here too (purely additive, unrelated to organizations/
    # memberships/analyses — analysis_jobs table, users.session_version,
    # password_reset_tokens.token_digest): several tests using this fixture
    # go through the real ORM (scripts/backfill_b02_organizations.py,
    # memberships_repo) rather than raw SQL, and the ORM's current User/
    # PasswordResetToken models unconditionally include the columns 0008
    # added. Without applying 0008 here too, any ORM INSERT/SELECT against
    # `users` fails with "no such column: users.session_version" — a schema
    # mismatch between this fixture's deliberately-old raw schema and the
    # live ORM model, not a real migration-logic defect. 0003-0006 are
    # skipped here on purpose (this fixture's whole point is a database that
    # predates them); 0007/0008 don't have that constraint since nothing in
    # this file exercises them directly.
    _run_migration(db_path, REV_0001, REV_0002, REV_0007, REV_0008)
    # Lot 49: same reasoning as 0007/0008 above — the ORM `Analysis` model now unconditionally includes
    # `parent_job_id` (added by migration 0012). The FULL 0012 migration also creates `ao_dossier_job_links`
    # (which depends on 0011's `ao_dossiers` table, itself depending on 0010 — all out of scope for a
    # database that deliberately predates B03/B06), so only the one column the ORM needs is patched in
    # directly here, exactly the same schema-compatibility patch, not a re-run of unrelated migrations.
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE analyses ADD COLUMN parent_job_id VARCHAR(50)"))
        conn.execute(text("ALTER TABLE analyses ADD COLUMN origin_job_id VARCHAR(50)"))
    engine.dispose()

    yield db_path

    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)


def _new_id() -> str:
    """SQLAlchemy's Uuid type stores values on SQLite as 32-char hex without
    dashes (its .hex form) — a raw SQL insert using str(uuid.uuid4())'s
    dashed form instead would store a *different* string, which then fails
    FK lookups against rows the ORM itself wrote (it compares stored text,
    not parsed UUID values). Every id/foreign-key value in this file's raw
    SQL must use this same form so they compare equal to what the ORM
    reads and writes later."""
    return uuid.uuid4().hex


def _insert_legacy_user(engine, *, email: str, company: str | None) -> str:
    user_id = _new_id()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id,email,password_hash,first_name,last_name,company,status,created_at,updated_at) "
            "VALUES (:id,:email,'x','Leg','Acy',:company,'active',:now,:now)"
        ), {"id": user_id, "email": email, "company": company, "now": now})
    return user_id


def _insert_legacy_analysis_with_document(engine, *, user_id: str, title: str) -> tuple[str, str]:
    analysis_id = _new_id()
    doc_id = _new_id()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO analyses (id,user_id,job_id,title,result_data,created_at,updated_at) "
            "VALUES (:id,:uid,:job_id,:title,'{}',:now,:now)"
        ), {"id": analysis_id, "uid": user_id, "job_id": f"legacy-{analysis_id[:8]}", "title": title, "now": now})
        conn.execute(text(
            "INSERT INTO analysis_documents (id,analysis_id,user_id,filename,storage_path,created_at) "
            "VALUES (:id,:aid,:uid,'f.pdf','local://f.pdf',:now)"
        ), {"id": doc_id, "aid": analysis_id, "uid": user_id, "now": now})
    return analysis_id, doc_id


def _run_backfill(dry_run: bool) -> tuple[int, str]:
    import scripts.backfill_b02_organizations as backfill
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = backfill.run(dry_run=dry_run)
    return code, buffer.getvalue()


def test_dry_run_reports_counts_and_writes_nothing(pre_b02_db):
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_a = _insert_legacy_user(engine, email="a@acme.example", company="Acme Corp")
    user_b = _insert_legacy_user(engine, email="b@acme.example", company="Acme Corp")
    _insert_legacy_analysis_with_document(engine, user_id=user_a, title="AO A")
    _insert_legacy_analysis_with_document(engine, user_id=user_b, title="AO B")

    code, output = _run_backfill(dry_run=True)
    assert code == 0
    assert "2 utilisateur(s) sans organisation active" in output
    assert "2 analyse(s) sans organization_id" in output

    with engine.connect() as conn:
        remaining = conn.execute(text("SELECT COUNT(*) FROM analyses WHERE organization_id IS NULL")).scalar()
        orgs = conn.execute(text("SELECT COUNT(*) FROM organizations")).scalar()
    assert remaining == 2, "dry-run must not write anything"
    assert orgs == 0


def test_same_company_name_gets_two_distinct_organizations(pre_b02_db):
    """T02: two legacy accounts sharing the same `company` string must never
    be merged into one organization by the migration."""
    from src.web.database import session as db_session_module
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.session import session_scope

    engine = db_session_module.get_engine()
    user_a = _insert_legacy_user(engine, email="a@acme.example", company="Acme Corp")
    user_b = _insert_legacy_user(engine, email="b@acme.example", company="Acme Corp")
    analysis_a, doc_a = _insert_legacy_analysis_with_document(engine, user_id=user_a, title="AO A")
    analysis_b, doc_b = _insert_legacy_analysis_with_document(engine, user_id=user_b, title="AO B")

    code, output = _run_backfill(dry_run=False)
    assert code == 0, output

    with session_scope() as db:
        memberships_a = memberships_repo.list_active_for_user(db, uuid.UUID(hex=user_a))
        memberships_b = memberships_repo.list_active_for_user(db, uuid.UUID(hex=user_b))
        assert len(memberships_a) == 1 and len(memberships_b) == 1
        assert memberships_a[0].organization_id != memberships_b[0].organization_id
        assert memberships_a[0].role == "organization_admin"

    with engine.connect() as conn:
        org_a = conn.execute(text("SELECT organization_id FROM analyses WHERE id=:id"), {"id": analysis_a}).scalar()
        org_b = conn.execute(text("SELECT organization_id FROM analyses WHERE id=:id"), {"id": analysis_b}).scalar()
        doc_org_a = conn.execute(text("SELECT organization_id FROM analysis_documents WHERE id=:id"), {"id": doc_a}).scalar()
        doc_org_b = conn.execute(text("SELECT organization_id FROM analysis_documents WHERE id=:id"), {"id": doc_b}).scalar()
    assert org_a != org_b
    assert doc_org_a == org_a
    assert doc_org_b == org_b


def test_legacy_analysis_and_document_keep_owner_and_become_accessible(pre_b02_db):
    """T03: a B01-era analysis/document keeps its user_id, and the finalize
    migration (0003) succeeds and enforces the new constraints afterward."""
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="owner@example.com", company=None)
    analysis_id, doc_id = _insert_legacy_analysis_with_document(engine, user_id=user_id, title="Legacy AO")

    code, _ = _run_backfill(dry_run=False)
    assert code == 0

    _run_migration(pre_b02_db, REV_0003)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id, organization_id FROM analyses WHERE id=:id"), {"id": analysis_id}
        ).fetchone()
        doc_row = conn.execute(
            text("SELECT user_id, organization_id FROM analysis_documents WHERE id=:id"), {"id": doc_id}
        ).fetchone()
    assert row.user_id == user_id
    assert row.organization_id is not None
    assert doc_row.organization_id == row.organization_id

    # T09 (also exercised here): the composite FK now rejects a document
    # whose organization_id disagrees with its own analysis's, even when
    # inserted directly with raw SQL — bypassing the repository entirely.
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO analysis_documents (id,analysis_id,user_id,organization_id,filename,storage_path,created_at) "
                "VALUES (:id,:aid,:uid,:badorg,'bad.pdf','local://bad.pdf',:now)"
            ), {
                "id": _new_id(), "aid": analysis_id, "uid": user_id,
                "badorg": _new_id(), "now": datetime.now(timezone.utc).isoformat(),
            })


def test_backfill_is_idempotent_on_rerun(pre_b02_db):
    """T10: re-running the backfill after it already succeeded must create
    no duplicate organizations or memberships, and must not error."""
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="rerun@example.com", company=None)
    _insert_legacy_analysis_with_document(engine, user_id=user_id, title="AO")

    code1, _ = _run_backfill(dry_run=False)
    assert code1 == 0
    code2, output2 = _run_backfill(dry_run=False)
    assert code2 == 0
    assert "0 organisation(s) créée(s)" in output2
    assert "0 analyse(s) rattachée(s)" in output2

    with engine.connect() as conn:
        org_count = conn.execute(text("SELECT COUNT(*) FROM organizations")).scalar()
        membership_count = conn.execute(text("SELECT COUNT(*) FROM memberships")).scalar()
    assert org_count == 1
    assert membership_count == 1


def test_backfill_aborts_atomically_on_unattributable_analysis(pre_b02_db):
    """T10/section 7: an analysis whose user_id can't be resolved to any
    user must abort the *entire* run — no partial writes for the other,
    perfectly attributable users in the same batch."""
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    good_user = _insert_legacy_user(engine, email="good@example.com", company=None)
    _insert_legacy_analysis_with_document(engine, user_id=good_user, title="Good AO")

    # An analysis row with no matching user — simulates data corruption or a
    # foreign key that was somehow bypassed. Must not be silently attributed
    # to anyone. analyses.user_id already carries a NOT NULL FK to users.id
    # from revision 0001, so reaching this state at all requires disabling
    # FK enforcement for this one connection — exactly why the backfill
    # script calls this branch a "hard safety net" for something that
    # should be structurally impossible in practice.
    now = datetime.now(timezone.utc).isoformat()
    orphan_id = _new_id()
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text(
            "INSERT INTO analyses (id,user_id,job_id,title,result_data,created_at,updated_at) "
            "VALUES (:id,:uid,'orphan-job','Orphan AO','{}',:now,:now)"
        ), {"id": orphan_id, "uid": _new_id(), "now": now})
        conn.commit()

    code, output = _run_backfill(dry_run=False)
    assert code == 1
    assert "ABORT" in output

    with engine.connect() as conn:
        org_count = conn.execute(text("SELECT COUNT(*) FROM organizations")).scalar()
        good_org = conn.execute(
            text("SELECT organization_id FROM analyses WHERE user_id=:uid"), {"uid": good_user}
        ).scalar()
    assert org_count == 0, "abort must roll back everything, including the good user's new organization"
    assert good_org is None


# ---------------------------------------------------------------------------
# T19 (B03) — the additive 0004 migration on a fully-populated B02 database
# changes nothing that already existed
# ---------------------------------------------------------------------------

def test_b03_migration_on_populated_b02_db_preserves_all_existing_rows(pre_b02_db):
    """Builds a B02-complete database (0001->0002->backfill->0003) with
    real users/organizations/memberships/analyses/documents, records exact
    row counts and a content checksum, runs 0004, and confirms every
    existing table's row count and content are byte-for-byte identical —
    0004 only ever adds new, empty tables."""
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="populated@example.com", company="Populated Co")
    analysis_id, doc_id = _insert_legacy_analysis_with_document(engine, user_id=user_id, title="Populated AO")

    code, _ = _run_backfill(dry_run=False)
    assert code == 0
    _run_migration(pre_b02_db, REV_0003)

    def _snapshot():
        with engine.connect() as conn:
            return {
                table: conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).fetchall()
                for table in ("users", "organizations", "memberships", "analyses", "analysis_documents")
            }

    before = _snapshot()

    _run_migration(pre_b02_db, REV_0004)

    after = _snapshot()
    for table in before:
        assert before[table] == after[table], f"table {table} changed during the additive 0004 migration"

    with engine.connect() as conn:
        for new_table in ("knowledge_corpora", "knowledge_documents", "knowledge_document_versions",
                           "knowledge_chunks", "private_capacity_plans"):
            count = conn.execute(text(f"SELECT COUNT(*) FROM {new_table}")).scalar()
            assert count == 0, f"{new_table} must start empty — B03 does not migrate the global demo corpus/capacity"


# ---------------------------------------------------------------------------
# B06-T2 §5/T4 — the additive 0005 migration on a fully-populated B02+B03
# database (real analysis AND a real private capacity plan, not just an
# empty schema) changes nothing that already existed, and never activates
# an old global registry as a policy for anyone.
# ---------------------------------------------------------------------------

def test_b06_migration_on_populated_b03_db_preserves_all_existing_rows(pre_b02_db):
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="populatedb06@example.com", company="Populated B06 Co")
    analysis_id, doc_id = _insert_legacy_analysis_with_document(engine, user_id=user_id, title="Populated AO B06")

    code, _ = _run_backfill(dry_run=False)
    assert code == 0
    _run_migration(pre_b02_db, REV_0003, REV_0004)

    # Populate real B03 data (a private capacity plan) so this migration is
    # exercised against genuinely non-empty B03 tables, not just B02's.
    org_id = None
    with engine.connect() as conn:
        org_id = conn.execute(text("SELECT organization_id FROM analyses WHERE id=:aid"), {"aid": analysis_id}).scalar()
    capacity_id = _new_id()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO private_capacity_plans "
            "(id,organization_id,owner_user_id,status,charge_globale_pct,nombre_projets_en_cours,"
            "projets_en_cours,capacites_par_pole,version,created_at,updated_at) "
            "VALUES (:id,:org,:user,'configured',42,1,'[]','{}',1,:now,:now)"
        ), {"id": capacity_id, "org": org_id, "user": user_id, "now": now})

    def _snapshot():
        with engine.connect() as conn:
            return {
                table: conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).fetchall()
                for table in ("users", "organizations", "memberships", "analyses", "analysis_documents", "private_capacity_plans")
            }

    before = _snapshot()

    _run_migration(pre_b02_db, REV_0005)

    after = _snapshot()
    for table in before:
        assert before[table] == after[table], f"table {table} changed during the additive 0005 migration"

    with engine.connect() as conn:
        for new_table in ("provider_profiles", "scoring_policies"):
            count = conn.execute(text(f"SELECT COUNT(*) FROM {new_table}")).scalar()
            assert count == 0, (
                f"{new_table} must start empty — B06-T2 never auto-activates an old global "
                f"registry (ScoringEngine.mastered/certs_ok/weights) as anyone's private policy"
            )
        # The pre-existing analysis is untouched — no retroactive scoring_policy_version
        # is fabricated for a historical analysis that predates this ticket.
        result_data = conn.execute(text("SELECT result_data FROM analyses WHERE id=:aid"), {"aid": analysis_id}).scalar()
        assert result_data == "{}", "a pre-existing analysis's result_data must never be rewritten by an additive migration"


# ---------------------------------------------------------------------------
# B06-T4/B07-T1/B08-T1 — the additive 0006 migration on a POPULATED
# scoring_policies/provider_profiles/private_capacity_plans database only
# adds columns with safe, inert defaults; it never back-fills an existing
# row with a "configured" business rule, an enabled external lookup, or any
# value other than the documented default.
# ---------------------------------------------------------------------------

def test_0006_migration_on_populated_b06_db_adds_only_safe_defaults(pre_b02_db):
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="populatedb06t4@example.com", company="Populated B06T4 Co")
    analysis_id, doc_id = _insert_legacy_analysis_with_document(engine, user_id=user_id, title="Populated AO B06T4")

    code, _ = _run_backfill(dry_run=False)
    assert code == 0
    _run_migration(pre_b02_db, REV_0003, REV_0004, REV_0005)

    org_id = None
    with engine.connect() as conn:
        org_id = conn.execute(text("SELECT organization_id FROM analyses WHERE id=:aid"), {"aid": analysis_id}).scalar()

    # Real, pre-0006 rows in all three tables this migration touches —
    # exactly the shape an already-live account would have.
    policy_id = _new_id()
    profile_id = _new_id()
    capacity_id = _new_id()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO scoring_policies "
            "(id,organization_id,owner_user_id,version,status,weights,threshold_go,threshold_sous_reserve,"
            "created_by_user_id,created_at,updated_at) "
            "VALUES (:id,:org,:user,1,'active','{\"Adequation expertise\": 100}',88,60,:user,:now,:now)"
        ), {"id": policy_id, "org": org_id, "user": user_id, "now": now})
        conn.execute(text(
            "INSERT INTO provider_profiles "
            "(id,organization_id,owner_user_id,status,raison_sociale,effectif,competences,certifications,"
            "version,created_at,updated_at) "
            "VALUES (:id,:org,:user,'complete','Nova Digital','10-50','[\"python\"]','[]',1,:now,:now)"
        ), {"id": profile_id, "org": org_id, "user": user_id, "now": now})
        conn.execute(text(
            "INSERT INTO private_capacity_plans "
            "(id,organization_id,owner_user_id,status,charge_globale_pct,nombre_projets_en_cours,"
            "projets_en_cours,capacites_par_pole,version,created_at,updated_at) "
            "VALUES (:id,:org,:user,'configured',42,1,'[]','{}',1,:now,:now)"
        ), {"id": capacity_id, "org": org_id, "user": user_id, "now": now})

    def _snapshot():
        with engine.connect() as conn:
            return {
                table: conn.execute(text(
                    f"SELECT id,organization_id,owner_user_id,status,version FROM {table} ORDER BY id"
                )).fetchall()
                for table in ("scoring_policies", "provider_profiles", "private_capacity_plans")
            }

    before = _snapshot()

    _run_migration(pre_b02_db, REV_0006)

    after = _snapshot()
    for table in before:
        assert before[table] == after[table], f"table {table}'s existing columns changed during the additive 0006 migration"

    with engine.connect() as conn:
        policy_row = conn.execute(
            text("SELECT business_rules, weights, threshold_go FROM scoring_policies WHERE id=:id"), {"id": policy_id}
        ).fetchone()
        profile_row = conn.execute(
            text("SELECT external_enrichment_enabled, raison_sociale FROM provider_profiles WHERE id=:id"), {"id": profile_id}
        ).fetchone()
        capacity_row = conn.execute(
            text("SELECT disponibilite_minimum_pct, charge_globale_pct FROM private_capacity_plans WHERE id=:id"), {"id": capacity_id}
        ).fetchone()

    import json
    assert json.loads(policy_row.business_rules) == {}, (
        "an existing ScoringPolicy must never be back-filled with the old hardcoded "
        "engine constants (50000/95/4/20) as if the account had configured them"
    )
    assert json.loads(policy_row.weights) == {"Adequation expertise": 100}, "existing weights must survive untouched"
    assert policy_row.threshold_go == 88

    assert profile_row.external_enrichment_enabled in (0, False), (
        "an existing account must never be silently opted into external company lookups by this migration"
    )
    assert profile_row.raison_sociale == "Nova Digital"

    assert capacity_row.disponibilite_minimum_pct == 10, "matches the pre-existing hardcoded threshold exactly"
    assert capacity_row.charge_globale_pct == 42


# ---------------------------------------------------------------------------
# B06-T5 — the additive 0009 migration on a POPULATED scoring_policies/
# provider_profiles database (real, already-active policy/profile rows, not
# just an empty schema) only adds columns with safe, inert defaults; it
# never back-fills an existing account with a business fact or custom
# criterion it never configured, and the pre-existing 12-criteria formula
# stays exactly what it was for every account that migrates through this.
# ---------------------------------------------------------------------------

def test_0009_migration_on_populated_b06_db_adds_only_safe_defaults(pre_b02_db):
    from src.web.database import session as db_session_module

    engine = db_session_module.get_engine()
    user_id = _insert_legacy_user(engine, email="populatedb06t5@example.com", company="Populated B06T5 Co")
    analysis_id, doc_id = _insert_legacy_analysis_with_document(engine, user_id=user_id, title="Populated AO B06T5")

    code, _ = _run_backfill(dry_run=False)
    assert code == 0
    _run_migration(pre_b02_db, REV_0003, REV_0004, REV_0005, REV_0006)

    org_id = None
    with engine.connect() as conn:
        org_id = conn.execute(text("SELECT organization_id FROM analyses WHERE id=:aid"), {"aid": analysis_id}).scalar()

    # Real, pre-0009 rows — exactly the shape an already-live, already-
    # configured-and-activated account would have.
    policy_id = _new_id()
    profile_id = _new_id()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO scoring_policies "
            "(id,organization_id,owner_user_id,version,status,weights,threshold_go,threshold_sous_reserve,"
            "business_rules,created_by_user_id,created_at,updated_at) "
            "VALUES (:id,:org,:user,1,'active','{\"Adequation expertise\": 100}',88,60,'{}',:user,:now,:now)"
        ), {"id": policy_id, "org": org_id, "user": user_id, "now": now})
        conn.execute(text(
            "INSERT INTO provider_profiles "
            "(id,organization_id,owner_user_id,status,raison_sociale,effectif,competences,certifications,"
            "external_enrichment_enabled,version,created_at,updated_at) "
            "VALUES (:id,:org,:user,'complete','Nova Digital','10-50','[\"python\"]','[]',0,1,:now,:now)"
        ), {"id": profile_id, "org": org_id, "user": user_id, "now": now})

    def _snapshot():
        with engine.connect() as conn:
            return {
                table: conn.execute(text(
                    f"SELECT id,organization_id,owner_user_id,status,version FROM {table} ORDER BY id"
                )).fetchall()
                for table in ("scoring_policies", "provider_profiles")
            }

    before = _snapshot()

    _run_migration(pre_b02_db, REV_0009)

    after = _snapshot()
    for table in before:
        assert before[table] == after[table], f"table {table}'s existing columns changed during the additive 0009 migration"

    with engine.connect() as conn:
        policy_row = conn.execute(
            text("SELECT custom_criteria, weights, threshold_go FROM scoring_policies WHERE id=:id"), {"id": policy_id}
        ).fetchone()
        profile_row = conn.execute(
            text("SELECT business_facts, raison_sociale FROM provider_profiles WHERE id=:id"), {"id": profile_id}
        ).fetchone()

    import json
    assert json.loads(policy_row.custom_criteria) == [], (
        "an existing, already-active ScoringPolicy must never be back-filled with a custom "
        "criterion it never configured — it keeps scoring with exactly the same fixed 12 "
        "IT-specific criteria it always used"
    )
    assert json.loads(policy_row.weights) == {"Adequation expertise": 100}, "existing weights must survive untouched"
    assert policy_row.threshold_go == 88

    assert json.loads(profile_row.business_facts) == {}, (
        "an existing account must never be silently given a declared business fact it never entered"
    )
    assert profile_row.raison_sociale == "Nova Digital"
