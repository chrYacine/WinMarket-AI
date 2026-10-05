"""Lot 54 §2 — a GENERIC, whole-database SQLite -> PostgreSQL data transfer, distinct from
scripts/migrate_history_to_postgresql.py (which only ever moved the legacy JSON history file into a
handful of `analyses` rows). This script copies EVERY table `src.web.database.models.Base` declares —
users/auth, organizations/memberships, subscriptions, analyses/results/snapshots, complements/provenance,
AO dossiers/pieces/filiations, knowledge base/versions/chunks (never `knowledge_passages.embedding` itself,
a PostgreSQL-only raw-SQL column never declared on the ORM model — vectors are derived data, re-indexed on
the target instead, see §3 of the lot 54 report) — in one generic pass, so a future schema change is
automatically covered without a rewrite of this script.

Never touches:
- Private files themselves (only their DB references) — see scripts/backup_restore.py for file-level backup/
  restore; this script's job is the database ONLY.
- Any business logic: rows are copied via SQLAlchemy Core `Table` objects bound to each engine directly —
  no repository function, no ORM session event, no job/email/LLM call is ever triggered by this script.
- The vector column / embeddings: not declared on `Base.metadata` at all (raw SQL, PostgreSQL-only, managed
  by src/rag/hybrid_index.py) — never copied, never faked; every migrated `knowledge_document_versions` row
  has its `embedding_status` explicitly reset to 'pending' (never left claiming 'ready' with no vector
  behind it) so the target re-embeds for real (src/rag/hybrid_index.index_version) instead of trusting a
  stale claim.

Modes:
- `--dry-run` (default False): reports row counts per table that WOULD be copied, writes nothing.
- `--mode empty-target` (default): refuses outright if ANY of the target's own tables already has a row —
  "cible vide imposée" (ticket, verbatim). Never guesses that an existing row is safe to skip.
- `--mode resume`: for a target that already received a PARTIAL transfer (a previous run failed partway) —
  skips a row whose PRIMARY KEY already exists in the target (identity by PK ONLY, never a name/email
  match — the ticket explicitly forbids merging accounts by their display identity) and inserts every row
  that is genuinely new. Never updates an existing target row's OTHER columns — a resume never overwrites.

Order: `Base.metadata.sorted_tables` (a real topological sort over every declared ForeignKey) — child tables
are always copied strictly after every table they reference, so `PRAGMA/`constraint` FK checks on the
target never fail on ordering alone. One transaction per table (COMMIT per table, ROLLBACK-per-row via a
SAVEPOINT — same idiom as scripts/migrate_history_to_postgresql.py's own B23-T1 fix) — a bad row inside one
table never aborts a table that already fully committed before it, and a table already fully copied is
never re-attempted from scratch on a `--mode resume` re-run.

One genuinely CIRCULAR reference exists in this schema (DEFECT reproduced and fixed by this lot's own real
pgserver rehearsal — a real FK violation, not a hypothetical one): `knowledge_documents.active_version_id`
references `knowledge_document_versions.id`, which itself references `knowledge_documents.id` via
`document_id` — the model's own `use_alter=True` on that column exists for exactly this reason (SQLite/
PostgreSQL DDL creates it as a deferred `ALTER TABLE`), but a topological sort still has to pick ONE linear
order for the two tables' ROWS, and whichever comes first cannot have this column already pointing at a row
that does not exist yet on the target. `_DEFERRED_FK_COLUMNS` names this ONE column explicitly: it is
inserted as NULL, and backfilled to its real value in a dedicated pass run once BOTH tables are fully
copied — never a second, silent, undocumented special case; the exact column and reason are named here.

Refuses (via src.core.db_target, exactly like every test/migration script in this repository since lot
50 ter) any target that looks like the real application database when WM_DB_TEST_MODE=1 is set — this
script itself never sets that flag; the OPERATOR is responsible for setting it when rehearsing on disposable
data (see docs/qa/lot_54_20260926/RAPPORT.md §3), and for NOT setting it for a real, authorized cutover
(§4 of that report) — the real target is deliberately allowed to be a real, persistent PostgreSQL server in
that case, which `WM_DB_TEST_MODE=1` would incorrectly refuse.

Never triggers a job/email/LLM/reindex/maintenance pass during the copy — see the module docstring above.

Lot 55 additions (schema/resume correctness, never invented for their own sake):
- **Resume-mode value comparison** (`_existing_rows`/`_diverging_columns`): an existing PRIMARY KEY on the
  target is no longer treated as proof the row is identical. Every column is compared against the source's
  canonical value, excluding ONLY the two explicitly-documented derived transforms this script itself applies
  (`embedding_status`/`embedding_indexed_at` on `_RESET_EMBEDDING_STATUS_TABLE`, and each table's own
  `_DEFERRED_FK_COLUMNS` entry, which is legitimately NULL mid-transfer pending `_backfill_deferred_fk`). A
  genuine divergence is refused and counted as an error (non-zero exit) — never silently skipped or
  overwritten.
- **Schema-compatibility check** (`_assert_schema_revisions_compatible`): before a real (non-dry-run)
  transfer, if EITHER side carries a real Alembic `alembic_version` row (the actual cutover case — a
  disposable rehearsal/test database built directly via `Base.metadata.create_all`, with no such row on
  either side, is unaffected and unblocked as before), both must sit at the SAME expected head revision
  (read from `migrations/`'s own `ScriptDirectory`, never assumed). Table existence alone was never proof of
  column-level compatibility for a future schema change.
- **No sequence-reset mechanism** — deliberately absent. Confirmed by inspection (grep of
  `src/web/database/models.py` for `autoincrement`/`Identity(`/`Sequence(`): every primary key in this
  schema is a Python-side UUID (`default=uuid.uuid4`) or a Python-generated string (`AnalysisJob.id`) —
  never a PostgreSQL-generated sequence value. Adding a sequence-reset step here would be an unused
  mechanism for a schema that has none.
- **Source/target URLs via environment variables** (`WM_MIGRATE_SOURCE_DB_URL`/`WM_MIGRATE_TARGET_DB_URL`),
  preferred over `--source-db-url`/`--target-db-url` for a real run — a password embedded in a literal CLI
  argument lands in shell history/process listings/this script's own printed argv; an env var does not. The
  printed summary always uses `render_as_string(hide_password=True)` regardless of which path supplied it.
- **Deferred-FK backfill never touches a row absent from the target** (`_backfill_deferred_fk` now reports
  `rowcount`, not an assumed success) — a source row with no corresponding target row (e.g. it failed to
  copy) is reported, never silently treated as backfilled, and counts toward a non-zero exit code.

Usage:
    python scripts/migrate_sqlite_to_postgresql.py [--source-db-url sqlite:///path/to/source.db] \\
        [--target-db-url postgresql+pg8000://user:pass@host/db] [--dry-run] [--mode empty-target|resume] \\
        [--batch-size 500]
    (or set WM_MIGRATE_SOURCE_DB_URL/WM_MIGRATE_TARGET_DB_URL instead of the two --*-db-url flags)
"""
from __future__ import annotations

