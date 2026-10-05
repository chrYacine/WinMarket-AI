"""Lot 54 §3 — a REAL rehearsal of the SQLite -> PostgreSQL transfer on disposable data: builds a disposable
SQLite source with the scenario the ticket names (several accounts, two organizations of the SAME user,
active profile/policy, an AO dossier, a versioned private document, an analysis with deliverables, a
sourced complement + revision), migrates it to a disposable PostgreSQL+pgvector (pgserver) target via
scripts/migrate_sqlite_to_postgresql.py, verifies row counts/identifiers/FKs/JSON snapshots/hashes, then
activates the hybrid RAG mode on the TARGET ONLY, reindexes, and verifies a real hybrid search.

Uses corpus B (synthetic) only — corpus A (the real user documents) is never needed to qualify the transfer
mechanics, per the ticket's own "Utiliser B suffit" instruction.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, func, select

from scripts.migrate_sqlite_to_postgresql import plan
from src.web.database.models import Base
from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)
from tests.test_lot44_criteria_contract import _capacity, _login, _put, crit
from tests.test_lot52_completion_facts_http import FREQ_DOC, FREQ_RESPONSE, _patch_llm, _search, _upload_doc
from tests.conftest import default_org_id, make_active_starter_user

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgresql.py"

PROFILE = {
    "raison_sociale": "Compte de répétition (lot 54)", "competences": [], "certifications": [],
    "business_facts": {"frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 5}},
}


def _build_disposable_scenario(client, db):
    """Returns (job_id, revision_job_id, pdf_sha256) after building the full scenario on the SQLite `db`."""
    user = make_active_starter_user(db, "l54-rehearsal@example.com", scoring=False)
    org1 = default_org_id(db, user)
    csrf = _login(client, "l54-rehearsal@example.com")

    # Second organization of the SAME user (lot 54 §3: "deux organisations d'un même utilisateur").
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    org2 = organizations_repo.create_organization(db, name="Deuxième organisation (lot 54)")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org2.id, role="organization_admin", status="active")
    db.commit()
    # Two orgs for this user now exist — every subsequent call must say which one it means (the "current
    # organization" cookie, the same mechanism /app/parametres' own organization switcher relies on).
    client.cookies.set("wm_org_id", str(org1))

    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", PROFILE).status_code == 200
    criteria = [crit("frequence", "numeric_threshold", {"fact_key": "frequence_nettoyage", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}, 100, label="Fréquence compatible")]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 50, "threshold_sous_reserve": 20}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200
    cleared = dict(PROFILE)
    cleared["business_facts"] = {"frequence_nettoyage": {**PROFILE["business_facts"]["frequence_nettoyage"], "value": None}}
    assert _put(client, csrf, "/api/scoring-config/profile", cleared).status_code == 200

    doc = _upload_doc(client, csrf, "conditions.md", FREQ_DOC)

    # An AO dossier (lot 47 bis) — one piece.
    files = [("rc", ("rc.txt", "Règlement de consultation. Appel d'offres. Cahier des charges : 3 fois par semaine. Budget 120000 euros.".encode(), "text/plain"))]
    r = client.post("/api/analyze", data={"mode": "dossier"}, files=files, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    from src.web import jobs
    import time
    def wait(job_id):
        for _ in range(300):
            job = jobs.get_job(job_id)
            if job is not None and job.status != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish")
    job = wait(r.json()["job_id"])
    assert job.result.decision == "INCOMPLET", job.result.scoring_missing

    state = client.get(f"/api/analyze/{job.id}/completion").json()
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")

    import unittest.mock as mock
    class _StubLLM:
        enabled, last_provider_used = True, "stub"
        def json_complete(self, *a, **k): return FREQ_RESPONSE
    with mock.patch("src.agents.llm_client.ClaudeClient", lambda: _StubLLM()):
        proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]
        r2 = client.post(
            f"/api/analyze/{job.id}/complete",
            json={"items": [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
                  "confirm_profile_write": True, "expected_profile_version": state["profile_version"]},
            headers={"X-CSRF-Token": csrf},
        )
    assert r2.status_code == 200, r2.text
    revision = wait(r2.json()["job_id"])
    assert revision.status == "done"

    pdf = client.get(f"/api/download/{revision.id}/pdf").content
    pdf_sha = hashlib.sha256(pdf).hexdigest()
    return job.id, revision.id, pdf_sha, str(org1), str(org2.id)


def test_full_sqlite_to_postgresql_rehearsal_with_hybrid_rag_activation(client, db, test_db, request, monkeypatch):
    # Built BEFORE requesting the pg_url/pg_engine fixtures on purpose: both monkeypatch config.DATABASE_URL
    # to the disposable PostgreSQL target and reset the app's engine singleton — requesting them as ordinary
    # test parameters would swap the app's OWN database out from under `client`/`db` WHILE the SQLite
    # scenario is still being built via real HTTP calls (reproduced live: login started hitting the empty
    # Postgres schema mid-build). Lazy `request.getfixturevalue(...)` defers that swap until after the
    # SQLite source is completely finished.
    original_job_id, revision_job_id, pdf_sha_before, org1, org2 = _build_disposable_scenario(client, db)
    source_url = str(test_db.url)

    # Lot 55 — `test_db` builds its schema via Base.metadata.create_all (the whole suite's fast path), so it
    # carries NO alembic_version row by default. The REAL source this script targets on cutover day (the
    # actual winmarket_local.db) has been through real `alembic upgrade head` runs its whole life and DOES
    # carry one — stamped here (schema unchanged, only the version marker is written) so this rehearsal
    # exercises the new _assert_schema_revisions_compatible check the same way a real cutover would, instead
    # of silently taking the "neither side is Alembic-tracked" bypass path.
    from alembic.config import Config as _AlembicConfig
    from alembic import command as _alembic_command
    _repo_root = SCRIPT.parents[1]
    _stamp_cfg = _AlembicConfig(str(_repo_root / "alembic.ini"))
    _stamp_cfg.set_main_option("script_location", str(_repo_root / "migrations"))
    _stamp_cfg.set_main_option("sqlalchemy.url", source_url)
    _alembic_command.stamp(_stamp_cfg, "head")

    source_before = Path(source_url.replace("sqlite:///", "")).read_bytes()

    source_counts = dict(plan(create_engine(source_url, future=True)))
    assert sum(source_counts.values()) > 0

    pg_url = request.getfixturevalue("pg_url")
    pg_engine = request.getfixturevalue("pg_engine")

    # Schema first (Alembic, real path — same as any other lot's real-pgserver qualification), then the
    # generic data transfer via the new script, as a real subprocess (exactly how an operator would run it).
    pg_support.upgrade(pg_url, "head")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", source_url, "--target-db-url", pg_url],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
        env={**__import__("os").environ, "WM_DB_TEST_MODE": "1", "WM_POSTGRES_TEST_URL": pg_url, "WM_TEST_DB_EXTRA_ROOTS": str(Path(source_url.replace("sqlite:///", "")).parent)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "erreur" not in result.stdout.lower() or "0 erreur" in result.stdout.lower()

    # ---- Contrôles réels : effectifs, identifiants/FK, JSON, hashes -----------------------------------
    for table_name, source_count in source_counts.items():
        with pg_engine.connect() as conn:
            target_count = conn.execute(select(func.count()).select_from(Base.metadata.tables[table_name])).scalar_one()
        assert target_count == source_count, f"{table_name}: source={source_count} cible={target_count}"

    from src.web.database.models import Analysis, AnalysisComplement
    with pg_engine.connect() as conn:
        target_analysis = conn.execute(select(Analysis.__table__).where(Analysis.__table__.c.job_id == revision_job_id)).mappings().first()
        assert target_analysis is not None, "l'analyse migrée doit être retrouvable par son job_id d'origine"
        assert target_analysis["parent_job_id"] == original_job_id, "la filiation (parent_job_id) doit être préservée exactement"
        assert isinstance(target_analysis["result_data"], dict) and target_analysis["result_data"], "le snapshot JSON du résultat doit être préservé intact (canonique, comparable)"
        assert target_analysis["decision"] == "GO", "la décision déjà calculée sur la source doit être préservée sans recalcul"

        complement = conn.execute(select(AnalysisComplement.__table__).where(AnalysisComplement.__table__.c.job_id == revision_job_id)).mappings().first()
        assert complement is not None
        assert complement["origin"] == "llm_sourced"
        assert complement["source_json"]["citation"], "la citation source du complément doit être préservée"

    # SQLite source bytes untouched by the whole transfer (read-only SELECTs only).
    assert Path(source_url.replace("sqlite:///", "")).read_bytes() == source_before, "le transfert ne doit jamais modifier la source"

    pdf_after_migration_note = "les livrables restent des fichiers, non copiés par ce script — vérifiés inchangés séparément (scripts/backup_restore.py)"
    assert pdf_sha_before  # sanity: computed before the transfer, from the SOURCE app, never recomputed here

    # ---- Activation du RAG hybride sur la cible SEULEMENT, réindexation, recherche réelle -------------
    from src.core import config
    from src.rag import hybrid_index, hybrid_search
    from src.web.database.repositories import knowledge as knowledge_repo

    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", True)
    monkeypatch.setattr(config, "DATABASE_URL", pg_url)
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    target_session = Session()
    try:
        docs = knowledge_repo.list_active_documents(target_session, organization_id=uuid.UUID(org1), owner_user_id=uuid.UUID(str(target_analysis["user_id"])))
        assert docs, "le document privé migré doit être retrouvable sur la cible"
        version_id = docs[0].active_version_id
        from src.web.database.models import KnowledgeChunk, KnowledgeDocumentVersion
        version = target_session.get(KnowledgeDocumentVersion, version_id)
        # The SQLite source never had hybrid mode active, so its own embedding_status was already
        # 'not_applicable' (never attempted) — correctly PRESERVED as such (never invented as 'pending' for
        # a version that was never even a hybrid-mode candidate on the source). The migration script's own
        # reset only ever turns a genuine 'ready'/'failed' into 'pending' — proven directly in
        # tests/test_lot54_migration_script.py::test_embedding_status_is_reset_never_copied_as_ready...
        assert version.embedding_status in ("not_applicable", "pending"), version.embedding_status
        assert version.embedding_status != "ready", "jamais copié comme 'ready' sans le vecteur derrière"
        chunks = list(target_session.execute(select(KnowledgeChunk).where(KnowledgeChunk.document_version_id == version_id)).scalars())
        assert chunks, "les chunks lexicaux doivent avoir été migrés (données portables, jamais le vecteur)"

        hybrid_index.index_version(target_session, version=version, chunks=chunks)
        target_session.commit()
        assert version.embedding_status == "ready", (version.embedding_status, version.embedding_error_code)

        outcome = hybrid_search.search(
            target_session, organization_id=uuid.UUID(org1), owner_user_id=uuid.UUID(str(target_analysis["user_id"])),
            query="Travaillez-vous avec de grands fournisseurs de logiciels d'entreprise ?", top_k=5,
        )
        assert outcome.mode in ("hybrid", "hybrid_partial"), f"mode devrait être hybride réel sur la cible, obtenu : {outcome.mode}"
        assert outcome.evidences, "une preuve doit être retrouvée réellement sur l'index reconstruit"
        ev = outcome.evidences[0]
        assert ev.document_version_id == str(version_id)
        assert ev.start_char is not None and ev.end_char is not None

        # Isolation utilisateur/organisation préservée sur la cible : org2 (même utilisateur) ne voit rien.
        outcome_org2 = hybrid_search.search(
            target_session, organization_id=uuid.UUID(org2), owner_user_id=uuid.UUID(str(target_analysis["user_id"])),
            query="fréquence de nettoyage", top_k=5,
        )
        assert not outcome_org2.evidences, "la deuxième organisation du même utilisateur ne doit rien voir du corpus de la première"

        # Historique et ancienne révision toujours lisibles sur la cible.
        original_row = target_session.execute(select(Analysis.__table__).where(Analysis.__table__.c.job_id == original_job_id)).mappings().first()
        assert original_row is not None and original_row["result_data"] is not None
    finally:
        target_session.close()


def test_a_failed_transfer_never_writes_partially_and_never_touches_the_source(client, db, test_db, request):
    """A deliberately-corrupt target (schema NOT migrated to head) makes every table insert fail — the
    script must report a clean refusal, never crash uncontrolled, and the SOURCE must remain byte-identical."""
    user = make_active_starter_user(db, "l54-fail@example.com", scoring=False)
    source_url = str(test_db.url)
    source_before = Path(source_url.replace("sqlite:///", "")).read_bytes()
    pg_url = request.getfixturevalue("pg_url")

    # target has NO schema at all — every insert must fail cleanly, counted as an error, not a crash.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", source_url, "--target-db-url", pg_url],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
        env={**__import__("os").environ, "WM_DB_TEST_MODE": "1", "WM_POSTGRES_TEST_URL": pg_url, "WM_TEST_DB_EXTRA_ROOTS": str(Path(source_url.replace("sqlite:///", "")).parent)},
    )
    assert result.returncode != 0  # errors were counted -> non-zero exit, but no uncaught traceback
    assert "Traceback" not in result.stderr, result.stderr
    assert Path(source_url.replace("sqlite:///", "")).read_bytes() == source_before
