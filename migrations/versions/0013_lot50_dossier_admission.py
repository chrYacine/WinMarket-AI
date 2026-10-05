"""Lot 50 — documents multiples, classement/modération assistés et admission explicite avant analyse.

Purely ADDITIVE, nothing dropped/renamed on any existing column or row, no business data back-filled:

1. `ao_dossiers`: `status` may now also be `'staging'` (a received, vetted dossier awaiting the user's
   confirmation of the final admitted set — never analysed, may expire) alongside the existing `'validated'`
   /`'submitted'`. New columns `staging_expires_at`, `categories_missing`, `scope_limited`,
   `confirmed_by_user_id`, `confirmed_at` — all NULL/False for every dossier that existed before this lot
   (none of them were ever staged, so there is nothing truthful to back-fill).
2. `ao_dossier_pieces`: `category` may now also be `'autre'` (the new free-form slot, §1) alongside the
   4 guided categories and `'annexe'`. New columns for the admission manifest of §2/§3: `category_proposed`,
   `category_final`, `security_state` (defaults to `'authorized'` — every pre-lot-50 piece already passed
   the existing whole-dossier + per-piece security gate, so this default is not a new, weaker claim about
   it), `security_code`, `security_reason`, `moderation_verdict`, `moderation_reason`, `admitted` (defaults
   to `True` — every pre-lot-50 piece was, by definition, part of the analysis it fed), `exclusion_reason`,
   `user_link_note`.

SQLite cannot ALTER a CHECK constraint in place: `ao_dossiers`/`ao_dossier_pieces` are rebuilt via
`batch_alter_table` (Alembic's standard SQLite-safe recreate-table strategy) — a plain `ALTER TABLE ADD
COLUMN`/constraint change on PostgreSQL.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ao_dossiers", schema=None) as batch_op:
        batch_op.drop_constraint("ck_ao_dossiers_status", type_="check")
        batch_op.create_check_constraint("ck_ao_dossiers_status", "status IN ('staging','validated','submitted')")
        batch_op.add_column(sa.Column("staging_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("categories_missing", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("scope_limited", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("confirmed_by_user_id", sa.Uuid(as_uuid=True), nullable=True))
        batch_op.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_ao_dossiers_confirmed_by_user_id", "users", ["confirmed_by_user_id"], ["id"], ondelete="SET NULL",
        )

    with op.batch_alter_table("ao_dossier_pieces", schema=None) as batch_op:
        batch_op.drop_constraint("ck_ao_dossier_pieces_category", type_="check")
        batch_op.create_check_constraint("ck_ao_dossier_pieces_category", "category IN ('rc','cctp','ccap','acte_engagement','annexe','autre')")
        batch_op.add_column(sa.Column("category_proposed", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("category_final", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("security_state", sa.String(length=20), nullable=False, server_default="authorized"))
        batch_op.add_column(sa.Column("security_code", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("security_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("moderation_verdict", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("moderation_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("admitted", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("exclusion_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("user_link_note", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_ao_dossier_pieces_category_proposed",
            "category_proposed IS NULL OR category_proposed IN ('rc','cctp','ccap','acte_engagement','annexe','autre')",
        )
        batch_op.create_check_constraint(
            "ck_ao_dossier_pieces_category_final",
            "category_final IS NULL OR category_final IN ('rc','cctp','ccap','acte_engagement','annexe','autre')",
        )
        batch_op.create_check_constraint("ck_ao_dossier_pieces_security_state", "security_state IN ('authorized','blocked','to_verify')")
        batch_op.create_check_constraint(
            "ck_ao_dossier_pieces_moderation_verdict",
            "moderation_verdict IS NULL OR moderation_verdict IN ('lie','incertain','hors_sujet')",
        )

    # One-time repair, not a business back-fill: `category_final` is defined (see the ORM model docstring) as
    # falling back to `category` when unset — making that explicit for existing rows costs nothing and lets a
    # reader query `category_final` alone without a COALESCE, for every row old and new alike.
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE ao_dossier_pieces SET category_final = category WHERE category_final IS NULL"))


def downgrade() -> None:
    # Refused while any piece carries lot-50 manifest data that would be lost — same policy as 0010/0011/0012:
    # restore a verified backup taken before 0013 instead. A 'staging' dossier or an 'autre' piece has no
    # representation at all in the pre-0013 schema, so downgrading with either present would silently corrupt
    # data (an unrepresentable status/category), not just lose metadata.
    bind = op.get_bind()
    staging = bind.execute(sa.text("SELECT COUNT(*) FROM ao_dossiers WHERE status = 'staging'")).scalar()
    autres = bind.execute(sa.text("SELECT COUNT(*) FROM ao_dossier_pieces WHERE category = 'autre' OR category_final = 'autre'")).scalar()
    manifest = bind.execute(sa.text(
        "SELECT COUNT(*) FROM ao_dossier_pieces WHERE admitted = 0 OR security_state <> 'authorized' "
        "OR moderation_verdict IS NOT NULL OR category_proposed IS NOT NULL"
    )).scalar()
    if staging or autres or manifest:
        raise RuntimeError(
            f"Downgrade of 0013 refused: {staging} staging dossier(s), {autres} 'autre'-category piece(s) and "
            f"{manifest} piece(s) carrying lot-50 admission data exist and would be lost or unrepresentable. "
            "Restore a verified backup taken before 0013 instead."
        )

    with op.batch_alter_table("ao_dossier_pieces", schema=None) as batch_op:
        batch_op.drop_constraint("ck_ao_dossier_pieces_moderation_verdict", type_="check")
        batch_op.drop_constraint("ck_ao_dossier_pieces_security_state", type_="check")
        batch_op.drop_constraint("ck_ao_dossier_pieces_category_final", type_="check")
        batch_op.drop_constraint("ck_ao_dossier_pieces_category_proposed", type_="check")
        batch_op.drop_column("user_link_note")
        batch_op.drop_column("exclusion_reason")
        batch_op.drop_column("admitted")
        batch_op.drop_column("moderation_reason")
        batch_op.drop_column("moderation_verdict")
        batch_op.drop_column("security_reason")
        batch_op.drop_column("security_code")
        batch_op.drop_column("security_state")
        batch_op.drop_column("category_final")
        batch_op.drop_column("category_proposed")
        batch_op.drop_constraint("ck_ao_dossier_pieces_category", type_="check")
        batch_op.create_check_constraint("ck_ao_dossier_pieces_category", "category IN ('rc','cctp','ccap','acte_engagement','annexe')")

    with op.batch_alter_table("ao_dossiers", schema=None) as batch_op:
        batch_op.drop_constraint("fk_ao_dossiers_confirmed_by_user_id", type_="foreignkey")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmed_by_user_id")
        batch_op.drop_column("scope_limited")
        batch_op.drop_column("categories_missing")
        batch_op.drop_column("staging_expires_at")
        batch_op.drop_constraint("ck_ao_dossiers_status", type_="check")
        batch_op.create_check_constraint("ck_ao_dossiers_status", "status IN ('validated','submitted')")