# Lot 56: retained for regression/legacy reference, not an authorized data-import path.
import os as _guard_os
if __name__ == '__main__' and _guard_os.getenv('WM_DB_TEST_MODE') != '1':
    raise SystemExit('Legacy CLI disabled in this isolated delivery. Use local_env.py, operator_access.py and runtime_backup.py. Historical import requires a separate operation.')

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select, text

from src.core.db_target import _in_test_mode, assert_disposable_test_target
from src.web.database.models import Base, KnowledgeDocument, KnowledgeDocumentVersion

# Tables this script deliberately treats specially — never a silent, undocumented special case.
_RESET_EMBEDDING_STATUS_TABLE = KnowledgeDocumentVersion.__tablename__
_SKIP_ROW_COPY_TABLES: set[str] = set()  # currently none skipped outright — KnowledgePassage rows ARE
# copied (offsets/content/model metadata are real, portable data — only the vector column, which does not
# exist on this ORM model at all, is naturally never touched); their target embedding readiness is instead
# implied by the version-level reset below, which every real re-embed already keys off of.

# (table_name, column_name): a column whose FK target is only fully available AFTER a table further down
# the sort order has been copied (the one genuine circular reference in this schema — see module docstring).
# Inserted as NULL, backfilled by _backfill_deferred_fk once every table has been copied.
_DEFERRED_FK_COLUMNS: dict[str, str] = {KnowledgeDocument.__tablename__: "active_version_id"}

