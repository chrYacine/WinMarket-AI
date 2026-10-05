"""B21-T1 — password reset hardening: fast token lookup + session
revocation.

Purely additive: two new/altered columns, nothing dropped, nothing
renamed. Tested (see tests/test_b21_t1_password_reset.py) against both a
fresh empty SQLite db and one seeded with rows from migrations 0001-0007
applied first.

1. `users.session_version` (Integer, NOT NULL, server_default '0'):
   bumped by 1 as part of the same transaction as a password reset
   (src/web/routes_account.py::reset_password_submit). Embedded in the
   signed session cookie alongside user_id (src/web/auth/
   session_cookie.py) and compared against this column's CURRENT value on
   every read (src/web/auth/dependencies.py::get_current_user) — a
   mismatch is treated exactly like "no session". This is what makes
   EVERY previously-issued cookie for that user invalid immediately after
   a reset, without a server-side session store or a revocation list.
   Backfilled to 0 for every existing row. A cookie minted BEFORE this
   migration was a plain signed string (no embedded version at all, a
   different shape entirely) — session_cookie.py treats any cookie that
   does not deserialize into the new {"uid", "v"} shape as invalid, so
   every user with an old-shaped cookie simply has to log in again once
   this migration ships. This is a deliberate, safe choice (documented in
   docs/api/B21_T1_PASSWORD_RESET_CONTRACT.md), not an oversight.

2. `password_reset_tokens.token_digest` (String(64), nullable, unique
   index): fast HMAC-SHA256 digest of the raw reset token, looked up with
   a direct `WHERE token_digest = :digest` — replaces the pre-existing
   O(n) Argon2-verify-every-live-token loop. `password_reset_tokens.
   token_hash` (the old Argon2 hash column) is kept but made nullable and
   is no longer populated by new writes — additive/non-destructive rather
   than dropped. Existing pre-migration rows get NULL in token_digest
   (their raw token was never persisted, only its Argon2 hash) and can
   never be matched again post-migration — not a new data-loss regression,
   since those tokens were already only reachable via the now-removed
   Argon2 loop.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-17
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )

    with op.batch_alter_table("password_reset_tokens") as batch_op:
        batch_op.alter_column("token_hash", existing_type=sa.String(length=255), nullable=True)
        batch_op.add_column(sa.Column("token_digest", sa.String(length=64), nullable=True))

    op.create_index(
        "uq_password_reset_tokens_token_digest",
        "password_reset_tokens",
        ["token_digest"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_password_reset_tokens_token_digest", table_name="password_reset_tokens")
    with op.batch_alter_table("password_reset_tokens") as batch_op:
        batch_op.drop_column("token_digest")
        # Best-effort only: a downgrade after any post-migration row was
        # written with token_hash left NULL (the new, expected shape) will
        # fail this NOT NULL restore — downgrading past this revision on a
        # database that has taken live B21-T1 traffic is not a supported
        # path, only "never applied a real reset yet" is.
        batch_op.alter_column("token_hash", existing_type=sa.String(length=255), nullable=False)
    op.drop_column("users", "session_version")
