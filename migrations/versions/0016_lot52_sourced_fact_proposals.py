"""Lot 52 — sourced fact proposals: provenance for a completion complement.

Purely ADDITIVE. `analysis_complements` (lot 49) recorded ONLY what a user declared, with no notion of
where a value came from. Lot 52 lets a value be PROPOSED from a search of the account's own documents
(private knowledge base for prestataire facts, the analysis's own dossier pieces / AO text for ao/acheteur
facts) — the user still explicitly accepts or corrects it, but the accepted trail must say so, honestly:

- `origin`: `'declared_user'` (default — a plain user-typed value, byte-for-byte the lot 49/49 bis
  contract, nothing changes for it) or `'llm_sourced'` (the user accepted a proposal produced by a
  citation-verified LLM read of a specific document/version/passage or dossier piece, unmodified).
  A value the user CORRECTED after seeing a proposal is recorded as `'declared_user'`, never
  `'llm_sourced'` — the ticket is explicit that a citation must never be presented as proof of a value it
  no longer supports.
- `source_json`: the full provenance for an `'llm_sourced'` row (document_version_id/chunk_id or
  dossier piece_id, source label, citation, offsets, offset frame, model id, prompt version, extracted
  timestamp) — `NULL` for `'declared_user'`. Never re-verified FROM this column alone at read time; it is
  the FROZEN record of what was verified once, at submission (src/web/completion_service.py).

Every row that existed before this migration is a `'declared_user'` complement by construction (lot 49/49
bis never had any other origin) — the `server_default` backfills it, no business assumption invented.

SQLite cannot ALTER a CHECK constraint in place: `batch_alter_table` (Alembic's SQLite-safe recreate-table
strategy) is used, exactly like lot 50 bis's own migration 0014 — a plain `ALTER TABLE ADD COLUMN`/
constraint change on PostgreSQL.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_complements", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin", sa.String(length=20), nullable=False, server_default="declared_user"))
        batch_op.add_column(sa.Column("source_json", sa.JSON(), nullable=True))
        batch_op.create_check_constraint(
            "ck_analysis_complements_origin", "origin IN ('declared_user','llm_sourced')",
        )

    bind = op.get_bind()
    bind.execute(sa.text("UPDATE analysis_complements SET origin = 'declared_user' WHERE origin IS NULL"))


def downgrade() -> None:
    # Refused while any row actually carries lot-52 sourced-fact provenance — same policy as every migration
    # since 0010: restore a verified backup taken before 0016 instead.
    bind = op.get_bind()
    sourced = bind.execute(sa.text("SELECT COUNT(*) FROM analysis_complements WHERE origin <> 'declared_user'")).scalar()
    if sourced:
        raise RuntimeError(
            f"Downgrade of 0016 refused: {sourced} complement(s) carry lot-52 sourced-fact provenance and "
            "would be lost or unrepresentable. Restore a verified backup taken before 0016 instead."
        )
    with op.batch_alter_table("analysis_complements", schema=None) as batch_op:
        batch_op.drop_constraint("ck_analysis_complements_origin", type_="check")
        batch_op.drop_column("source_json")
        batch_op.drop_column("origin")