# Lot 55 — columns EXPLICITLY excluded from the resume-mode value-divergence check because this script
# itself documents them as derived/transient, never because comparing them would be inconvenient. Anything
# not listed here (or in _DEFERRED_FK_COLUMNS, excluded separately) must match the source exactly, or the
# row is refused as a genuine divergence.
_VALUE_COMPARISON_EXCLUDED_COLUMNS: dict[str, set[str]] = {
    KnowledgeDocumentVersion.__tablename__: {"embedding_status", "embedding_indexed_at"},
}


class TargetSchemaMissing(Exception):
    """Lot 54 §3 (DEFECT reproduced and fixed): counting rows on a target whose schema was never migrated to
    head used to crash with a raw, unhelpful DB-driver traceback ("relation does not exist") — an ordinary,
    expected precondition failure (the operator forgot `alembic upgrade head` on the target first), never a
    script bug. Caught explicitly in main() and reported as a clean, one-line message — never a traceback."""


def _table_row_count(engine, table) -> int:
    try:
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar_one()
    except Exception as exc:
        raise TargetSchemaMissing(
            f"La cible ne semble pas avoir son schéma migré à la tête Alembic (table {table.name!r} "
            f"introuvable ou inaccessible : {type(exc).__name__}). Exécutez d'abord "
            "`alembic upgrade head` (avec la bonne cible) avant ce script."
        ) from None


def _existing_rows(engine, table) -> dict[tuple, dict]:
    """Lot 55 — replaces the old `_existing_pks` (PK-only). Resume mode needs the FULL existing row to prove
    a PK match is actually the SAME row, not merely occupying the same identity slot."""
    pk_cols = [c.name for c in table.primary_key.columns]
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(table)).all()
    except Exception as exc:
        raise TargetSchemaMissing(
            f"La cible ne semble pas avoir son schéma migré à la tête Alembic (table {table.name!r} "
            f"introuvable ou inaccessible : {type(exc).__name__}). Exécutez d'abord "
            "`alembic upgrade head` (avec la bonne cible) avant ce script."
        ) from None
    result: dict[tuple, dict] = {}
    for row in rows:
        row_dict = dict(row._mapping)
        result[tuple(row_dict[c] for c in pk_cols)] = row_dict
    return result


def _diverging_columns(table_name: str, source_row: dict, target_row: dict) -> list[str]:
    """Columns where the source's canonical value differs from what is already on the target, excluding
    only the explicitly-documented derived/transient columns (see _VALUE_COMPARISON_EXCLUDED_COLUMNS and
    _DEFERRED_FK_COLUMNS). An existing primary key alone is never treated as proof of an identical row."""
    excluded = set(_VALUE_COMPARISON_EXCLUDED_COLUMNS.get(table_name, ()))
    deferred_col = _DEFERRED_FK_COLUMNS.get(table_name)
    if deferred_col:
        excluded.add(deferred_col)
    return [col for col, value in source_row.items() if col not in excluded and target_row.get(col) != value]


def _expected_head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise SystemExit(
            f"REFUSED : {len(heads)} tête(s) Alembic trouvée(s) dans migrations/ (1 attendue) — "
            "dépôt de migrations ambigu, à corriger avant toute bascule."
        )
    return heads[0]


def _current_alembic_revision(engine) -> str | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row else None
    except Exception:
        return None


def _assert_schema_revisions_compatible(source_engine, target_engine) -> None:
    """Lot 55 — "la table existe" n'est pas la preuve qu'un schéma est compatible pour une VRAIE bascule.

    Si NI la source NI la cible n'a de table `alembic_version` renseignée (schéma créé directement via
    `Base.metadata.create_all` — le cas de tous les scénarios jetables/tests existants, jamais celui d'une
    vraie base applicative), ce contrôle reste sans objet et ne bloque rien de plus qu'avant. Dès que l'UN
    des deux côtés a une révision Alembic réelle, les DEUX doivent être exactement à la tête attendue —
    sinon un futur changement de colonne pourrait passer inaperçu derrière une simple présence de table.
    """
    source_rev = _current_alembic_revision(source_engine)
    target_rev = _current_alembic_revision(target_engine)
    if source_rev is None and target_rev is None:
        return
    expected = _expected_head_revision()
    problems = []
    if source_rev != expected:
        problems.append(f"source à la révision {source_rev!r} (attendu {expected!r})")
    if target_rev != expected:
        problems.append(f"cible à la révision {target_rev!r} (attendu {expected!r})")
    if problems:
        raise SystemExit(
            "REFUSED : schéma non compatible avant transfert — " + " ; ".join(problems) +
            ". Exécutez `alembic -x db_url=<URL> upgrade head` (résolveur lot 50 ter) sur chaque côté "
            "avant de relancer ce script."
        )


