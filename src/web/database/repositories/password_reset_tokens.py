"""Pure data-access + fast lookup helpers for the `password_reset_tokens`
table (B21-T1).

DEFECT FIXED: verification used to SELECT every live, unexpired token and
run an Argon2 verify() against each one in a Python loop (see git history
of src/web/routes_account.py::reset_password_submit) — Argon2 is
deliberately slow (that is the whole point for `User.password_hash`), so
this scaled O(n) Argon2 verifications per reset attempt across the WHOLE
token table, a real performance/DoS concern that only gets worse as the
table grows. The raw reset token is already `secrets.token_urlsafe(32)` —
256 bits of real CSPRNG entropy, and THAT entropy is what makes it
unguessable, not a slow hash. So verification here is a direct indexed
equality lookup on a fast, deterministic digest (`token_digest`, unique +
indexed) — no loop, no Argon2 anywhere in this path. Argon2
(src/web/auth/service.py) stays exactly where it already is, used only for
`User.password_hash`.

Digest choice: HMAC-SHA256 keyed by `config.SESSION_SECRET` rather than
plain `sha256(raw_token)`. This is defense in depth, not the primary
defense: the raw token's own 256 bits of entropy already makes guessing
infeasible either way. Keying it means a stolen DB dump ALONE (the
token_digest column, without the app's SESSION_SECRET) is not enough to
test candidate tokens against a precomputed table.

Atomic single-use consumption (`try_consume`) follows the exact same idiom
as src/web/database/repositories/analysis_jobs.py::try_claim: a single
conditional UPDATE whose WHERE clause encodes every precondition
(token_digest matches AND not already used AND not expired), with
`rowcount` as the sole correctness signal — never a preceding SELECT
deciding the outcome, which would be a read-then-write TOCTOU race under
two concurrent consumption attempts for the same token.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.core import config
from src.web.database.models import PasswordResetToken


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_digest(raw_token: str) -> str:
    """Fast, deterministic digest used ONLY for reset-token lookup — never
    used for User.password_hash (Argon2 stays there, untouched)."""
    key = (config.SESSION_SECRET or "").encode("utf-8")
    return hmac.new(key, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_token(
    db: Session, *, user_id: uuid.UUID, raw_token: str, ttl_seconds: int = 3600,
) -> PasswordResetToken:
    """Insert a new reset-token row. `token_hash` is deliberately left
    unset (None) — no longer populated, see PasswordResetToken's
    docstring; `token_digest` is the only column verification ever reads."""
    row = PasswordResetToken(
        user_id=user_id,
        token_hash=None,
        token_digest=compute_digest(raw_token),
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )
    db.add(row)
    db.flush()
    return row


def try_consume(db: Session, *, raw_token: str, now: datetime | None = None) -> bool:
    """Atomically mark the token identified by `raw_token` as used, IFF a
    row with that digest exists, is unused and unexpired — a single
    conditional UPDATE, `rowcount == 1` is the sole "I won" signal
    (mirrors analysis_jobs_repo.try_claim exactly). `rowcount == 0` means
    invalid, already-consumed (lost a race) or expired — deliberately
    indistinguishable from the caller's point of view, so no state is
    leaked either way.
    """
    now = now or _now()
    digest = compute_digest(raw_token)
    stmt = (
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_digest == digest,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
    )
    result = db.execute(stmt)
    return (result.rowcount or 0) == 1


def get_by_raw_token(db: Session, *, raw_token: str) -> Optional[PasswordResetToken]:
    """Read-after-write only: call AFTER try_consume has returned True, to
    fetch the row (its user_id above all) whose consumption is already
    durably decided by that UPDATE. Never used to DECIDE whether a token
    is valid — that decision belongs to try_consume's UPDATE alone, never
    a preceding/following SELECT."""
    digest = compute_digest(raw_token)
    stmt = select(PasswordResetToken).where(PasswordResetToken.token_digest == digest)
    return db.execute(stmt).scalar_one_or_none()


def invalidate_other_tokens_for_user(
    db: Session, *, user_id: uuid.UUID, exclude_id: uuid.UUID | None = None, now: datetime | None = None,
) -> int:
    """After a successful reset, invalidate every OTHER still-valid
    (unused) token for the same user — a user who requested a reset twice
    and used the second link must not have the first link still work.
    Bulk conditional UPDATE, same idiom as the rest of this module; safe
    to call even if there are zero other rows (returns 0)."""
    now = now or _now()
    conditions = [PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None)]
    if exclude_id is not None:
        conditions.append(PasswordResetToken.id != exclude_id)
    stmt = update(PasswordResetToken).where(*conditions).values(used_at=now)
    result = db.execute(stmt)
    return result.rowcount or 0
