"""Personal (per-user) analysis history — the V3 read path for /app/historique.

PostgreSQL is now the source of truth for the FastAPI SaaS UI. The legacy
global JSON file (`data/historique/historique_ao.json`) is no longer written
by the application (lot 43) and this module never reads it; it stays on disk
as data, importable with scripts/migrate_history_to_postgresql.py.

Records are formatted into the exact same dict shape the existing history
template/JS already expect (titre, client, secteur, decision, score,
budget, techs, date, resultat, job_id), so no frontend change was needed
beyond swapping the data source.
"""
from __future__ import annotations

import uuid
from typing import Iterator

from sqlalchemy.orm import Session

from src.web.database.models import Analysis
from src.web.database.repositories import analyses as analyses_repo
from src.web.database.repositories import ao_dossiers as dossiers_repo

# B22-T1: bounded batch size for the export path — real pagination/streaming
# never materializes the whole authorized set in one SQL round-trip, however
# large it is.
_EXPORT_BATCH_SIZE = 500

# CSV formula-injection guard (B22-T1): a cell whose value begins with one of
# these characters is interpreted as a FORMULA by Excel/LibreOffice/Sheets
# when the file is opened, not as plain text — a stored title/client name
# containing "=HYPERLINK(...)" or "=cmd|'/c calc'!A1" (both real, documented
# spreadsheet-formula-injection payloads) would execute on open. Neutralized
# ONLY in the exported cell (a leading apostrophe forces "treat as text" in
# every major spreadsheet application) — the STORED value in `analyses` is
# never modified by this.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_csv_cell(value: str) -> str:
    if value and value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def _format_record(analysis: Analysis) -> dict:
    # Migrated legacy rows with no real job_id get a synthetic
    # "legacy:..." key purely for dedup (see scripts/migrate_history_to_postgresql.py)
    # — never expose that as an openable link.
    job_id = analysis.job_id if analysis.job_id and not analysis.job_id.startswith("legacy:") else None
    return {
        "ao_id": analysis.job_id or str(analysis.id),
        "titre": analysis.title or "",
        "client": analysis.client_name or "",
        "secteur": analysis.sector or "Non renseigne",
        "decision": analysis.decision or "",
        "score": float(analysis.score) if analysis.score is not None else 0,
        "budget": analysis.budget,
        "techs": analysis.technologies or [],
        "date": analysis.created_at.strftime("%d/%m/%Y %H:%M"),
        "resultat": "en attente",
        "job_id": job_id,
        # Lot 53 — lets the history list distinguish a completion revision (lot 49, declarative) from a
        # documentary re-analysis (lot 50 bis §3, fresh extraction/scoring) from an ordinary analysis, and
        # link to its parent/origin without a second round-trip. Both are None for an ordinary analysis and
        # for any row migrated from the pre-B02 legacy JSON (job_id is also None there — no link is ever
        # shown for a row with no real job_id, see job_id's own comment above).
        "parent_job_id": analysis.parent_job_id if job_id else None,
        "origin_job_id": analysis.origin_job_id if job_id else None,
    }


def _attach_dossier_labels(db: Session, records: list[dict], user_id: uuid.UUID, organization_id: uuid.UUID) -> list[dict]:
    """Lot 47 bis: an analysis built from an AO dossier says so in the history ("5 pièces : RC, CCTP, …"). One query for
    the whole page; a single-file analysis simply has no `dossier` key value."""
    from src.web.ao_dossier import limits as dossier_limits
    by_job = dossiers_repo.piece_categories_for_jobs(
        db, job_ids=[r["job_id"] for r in records if r["job_id"]], organization_id=organization_id, user_id=user_id)
    for record in records:
        categories = by_job.get(record["job_id"])
        if categories:
            shown = ", ".join(dict.fromkeys(dossier_limits.CATEGORY_SHORT[c] for c in categories))
            record["dossier"] = f"{len(categories)} pièce{'s' if len(categories) > 1 else ''} : {shown}"
    return records


def list_for_user(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> list[dict]:
    """Kept for "N most recent" sidebar panels only — see
    analyses_repo.list_for_user's own docstring. Never use this for a
    complete listing, a total count, or an export: it is bounded at 200
    rows with no way to see beyond that (B22-T1's confirmed defect).

    B22-T2: `organization_id` is required — see analyses_repo.list_for_user
    for why (this used to silently aggregate every organization the caller
    belongs to). Must come from a server-resolved AccessContext, never a
    client-supplied value."""
    return [_format_record(a) for a in analyses_repo.list_for_user(db, user_id, organization_id)]


def list_for_user_page(
    db: Session, user_id: uuid.UUID, organization_id: uuid.UUID, *, page: int, page_size: int
) -> tuple[list[dict], int]:
    """B22-T1: real pagination over the user's FULL authorized set.
    `page` is 1-indexed. Returns (this page's records, total row count) —
    the total always comes from a real COUNT(*) (analyses_repo.
    count_for_user), never from len() of whatever page happened to be
    loaded, so a caller can compute total_pages correctly regardless of
    page_size.

    B22-T2: `organization_id` scopes the page and the total together to ONE
    organization — see analyses_repo.list_for_user's docstring."""
    page = max(1, page)
    page_size = max(1, page_size)
    offset = (page - 1) * page_size
    rows = analyses_repo.list_for_user_page(db, user_id, organization_id, limit=page_size, offset=offset)
    total = analyses_repo.count_for_user(db, user_id, organization_id)
    return _attach_dossier_labels(db, [_format_record(a) for a in rows], user_id, organization_id), total


def iter_for_export(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> Iterator[dict]:
    """B22-T1: yields EVERY authorized record, in bounded batches
    (_EXPORT_BATCH_SIZE at a time via analyses_repo.list_for_user_page) —
    never a single unbounded query, never the previous 200-row cap. The
    caller (api_export_history_csv) streams these straight into the CSV
    writer, so the full export is never held in memory as one Python list
    either.

    B22-T2: `organization_id` scopes the export to ONE organization — an
    export must never silently include another organization's analyses
    just because the same user also belongs to it."""
    offset = 0
    while True:
        batch = analyses_repo.list_for_user_page(db, user_id, organization_id, limit=_EXPORT_BATCH_SIZE, offset=offset)
        if not batch:
            return
        for analysis in batch:
            yield _format_record(analysis)
        offset += len(batch)


def sidebar_stats(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> dict:
    """B22-T1 (DEFECT confirmed): previously counted GO/RESERVE/NO-GO from
    `list_for_user(...)`'s own 200-row-capped list — an account with more
    than 200 analyses got silently WRONG stats (undercounted, truncated to
    the 200 most recent). Now a real SQL COUNT(*) for the total and a SQL
    GROUP BY for the decision breakdown (analyses_repo.count_for_user /
    decision_counts_for_user) — correct over the full authorized set
    regardless of table size, and cheaper (no row ever loaded into Python
    just to be counted).

    B22-T2: `organization_id` scopes the stats to ONE organization — the
    sidebar must reflect the currently selected organization's own
    analyses, never an aggregate across every organization the user
    belongs to."""
    total = analyses_repo.count_for_user(db, user_id, organization_id)
    by_decision = analyses_repo.decision_counts_for_user(db, user_id, organization_id)
    go = by_decision.get("GO", 0)
    nogo = by_decision.get("NO-GO", 0)
    reserve = sum(count for decision, count in by_decision.items() if "RESERVE" in decision.upper())
    return {"total": total, "go": go, "reserve": reserve, "nogo": nogo}