def plan(engine) -> list[tuple[str, int]]:
    """(table_name, row_count) for every table on the given engine, in FK-safe order — read-only, safe to
    call in --dry-run or before/after a real run to compare source vs. target."""
    return [(table.name, _table_row_count(engine, table)) for table in Base.metadata.sorted_tables]


def _refuse_if_target_not_empty(target_engine) -> None:
    non_empty = [table.name for table in Base.metadata.sorted_tables if _table_row_count(target_engine, table) > 0]
    if non_empty:
        raise SystemExit(
            "REFUSED (--mode empty-target) : la cible contient déjà des lignes dans "
            f"{len(non_empty)} table(s) ({', '.join(non_empty[:8])}{'…' if len(non_empty) > 8 else ''}) — "
            "une migration générale exige une cible vide, jamais une fusion implicite. "
            "Utilisez --mode resume si une tentative précédente a été interrompue et doit reprendre."
        )


def copy_table(source_engine, target_engine, table, *, mode: str, batch_size: int) -> dict:
    """Copies one table's rows, source -> target, in the SAME transaction for the whole table (a SAVEPOINT
    per row so one bad row never aborts the rows already flushed before it — see module docstring). Returns
    {"copied", "skipped_existing", "errors"} — never raises for a single bad row; the caller's own report
    surfaces the count, and the row itself is printed WITHOUT its content (a table may hold a password hash
    or an email — this script counts, it never prints row data)."""
    existing_rows = _existing_rows(target_engine, table) if mode == "resume" else {}
    pk_cols = [c.name for c in table.primary_key.columns]

    copied = skipped = errors = 0
    with source_engine.connect() as src_conn:
        result = src_conn.execution_options(stream_results=True).execute(select(table))
        with target_engine.begin() as tgt_conn:
            while True:
                batch = result.fetchmany(batch_size)
                if not batch:
                    break
                for row in batch:
                    row_dict = dict(row._mapping)
                    pk_tuple = tuple(row_dict[c] for c in pk_cols)
                    if mode == "resume" and pk_tuple in existing_rows:
                        divergences = _diverging_columns(table.name, row_dict, existing_rows[pk_tuple])
                        if divergences:
                            errors += 1
                            print(
                                f"  [{table.name}] DIVERGENCE sur la ligne {dict(zip(pk_cols, pk_tuple))} "
                                f"(colonnes: {divergences}) — refusé, jamais écrasé ni ignoré silencieusement."
                            )
                            continue
                        skipped += 1
                        continue
                    deferred_col = _DEFERRED_FK_COLUMNS.get(table.name)
                    if deferred_col and row_dict.get(deferred_col) is not None:
                        row_dict[deferred_col] = None  # backfilled by _backfill_deferred_fk after all tables are copied
                    if table.name == _RESET_EMBEDDING_STATUS_TABLE and "embedding_status" in row_dict:
                        # Lot 54 §2 — vectors are derived data, never copied (no vector column exists on
                        # this ORM model at all); a version that claimed 'ready' on the source would be
                        # lying on the target, which has no vector behind that claim yet.
                        row_dict["embedding_status"] = "pending" if row_dict.get("embedding_status") != "not_applicable" else "not_applicable"
                        row_dict["embedding_indexed_at"] = None
                    savepoint = tgt_conn.begin_nested()
                    try:
                        tgt_conn.execute(table.insert().values(**row_dict))
                        savepoint.commit()
                        copied += 1
                    except Exception as exc:  # noqa: BLE001 — one bad row must not abort the whole table
                        savepoint.rollback()
                        errors += 1
                        pk_repr = {c: row_dict.get(c) for c in pk_cols}  # PK only — never full row content
                        print(f"  [{table.name}] erreur sur la ligne {pk_repr} : {type(exc).__name__}")
    return {"copied": copied, "skipped_existing": skipped, "errors": errors}


def _backfill_deferred_fk(source_engine, target_engine, table_name: str, column: str) -> dict:
    """Re-reads the SOURCE table's (pk, deferred_column) pairs and UPDATEs the target's matching rows back
    to their real value — run once, after every table (including the one the deferred column points at) has
    been copied. Returns {"updated", "missing_targets"} — a source row whose PK has NO matching row on the
    target (e.g. it failed to copy earlier) is never silently counted as backfilled: the UPDATE affects zero
    rows, which is detected via `rowcount` and reported, never mistaken for success (lot 55 — "never modify
    a row foreign to this migration" also means never CLAIM to have modified one that was never there)."""
    table = Base.metadata.tables[table_name]
    pk_col = list(table.primary_key.columns)[0]
    updated = 0
    missing_targets = []
    with source_engine.connect() as src_conn:
        rows = src_conn.execute(select(pk_col, table.c[column]).where(table.c[column].is_not(None))).all()
    if not rows:
        return {"updated": 0, "missing_targets": []}
    with target_engine.begin() as tgt_conn:
        for pk_value, fk_value in rows:
            result = tgt_conn.execute(table.update().where(pk_col == pk_value).values(**{column: fk_value}))
            if result.rowcount == 1:
                updated += 1
            else:
                missing_targets.append(pk_value)
    return {"updated": updated, "missing_targets": missing_targets}


def _reconcile_interrupted_jobs_on_target(target_engine) -> int:
    """Lot 55 — a source `analysis_jobs` row still 'queued'/'running' at the moment writes were stopped for
    the cutover copies onto the target as-is (this script's job is a faithful row-for-row transfer, never a
    business-logic recompute) — but the ORIGINAL worker process that would have picked it up is gone for
    good once the source is retired: left alone, that row would sit 'running' forever on the target, never
    claimed by anything. Reuses the SAME reconciliation `scripts/backup_restore.py`'s own restore path
    already calls (`analysis_jobs_repo.reconcile_stale_jobs`) — never a second, divergent implementation of
    "what does an interrupted job look like"."""
    from sqlalchemy.orm import sessionmaker

    from src.web.database.repositories import analysis_jobs as analysis_jobs_repo

    Session = sessionmaker(bind=target_engine)
    session = Session()
    try:
        count = analysis_jobs_repo.reconcile_stale_jobs(session, staleness_seconds=0)
        session.commit()
        return count
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source-db-url", default=None,
        help="Or set WM_MIGRATE_SOURCE_DB_URL instead — avoids a password ending up in shell history.",
    )
    parser.add_argument(
        "--target-db-url", default=None,
        help="Or set WM_MIGRATE_TARGET_DB_URL instead — avoids a password ending up in shell history.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["empty-target", "resume"], default="empty-target")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    # Lot 55 — env vars are the PREFERRED way to supply a real, password-bearing URL: a literal CLI argument
    # lands in shell history and process listings; an environment variable set for a single invocation does
    # not. Never printed as-is below — always through render_as_string(hide_password=True).
    source_db_url = args.source_db_url or os.environ.get("WM_MIGRATE_SOURCE_DB_URL", "")
    target_db_url = args.target_db_url or os.environ.get("WM_MIGRATE_TARGET_DB_URL", "")
    if not source_db_url or not target_db_url:
        print(
            "REFUSED : URL source et/ou cible manquante. Fournissez --source-db-url/--target-db-url, ou "
            "(recommandé pour un vrai mot de passe) WM_MIGRATE_SOURCE_DB_URL/WM_MIGRATE_TARGET_DB_URL."
        )
        return 1

    if source_db_url.strip().lower() == target_db_url.strip().lower():
        print("REFUSED : source et cible sont identiques.")
        return 1

    # Lot 54 §2 — the SAME structural guard every test/migration script in this repository uses since lot
    # 50 ter (src.core.db_target), applied explicitly here (this script manages two independent engines of
    # its own, never the app's singleton session): active ONLY when WM_DB_TEST_MODE=1/pytest is set — the
    # operator's own responsibility to set for a disposable rehearsal (docs/qa/lot_54_20260926/RAPPORT.md
    # §3) and to NOT set for a real, authorized cutover (§4), where the target is deliberately a real,
    # persistent PostgreSQL server this guard would otherwise refuse.
    if _in_test_mode():
        assert_disposable_test_target(source_db_url)
        assert_disposable_test_target(target_db_url)

    source_engine = create_engine(source_db_url, future=True)
    target_engine = create_engine(target_db_url, future=True)

    print(f"Source : {source_engine.url.render_as_string(hide_password=True)}")
    print(f"Cible  : {target_engine.url.render_as_string(hide_password=True)}")
    print(f"Mode   : {args.mode}{' (DRY-RUN — aucune écriture)' if args.dry_run else ''}\n")

    table_plan = plan(source_engine)
    total_rows = sum(count for _, count in table_plan)
    for name, count in table_plan:
        print(f"  {name:<32} {count:>8} ligne(s) côté source")
    print(f"\n{total_rows} ligne(s) au total sur {len(table_plan)} table(s).")

    if args.dry_run:
        print("\nAucune écriture effectuée (--dry-run). La cible n'a pas été interrogée : ce résumé ne porte que sur la source.")
        source_engine.dispose()
        target_engine.dispose()
        return 0

    try:
        if args.mode == "empty-target":
            _refuse_if_target_not_empty(target_engine)
    except TargetSchemaMissing as exc:
        print(f"\nREFUSED : {exc}")
        source_engine.dispose()
        target_engine.dispose()
        return 1

    # Lot 55 — table existence alone is not proof of column-level compatibility; see
    # _assert_schema_revisions_compatible's own docstring for exactly when this does/doesn't apply.
    _assert_schema_revisions_compatible(source_engine, target_engine)

    print()
    grand_total = {"copied": 0, "skipped_existing": 0, "errors": 0}
    try:
        for table in Base.metadata.sorted_tables:
            if table.name in _SKIP_ROW_COPY_TABLES:
                continue
            report = copy_table(source_engine, target_engine, table, mode=args.mode, batch_size=args.batch_size)
            grand_total["copied"] += report["copied"]
            grand_total["skipped_existing"] += report["skipped_existing"]
            grand_total["errors"] += report["errors"]
            print(f"  {table.name:<32} copiées={report['copied']:<6} déjà présentes={report['skipped_existing']:<6} erreurs={report['errors']}")
    except TargetSchemaMissing as exc:
        print(f"\nREFUSED : {exc}")
        source_engine.dispose()
        target_engine.dispose()
        return 1

    for table_name, column in _DEFERRED_FK_COLUMNS.items():
        backfill_report = _backfill_deferred_fk(source_engine, target_engine, table_name, column)
        print(
            f"  [{table_name}.{column}] {backfill_report['updated']} valeur(s) réattribuée(s) après coup "
            "(référence circulaire connue)"
        )
        if backfill_report["missing_targets"]:
            grand_total["errors"] += len(backfill_report["missing_targets"])
            print(
                f"    ATTENTION : {len(backfill_report['missing_targets'])} ligne(s) source sans ligne cible "
                f"correspondante (jamais écrites) : {backfill_report['missing_targets'][:5]}"
            )

    interrupted_count = _reconcile_interrupted_jobs_on_target(target_engine)
    if interrupted_count:
        print(
            f"\n{interrupted_count} job(s) queued/running réattribué(s) 'interrupted' sur la cible — le "
            "processus source qui les traitait ne les reprendra jamais après la bascule."
        )

    print(f"\n=== Total : {grand_total['copied']} copiée(s), {grand_total['skipped_existing']} déjà présente(s), {grand_total['errors']} erreur(s) ===")
    print("\nAucun fichier source (documents privés/livrables) n'a été déplacé ni supprimé par ce script — voir scripts/backup_restore.py pour leur transfert, et réindexer le RAG hybride séparément (les vecteurs ne sont jamais copiés).")
    source_engine.dispose()
    target_engine.dispose()
    return 1 if grand_total["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
