"""SQLAlchemy ORM models for the WinMarket AI SaaS layer (V3).

These tables are additive: they do not touch, replace or read any of the
existing business models in src/core/models.py (AOContext, ScoringResult,
...), which remain the pipeline's data contract. `Analysis.result_data`
simply stores a serialized copy of those for a given user.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Boolean,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# JSONB on Postgres (production target); falls back to a portable JSON type
# on any other dialect (e.g. SQLite in local/unit tests).
JSONType = JSONB().with_variant(JSON(), "sqlite")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('pending','active','rejected','disabled')", name="ck_users_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # B21-T1: session revocation "security stamp". Embedded in the signed
    # session cookie (src/web/auth/session_cookie.py::set_user_session)
    # alongside user_id; src/web/auth/dependencies.py::get_current_user
    # rejects any cookie whose embedded version doesn't match this CURRENT
    # value. Bumped by 1 as part of the same transaction as a password
    # reset (src/web/routes_account.py::reset_password_submit), which makes
    # every previously-issued cookie for this user invalid immediately —
    # a real, DB-provable revocation, without a server-side session store.
    # Migration 0008 backfills every existing row to 0; see that
    # migration's docstring and docs/api/B21_T1_PASSWORD_RESET_CONTRACT.md
    # for how a cookie minted before that migration is treated (invalid).
    session_version: Mapped[int] = mapped_column(nullable=False, default=0)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    documents: Mapped[list["AnalysisDocument"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    memberships: Mapped[list["Membership"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Organization(Base):
    """B02: the billing-adjacent, data-owning space a Membership grants access
    to. Distinct from a User (a person) and from an AO's buyer/client (an
    analyzed third party, stored as free text on Analysis — never a row
    here). Every user gets a private Organization at registration; sharing
    across users is only ever explicit (a Membership row), never inferred
    from a matching `company` or email domain.
    """
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("status IN ('active','suspended')", name="ck_organizations_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    # Temporary B02/B03 boundary flag — see src/web/auth/access_context.py and
    # config.MULTI_CLIENT_MODE. True only for the organization(s) explicitly
    # vetted to share the current global RAG corpus / capacity plan, which
    # are NOT yet isolated per organization (that is B03). False by default:
    # a new organization gets no access to analyze/knowledge/capacity routes
    # once MULTI_CLIENT_MODE is enabled, until B03 ships real isolation.
    corpus_access: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="organization")


class Membership(Base):
    """A user's appartenance to an organization: role + status. Kept even
    once revoked (status='revoked') for history — never hard-deleted by a
    revoke operation, only by the user or organization itself disappearing.
    """
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('viewer','analyst','organization_admin')", name="ck_memberships_role"),
        CheckConstraint("status IN ('active','revoked')", name="ck_memberships_status"),
        UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("plan IN ('starter','business','enterprise')", name="ck_subscriptions_plan"),
        CheckConstraint("status IN ('pending','active','cancelled','expired')", name="ck_subscriptions_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    access_origin: Mapped[str] = mapped_column(String(40), default="legacy", server_default="legacy", nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    max_analyses: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="subscriptions")


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        # Lets AnalysisDocument carry a composite FK back to (id, organization_id)
        # below, so the database itself refuses a document whose organization_id
        # doesn't match its own analysis's — not just repository discipline.
        UniqueConstraint("id", "organization_id", name="uq_analyses_id_organization_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Owning Organization (B02). user_id above stays the author/owner for
    # privacy purposes during this migration — see Membership and
    # src/web/auth/access_context.py: an analysis is private to its user_id,
    # organization co-membership never grants read access on its own.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Technical id of the async job that produced this row (src/web/jobs.py).
    # Distinct from `id`: job_id is transient/technical, `id` is the durable
    # Postgres identity of the persisted analysis.
    job_id: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True, index=True)
    # Lot 49: the job_id of the analysis this one COMPLETES (never touches it — the parent's own row,
    # snapshots and deliverables stay exactly as they were). NULL for every analysis before this lot and
    # for every analysis that is not itself a completion. UNIQUE: a job may be the parent of at most one
    # revision at a time — a second completion attempt on the same parent is refused (409), never a second
    # silent revision (see src/web/completion_service.py). SQL UNIQUE allows any number of NULLs, so this
    # never constrains ordinary (non-revision) analyses.
    parent_job_id: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True, index=True)
    # Lot 50 bis §3 — additive, deliberately NOT unique (unlike `parent_job_id` above): the job this analysis
    # extends with a fresh, NEW documentary re-analysis (§3's "Ajouter les pièces restantes") — a genuinely
    # new extraction/scoring with the CURRENT active policy/profile/capacity, never the frozen references of a
    # `parent_job_id` completion revision. An analysis can have at most one of the two set, never both (an
    # application-level rule, checked where each is written — not a DB constraint, since NULL/NULL is the
    # overwhelmingly common case and a cross-column CHECK involving two nullable columns would be brittle
    # across SQLite/PostgreSQL). Several analyses may legitimately share the same `origin_job_id` (adding
    # pieces more than once) — this is the reason it is NOT unique.
    origin_job_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    budget: Mapped[str | None] = mapped_column(String(50), nullable=True)
    technologies: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    result_data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    summary_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="analyses")
    organization: Mapped[Organization] = relationship(back_populates="analyses")
    documents: Mapped[list["AnalysisDocument"]] = relationship(back_populates="analysis", cascade="all, delete-orphan")


class AnalysisDocument(Base):
    __tablename__ = "analysis_documents"
    __table_args__ = (
        # Composite FK, not just analysis_id -> analyses.id: this makes a
        # document row whose organization_id disagrees with its own
        # analysis's organization_id impossible to insert at all, regardless
        # of what the repository layer does or doesn't check (ticket B02
        # explicitly asks for a DB-enforced guarantee here, not just code
        # discipline). See tests/test_b02_organizations.py::test_document_organization_must_match_its_analysis.
        ForeignKeyConstraint(
            ["analysis_id", "organization_id"],
            ["analyses.id", "analyses.organization_id"],
            ondelete="CASCADE",
            name="fk_analysis_documents_analysis_org",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    analysis_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Backend-agnostic reference: a local relative path today, an object-store
    # key/URL tomorrow. See src/web/storage/service.py.
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    analysis: Mapped[Analysis] = relationship(back_populates="documents")
    user: Mapped[User] = relationship(back_populates="documents")


class AnalysisJob(Base):
    """B12-T1: a durable, private mirror of the in-process job queue/state
    machine `src/web/jobs.py`/`src/web/job_executor.py` run in memory —
    NOT a replacement for the `analyses` table. `analyses.result_data` is
    NOT NULL and represents a COMPLETED (or terminally-failed-with-a-
    shaped-error) analysis; a merely-queued/running job has no result yet
    and must never be forced into that table just to have SOME row. This
    table exists so that a job accepted but not yet (or never) finished —
    queued, running, or abandoned by a crash — still has a durable trace
    across a process restart, which the in-memory `Job`/`_JOBS` (jobs.py)
    cannot provide by construction.

    `id` deliberately reuses jobs.Job.id's own format (a 12-hex-char
    string minted by uuid.uuid4().hex[:12] in jobs.create_job) rather than
    a UUID primary key like every other table here — this row's identity
    IS that job id, not a new synthetic one; see _uuid_pk()'s docstring
    context in the other tables for the pattern this deliberately departs
    from, and why (jobs.py, this table's repository and job_executor.py
    all key off the exact same string with no translation step).

    `source_label` only (never the raw AO text/file): nothing in this
    ticket requires automatically replaying an interrupted job's LLM call
    — "relance explicite" (ticket) means the user re-submits via
    /api/analyze again, a brand new job with a brand new id. Storing
    enough input to auto-replay would be inventing a requirement (and a
    data-retention liability) this ticket never asked for.

    Abandonment detection (`last_heartbeat_at`, updated periodically by
    src/web/job_executor.py while a claimed job is actually running, NOT
    just `claimed_at` + a fixed timeout): a heartbeat is what lets
    src/web/job_executor.py::reconcile_on_startup tell "the process that
    claimed this crashed" apart from "a DIFFERENT, still-alive process
    legitimately still owns this" — a live worker keeps refreshing its
    OWN timestamp regardless of which process it runs in, so a stale
    heartbeat is a meaningful abandonment signal even in a (not exercised
    here beyond one real single-process-restart test — see
    tests/test_b12_t1_job_executor.py and this ticket's report) multi-
    instance deployment, whereas `claimed_at` alone could never
    distinguish "still running, taking a while" from "the owner is dead"
    without guessing a worst-case job duration. `worker_instance_id` is
    kept alongside for observability/debugging (which process/thread
    claimed this) but is NOT itself what reconciliation keys its decision
    on.

    `error_code` here is queue-level only (e.g. a saturation-time failure
    that still got durably recorded, or `job_interrupted` for an
    abandoned run) — DISTINCT from the rich per-analysis error taxonomy
    `src/web/jobs.py` already reports via its own Job.error_code/`analyses`
    table handling (extraction_failed, rag_failed, scoring_failed, ...);
    this column mirrors whichever code jobs.py's Job ended up with when a
    run's outcome is written back here, purely for this row's own
    durability, but is never treated as a second source of truth for that
    taxonomy.
    """
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','error','interrupted')",
            name="ck_analysis_jobs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    source_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContactRequest(Base):
    __tablename__ = "contact_requests"
    __table_args__ = (
        CheckConstraint("status IN ('new','contacted','closed')", name="ck_contact_requests_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(nullable=True)
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class PasswordResetToken(Base):
    """B21-T1: `token_hash` is the ORIGINAL column — an Argon2 hash of the
    raw reset token, verified pre-B21-T1 by looping over every live,
    unexpired token and running an Argon2 verify() against each (a real
    performance/DoS concern: Argon2 is deliberately slow, and that loop
    grew O(n) with the whole table). Kept, but made nullable and no longer
    populated by new writes — additive/non-destructive rather than
    dropped, in case of rollback/audit; nothing reads it for verification
    any more.

    `token_digest` is the REPLACEMENT: a fast, deterministic HMAC-SHA256
    digest (keyed by config.SESSION_SECRET — see
    src/web/database/repositories/password_reset_tokens.py::compute_digest
    for why), unique + indexed, looked up with a direct
    `WHERE token_digest = :digest` — no loop, no Argon2 involved at all.
    The raw token itself is `secrets.token_urlsafe(32)` (256 bits of real
    CSPRNG entropy) — its own entropy is what makes it unguessable, not a
    slow hash; Argon2 stays exactly where it already is, for
    User.password_hash only. Nullable because pre-migration rows have no
    digest (their raw token was never persisted) and are simply no longer
    matchable — those tokens were already only reachable via the removed
    Argon2 loop, so this is not a new data-loss regression.
    """
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    user: Mapped[User] = relationship(back_populates="reset_tokens")


# ---------------------------------------------------------------------------
# B03 — private knowledge (documentation, RAG index) and private capacity.
#
# Every table here is scoped by BOTH organization_id and owner_user_id, and
# every FK down the chain (Corpus -> Document -> DocumentVersion -> Chunk)
# is a *composite* FK carrying both columns, exactly like Analysis ->
# AnalysisDocument in B02: the database itself refuses a Document whose
# scope disagrees with its Corpus's, a Version whose scope disagrees with
# its Document's, and so on — never just a convention the repository layer
# has to remember. See docs/architecture/B03_PRIVATE_KNOWLEDGE.md.
#
# Two colleagues in the same Organization get two separate rows here (two
# different owner_user_id) — an organization never implies a shared corpus.
# ---------------------------------------------------------------------------

class KnowledgeCorpus(Base):
    """One private corpus per (organization, owner). `generation` is bumped
    on every write that changes what a search should see (new/failed/
    deleted document version) — it is the cache-invalidation and
    cross-instance-change-detection key for the RAG index snapshot (see
    src/rag/private_rag_manager.py): the database is the source of truth
    for "has anything changed", never a single process's memory.
    """
    __tablename__ = "knowledge_corpora"
    __table_args__ = (
        CheckConstraint("status IN ('active')", name="ck_knowledge_corpora_status"),
        UniqueConstraint("organization_id", "owner_user_id", name="uq_knowledge_corpora_org_owner"),
        UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_corpora_scope"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    generation: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    documents: Mapped[list["KnowledgeDocument"]] = relationship(back_populates="corpus", cascade="all, delete-orphan")


class KnowledgeDocument(Base):
    """A logical document (one row survives across versions). `active_version_id`
    points at the currently-searchable KnowledgeDocumentVersion — set only
    after that version's extraction succeeds, so a failed/in-progress
    upload never becomes searchable (ticket B03 section 6: "L'ajout raté ne
    devient pas searchable")."""
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("status IN ('active','deleted')", name="ck_knowledge_documents_status"),
        ForeignKeyConstraint(
            ["corpus_id", "organization_id", "owner_user_id"],
            ["knowledge_corpora.id", "knowledge_corpora.organization_id", "knowledge_corpora.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_documents_corpus_scope",
        ),
        UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_documents_scope"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    corpus_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_document_versions.id", ondelete="SET NULL", use_alter=True, name="fk_knowledge_documents_active_version"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    corpus: Mapped[KnowledgeCorpus] = relationship(back_populates="documents")
    versions: Mapped[list["KnowledgeDocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", foreign_keys="KnowledgeDocumentVersion.document_id"
    )
    active_version: Mapped["KnowledgeDocumentVersion | None"] = relationship(foreign_keys=[active_version_id], post_update=True)


class KnowledgeDocumentVersion(Base):
    """One immutable upload attempt. `extraction_status` tracks the
    ingestion pipeline; only a 'ready' version can ever become a
    KnowledgeDocument.active_version_id. `content_hash` is scoped to the
    same (organization_id, owner_user_id) pair only — see
    src/web/knowledge/documents_service.py for the deliberate non-global
    dedup policy (ticket B03 section 6: no cross-account signal)."""
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        CheckConstraint(
            "extraction_status IN ('received','processing','ready','failed')",
            name="ck_knowledge_document_versions_status",
        ),
        CheckConstraint(
            "content_category_proposed IS NULL OR content_category_proposed IN ('reference','certification','presentation','autre','indetermine')",
            name="ck_knowledge_document_versions_category_proposed",
        ),
        CheckConstraint(
            "content_category_final IS NULL OR content_category_final IN ('reference','certification','presentation','autre','indetermine')",
            name="ck_knowledge_document_versions_category_final",
        ),
        CheckConstraint(
            "classification_source IN ('heuristic','llm','heuristic_llm_unavailable','heuristic_llm_invalid','user','unknown')",
            name="ck_knowledge_document_versions_classification_source",
        ),
        CheckConstraint(
            "embedding_status IN ('not_applicable','pending','ready','failed')",
            name="ck_knowledge_document_versions_embedding_status",
        ),
        ForeignKeyConstraint(
            ["document_id", "organization_id", "owner_user_id"],
            ["knowledge_documents.id", "knowledge_documents.organization_id", "knowledge_documents.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_document_versions_document_scope",
        ),
        UniqueConstraint("document_id", "version_number", name="uq_knowledge_document_versions_number"),
        UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_document_versions_scope"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type_detected: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, default="received", index=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extractor_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # Lot 50 bis §2 — additive: what KIND of business content this version appears to be (reference,
    # certification, presentation, autre, indetermine) — a PROPOSAL, never a verified fact ("un document classé
    # « certification » n'est pas une certification vérifiée", ticket verbatim). `content_category_final` is
    # what the account confirmed/corrected (falls back to the proposal); `classified_by_user_id`/`classified_at`
    # are set ONLY when a human corrected it. Never updates the account's profile/scoring by itself.
    content_category_proposed: Mapped[str | None] = mapped_column(String(20), nullable=True)
    content_category_final: Mapped[str | None] = mapped_column(String(20), nullable=True)
    classification_source: Mapped[str] = mapped_column(String(30), nullable=False, default="heuristic")
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lot 51 — hybrid RAG: readiness of this version's VECTOR passages,
    # entirely separate from `extraction_status` above (which governs the
    # lexical/TF-IDF path, unchanged). 'not_applicable' on every SQLite
    # deployment and on any PostgreSQL deployment with hybrid mode not
    # explicitly enabled (config.RAG_HYBRID_MODE_ENABLED) — never silently
    # 'ready' by default. 'pending' before any embedding attempt (a version
    # that predates hybrid mode being turned on, or a config/model change
    # making its passages stale); 'ready' only once EVERY passage of this
    # version has a valid vector (never a partial index, see
    # src/rag/hybrid_index.py); 'failed' on any provider/dimension error
    # (degrades to lexical-only for this document, never zero results for
    # the whole corpus). `embedding_model_id/_revision/_dimension` record
    # EXACTLY what produced the current passages — a later config change
    # makes an already-'ready' version stale (detected by comparing these
    # to the current config, see hybrid_index.needs_reindex), never mixed
    # silently with vectors from a different model/dimension.
    embedding_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_applicable")
    embedding_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_model_revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(nullable=True)
    embedding_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="versions", foreign_keys=[document_id])
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="version", cascade="all, delete-orphan")


class KnowledgeChunk(Base):
    """One extracted, searchable passage. Kept in a table (not a filesystem
    manifest): at the dev-scale limits this ticket sets (100 active
    documents/corpus, bounded pages/paragraphs — src/core/config.py), a
    table lets every authorization check the RAG search path needs
    (scope, active version, document status) be a single indexed join,
    with the same composite-FK guarantee as the rest of this chain — a
    manifest file would need to re-implement that consistency check
    itself. See docs/architecture/B03_PRIVATE_KNOWLEDGE.md for the
    justification and the migration path to a manifest/columnar store if
    corpus sizes ever outgrow this."""
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_version_id", "organization_id", "owner_user_id"],
            ["knowledge_document_versions.id", "knowledge_document_versions.organization_id", "knowledge_document_versions.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_chunks_version_scope",
        ),
        # Lot 51 (additive, migration 0015): lets KnowledgePassage reference
        # a chunk with the SAME composite-FK scope guarantee (id + org +
        # owner together) already used everywhere else in this chain — a
        # passage can never be attached to a chunk belonging to a different
        # account by construction.
        UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_chunks_scope"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(nullable=False)
    page_number: Mapped[int | None] = mapped_column(nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    version: Mapped[KnowledgeDocumentVersion] = relationship(back_populates="chunks")
    passages: Mapped[list["KnowledgePassage"]] = relationship(back_populates="chunk", cascade="all, delete-orphan")


class KnowledgePassage(Base):
    """Lot 51 — one embedding-sized WINDOW of a KnowledgeChunk's content (the
    existing paragraph/table-level "section"). A short chunk yields exactly
    one passage covering it whole; a long one yields several overlapping
    windows (src/rag/chunking.py::window_passages, deterministic, pure).

    Portable across dialects — this table exists and is populated the SAME
    way on SQLite and PostgreSQL. Only the VECTOR itself is dialect-specific:
    the `embedding` column is added by migration 0015 ONLY on PostgreSQL
    (`op.execute` guarded by `bind.dialect.name`), is never declared as an
    ORM-mapped attribute here, and is read/written exclusively through raw,
    dialect-checked SQL in src/rag/hybrid_index.py / hybrid_search.py — so a
    SQLite deployment never even imports pgvector. On SQLite this table
    simply stays empty (hybrid mode is structurally impossible there); a
    PostgreSQL deployment with RAG_HYBRID_MODE_ENABLED=false ALSO leaves it
    empty (explicit opt-in, never a silent side effect of switching the
    database backend).

    `content == chunk.content[start_char:end_char]` holds by construction
    and is stored redundantly here (never re-sliced at query time, so a
    later edit to windowing logic cannot retroactively change what an
    already-computed vector was built from). `embedding_model_id/_revision/
    _dimension` are copied from the version at the moment THIS passage's
    vector was computed — a stale reindex-in-progress can never mix vectors
    from two different model configs in the same query (filtered on these,
    not merely trusted from the parent version's own status flag).
    """
    __tablename__ = "knowledge_passages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id", "organization_id", "owner_user_id"],
            ["knowledge_chunks.id", "knowledge_chunks.organization_id", "knowledge_chunks.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_passages_chunk_scope",
        ),
        CheckConstraint("end_char > start_char", name="ck_knowledge_passages_span"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    document_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    start_char: Mapped[int] = mapped_column(nullable=False)
    end_char: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    page_number: Mapped[int | None] = mapped_column(nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_model_revision: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    chunk: Mapped[KnowledgeChunk] = relationship(back_populates="passages")


class PrivateCapacityPlan(Base):
    """One private capacity plan per (organization, owner) — replaces the
    global data/reg_docs/ressources/capacite_charge_planification.md file
    as the source of truth (that file remains in data/, untouched, but no
    code reads it any more since lot 43 — see docs/architecture/B03_PRIVATE_KNOWLEDGE.md).
    status='unconfigured' (the default, never written by a real save) is
    what lets /api/analyze refuse with CAPACITY_NOT_CONFIGURED instead of
    silently scoring against demo defaults."""
    __tablename__ = "private_capacity_plans"
    __table_args__ = (
        CheckConstraint("status IN ('configured','unconfigured')", name="ck_private_capacity_plans_status"),
        UniqueConstraint("organization_id", "owner_user_id", name="uq_private_capacity_plans_org_owner"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unconfigured")
    charge_globale_pct: Mapped[int] = mapped_column(nullable=False, default=0)
    nombre_projets_en_cours: Mapped[int] = mapped_column(nullable=False, default=0)
    projets_en_cours: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    capacites_par_pole: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # B08-T1: replaces the hardcoded "remaining >= 10" threshold that used
    # to decide `equipe_disponible` in src/agents/capacity_analyzer.py —
    # every business threshold kept by that ticket is an explicit private
    # parameter, never a magic number in code. Default 10 only for a NEW
    # row (never silently back-filled onto an existing account's plan as
    # if they had chosen it — see migration 0006).
    disponibilite_minimum_pct: Mapped[int] = mapped_column(nullable=False, default=10)
    version: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class ProviderProfile(Base):
    """B06-T1: one private ESN/prestataire profile per (organization, owner)
    — the ESN's OWN declared business identity, competencies and claimed
    certifications. Distinct from src/core/models.py's CompanyProfile,
    which describes the AO's BUYER (a third party being analyzed) and is
    never persisted per-account. Also distinct from PrivateCapacityPlan
    (team load/availability) — see ticket B06-T1 section 3 for why these
    stay three separate objects rather than one blob.

    `competences` (list[str], lowercase) replaces ScoringEngine.mastered
    for an account with an active ScoringPolicy — never inferred from an
    uploaded knowledge document, only from this explicit form. Same for
    `certifications` (list of {"nom","statut","preuve_reference"}) versus
    ScoringEngine.certs_ok — a certification merely mentioned in a document
    is never auto-granted here (see docs/api/B06_SCORING_CONFIG_CONTRACT.md
    section on certifications). `statut` is 'declaree' (self-declared, no
    supporting file referenced) or 'verifiee' (a preuve_reference is set) —
    stored for future auditability; the current engine injection (B06-T1)
    treats both as "held" for the missing-certification blocker, since a
    certification the owner explicitly typed into their own private form is
    already a stronger signal than free text in a shared corpus document —
    an open point, not silently claimed as fully resolved (see report)."""
    __tablename__ = "provider_profiles"
    __table_args__ = (
        CheckConstraint("status IN ('incomplete','complete')", name="ck_provider_profiles_status"),
        UniqueConstraint("organization_id", "owner_user_id", name="uq_provider_profiles_org_owner"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="incomplete")
    raison_sociale: Mapped[str | None] = mapped_column(String(255), nullable=True)
    effectif: Mapped[str | None] = mapped_column(String(100), nullable=True)
    competences: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    certifications: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # B07-T1: the external company-lookup provider (Pappers) is optional
    # and OFF by default — a server-wide API key configured in .env is
    # never, by itself, an authorization to call it for a given account.
    # Only an explicit True here, set by the owner through this same
    # profile form, turns it on; False (the default for every existing and
    # every new row) means src/agents/company_enrichment.py makes ZERO
    # external calls for this account, whatever the server-wide key state.
    external_enrichment_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    # B06-T5: a private, sector-neutral catalogue of declared business
    # facts — {key: {"key","label","type","unit","value"}} — additive to
    # the fixed columns above, never a replacement of `competences`/
    # `certifications` (those stay the IT-specific fields ScoringEngine's
    # pre-existing 12 criteria read). See src/agents/business_facts.py for
    # the fixed catalogue of types/operators this data is validated
    # against, and ScoringPolicy.custom_criteria below for the criteria
    # built on top of these facts. Empty dict ('{}') on every pre-existing
    # row — no account is silently given facts it never declared.
    business_facts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class ScoringPolicy(Base):
    """B06-T1: a versioned, private scoring configuration — organization +
    owner scoped, never a process-wide default. Multiple rows per owner
    (one per version), unlike PrivateCapacityPlan's single mutable row,
    because activation must be atomic and a past analysis must keep
    referencing the exact version that was active when it ran (ticket
    section 5) — an in-place-mutated single row could not offer that.

    Exactly one row per (organization_id, owner_user_id) may have
    status='active' at a time — enforced by the DB itself via the partial
    unique index below (uq_scoring_policies_one_active), not only by
    application logic, so a genuine race between two activation requests
    fails one of them with an integrity error rather than silently leaving
    two policies active. 'draft' rows are freely mutable in place; 'active'
    and 'archived' rows are treated as immutable by every repository
    function (src/web/database/repositories/scoring_policy.py) — any
    edit after activation creates a new draft version instead.

    weights/threshold_go/threshold_sous_reserve mirror
    ScoringEngine.weights/SCORING_THRESHOLD_GO/SCORING_THRESHOLD_SOUS_RESERVE
    (src/agents/scoring_engine.py) exactly — see ScoringPolicySnapshot in
    that module for how a row here is turned into engine input. The engine
    formula itself is unchanged; only where these numbers come from."""
    __tablename__ = "scoring_policies"
    __table_args__ = (
        CheckConstraint("status IN ('draft','active','archived')", name="ck_scoring_policies_status"),
        UniqueConstraint("organization_id", "owner_user_id", "version", name="uq_scoring_policies_org_owner_version"),
        Index(
            "uq_scoring_policies_one_active", "organization_id", "owner_user_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    weights: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    threshold_go: Mapped[float | None] = mapped_column(nullable=True)
    threshold_sous_reserve: Mapped[float | None] = mapped_column(nullable=True)
    # B06-T4: the remaining scoring business values that were still
    # hardcoded in src/agents/scoring_engine.py's active code path —
    # budget_minimum_eur (blocking threshold), max_charge_pct (blocking
    # threshold), max_unmastered_technologies (blocking threshold),
    # certification_penalty_score (the score applied when a mandatory
    # certification is missing). A flexible JSON dict, like `weights`
    # above — an absent key means "not configured", NEVER silently
    # defaulted to the old hardcoded constant for a real account (see
    # ScoringEngine.score's handling of a missing key: the affected
    # blocker is not evaluated at all, and the overall result is marked
    # incomplete — src/agents/scoring_engine.py owns the exact contract).
    business_rules: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # B06-T5: additive, sector-neutral criteria built on the owner's
    # ProviderProfile.business_facts — a list of {"id","label","fact_key",
    # "operator","comparison","weight","blocking","pass_score",
    # "fail_score"}. UNCHANGED weights/business_rules above still drive the
    # fixed 12 IT-specific criteria; these are ADDITIONAL criteria in the
    # SAME 100-point weight budget (see src/agents/scoring_policy_
    # validation.py::validate_weights' custom_criteria_weight_total and
    # src/agents/scoring_engine.py::ScoringEngine.score). Empty list ('[]')
    # on every pre-existing row — no account gets a criterion it never
    # configured, and an account that configures none of this keeps the
    # exact pre-existing 12-criteria formula, byte for byte.
    custom_criteria: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # Lot 44 (migration 0010): the EXPLICIT criteria the engine evaluates and
    # their schema version (distinct from the business `version` above), the
    # policy-level settings, and the representation the policy is authored in.
    # origin='legacy': `criteria` is the materialized historical rules and the
    # three legacy columns above stay authoritative and consistent (a policy
    # migrated from before lot 44, or saved through the legacy-shaped API);
    # origin='user': `criteria` was authored in the new format and the legacy
    # columns are empty. criteria_version 0 only exists between the
    # `add_column` and the back-fill of migration 0010.
    criteria_version: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("0"))
    criteria: Mapped[list] = mapped_column(JSONType, nullable=False, default=list, server_default=text("'[]'"))
    settings: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict, server_default=text("'{}'"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default=text("'user'"))
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AoDossier(Base):
    """Lot 47 bis: ONE validated tender dossier (RC, CCTP, CCAP, acte d'engagement, annexes) submitted
    for ONE analysis. Distinct from `knowledge_*` (the account's own professional references, the RAG
    corpus) and from `analysis_documents` (generated deliverables): a dossier piece is INPUT to an
    analysis and is never indexed, never searched and never counted as a reference.

    Scoped like every private table here (organization + owner). `job_id` names the LATEST job that
    analyses this dossier (an interrupted job can be resumed on the same validated dossier; the earlier
    job id then stays an interrupted job). `status`: 'staging' (lot 50 — received and vetted, awaiting the
    user's confirmation of the final admitted set; no job yet, expires), 'validated' (confirmed, all
    admitted pieces accepted, no job yet) or 'submitted' (a job was created for it).

    Lot 50 (§3/§4 admission): `staging_expires_at` is set only while `status='staging'` — a confirm attempt
    after this instant is refused (410), never silently accepted. `categories_missing`/`scope_limited`
    freeze, at CONFIRMATION time, which of the 4 guided categories (RC/CCTP/CCAP/acte d'engagement) this
    dossier does NOT cover — never recomputed later from the current document set (a manifest is a record
    of what was actually confirmed, not a live view). `confirmed_by_user_id`/`confirmed_at` name who
    confirmed the final admitted set and when."""
    __tablename__ = "ao_dossiers"
    __table_args__ = (
        CheckConstraint("status IN ('staging','validated','submitted')", name="ck_ao_dossiers_status"),
        UniqueConstraint("id", "organization_id", "user_id", name="uq_ao_dossiers_scope"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="validated")
    total_bytes: Mapped[int] = mapped_column(nullable=False)
    piece_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # Lot 50 — additive; NULL for every dossier created before this lot and for one that never went through
    # staging (none currently do, but a future direct-commit path could still choose to skip it).
    staging_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    categories_missing: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    scope_limited: Mapped[bool] = mapped_column(nullable=False, default=False)
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lot 50 bis §3 — set ONLY on a staging dossier created by "Ajouter les pièces restantes" (a NEW
    # documentary re-analysis of an existing job's dossier, distinct from a NEW plain dossier submission):
    # the job this one extends. NULL for every ordinary dossier (including every one before this lot).
    # Propagated to `Analysis.origin_job_id` (never `parent_job_id`) once this staging dossier is confirmed.
    origin_job_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    pieces: Mapped[list["AoDossierPiece"]] = relationship(
        back_populates="dossier", cascade="all, delete-orphan", order_by="AoDossierPiece.position",
    )


class AoDossierPiece(Base):
    """One piece of a dossier: a server-minted id, the category the USER chose (never deduced from the
    file name, never a legal priority), a display-safe name, the validated format, the byte size and the
    SHA-256 of the received bytes. `storage_key` (the private original) and `text_storage_key` (the
    extracted chunks, with page numbers when the format has them) are paths relative to the storage
    root — internal, never returned to a client. A byte-identical repeat inside the same dossier keeps
    its row but points at the first one through `duplicate_of_piece_id` and is not read twice.

    Lot 50 (§1-§3 admission manifest): `category` stays what the user's UPLOAD SLOT declared (now including
    `'autre'`, the free-form slot — never inferred from the file name). `category_proposed` is the
    classifier agent's suggestion (nullable — `None` when no suggestion could be made). `category_final` is
    the category actually confirmed for this analysis (falls back to `category` for pre-lot-50 rows and for
    any row the user made no change to at confirmation) — this is the ONE category downstream code
    (consolidation, display, PDF/DOCX) must read, never `category` alone, so a corrected classification is
    honoured. `security_state`/`moderation_verdict` are the two independent agent verdicts of §2 (never a
    scoring decision); `admitted` is the FINAL, server-enforced inclusion decision at confirmation (a
    security 'blocked' piece can never be `admitted=True`); `exclusion_reason` is set only when
    `admitted=False`. `user_link_note` is the user's OWN declared justification for an 'incertain'/
    'hors_sujet' piece they chose to confirm as relevant anyway — a declaration, never upgraded to a
    verified fact."""
    __tablename__ = "ao_dossier_pieces"
    __table_args__ = (
        CheckConstraint("category IN ('rc','cctp','ccap','acte_engagement','annexe','autre')", name="ck_ao_dossier_pieces_category"),
        CheckConstraint("category_proposed IS NULL OR category_proposed IN ('rc','cctp','ccap','acte_engagement','annexe','autre')", name="ck_ao_dossier_pieces_category_proposed"),
        CheckConstraint("category_final IS NULL OR category_final IN ('rc','cctp','ccap','acte_engagement','annexe','autre')", name="ck_ao_dossier_pieces_category_final"),
        CheckConstraint("security_state IN ('authorized','blocked','to_verify')", name="ck_ao_dossier_pieces_security_state"),
        CheckConstraint("moderation_verdict IS NULL OR moderation_verdict IN ('lie','incertain','hors_sujet')", name="ck_ao_dossier_pieces_moderation_verdict"),
        CheckConstraint(
            "classification_source IN ('heuristic','llm','heuristic_llm_unavailable','heuristic_llm_invalid')",
            name="ck_ao_dossier_pieces_classification_source",
        ),
        CheckConstraint(
            "moderation_source IN ('heuristic','llm','heuristic_llm_unavailable','heuristic_llm_invalid')",
            name="ck_ao_dossier_pieces_moderation_source",
        ),
        CheckConstraint(
            "security_review_source IN ('heuristic','llm','heuristic_llm_unavailable','heuristic_llm_invalid')",
            name="ck_ao_dossier_pieces_security_review_source",
        ),
        ForeignKeyConstraint(
            ["dossier_id", "organization_id", "user_id"],
            ["ao_dossiers.id", "ao_dossiers.organization_id", "ao_dossiers.user_id"],
            ondelete="CASCADE", name="fk_ao_dossier_pieces_scope",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    position: Mapped[int] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    text_storage_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    char_count: Mapped[int] = mapped_column(nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(nullable=False, default=0)
    duplicate_of_piece_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # Lot 50 — additive manifest columns (see class docstring above).
    category_proposed: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category_final: Mapped[str | None] = mapped_column(String(20), nullable=True)
    security_state: Mapped[str] = mapped_column(String(20), nullable=False, default="authorized")
    security_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    security_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderation_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    moderation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    admitted: Mapped[bool] = mapped_column(nullable=False, default=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_link_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lot 50 bis §1 — additive: which judgment path actually produced `category_proposed`/`moderation_verdict`/
    # `security_reason` — "heuristic" (default, matches every pre-lot-50-bis row exactly, no LLM call ever
    # attempted for it), "llm" (a real, citation-verified answer from the account's configured provider), or
    # "heuristic_llm_unavailable"/"heuristic_llm_invalid" (a call was attempted but fell back — never silently
    # presented as an LLM validation that did not actually happen). See `src/agents/document_llm_support.py`.
    classification_source: Mapped[str] = mapped_column(String(30), nullable=False, default="heuristic")
    moderation_source: Mapped[str] = mapped_column(String(30), nullable=False, default="heuristic")
    security_review_source: Mapped[str] = mapped_column(String(30), nullable=False, default="heuristic")

    dossier: Mapped[AoDossier] = relationship(back_populates="pieces")


class AoDossierJobLink(Base):
    """Lot 49 (fixes a lot 47 bis reserve): EVERY job that was ever run against a dossier — the original,
    every `resume` attempt, and every completion revision — keeps its own row here, so
    `GET /api/analyze/{job_id}/dossier` never loses access just because `AoDossier.job_id` (which still
    names only the MOST RECENT job, for display/status purposes) was reassigned. `job_id` is UNIQUE: one
    job always resolves to exactly one dossier. Additive; `AoDossier.job_id` itself is untouched."""
    __tablename__ = "ao_dossier_job_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dossier_id", "organization_id", "user_id"],
            ["ao_dossiers.id", "ao_dossiers.organization_id", "ao_dossiers.user_id"],
            ondelete="CASCADE", name="fk_ao_dossier_job_links_scope",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AnalysisComplement(Base):
    """Lot 49 — one piece of information a user DECLARED to complete an INCOMPLET analysis, recorded on the
    resulting revision (`job_id` = the NEW job's id, never the parent's). Distinct from every extracted/
    observed value on `AOContext`/`ScoringResult`: a complement is a user's own statement, never presented
    as verified or as an extraction outcome (`docs/api/B22_T2... ` — see docs/api/LOT_49_COMPLETION_CONTRACT.md).
    `subject`: 'ao' (this analysis's own document data — a budget, a certification list…), 'acheteur' (the
    buyer's profile — e.g. its sector — never re-triggers an external lookup), or 'prestataire' (the
    account's OWN profile — recorded here for traceability of what changed THIS revision, alongside the
    real permanent write to `provider_profiles` that `confirm_profile_write` authorized).
    Never mutated after creation; a later revision gets its own new rows."""
    __tablename__ = "analysis_complements"
    __table_args__ = (
        CheckConstraint("subject IN ('ao','acheteur','prestataire')", name="ck_analysis_complements_subject"),
        CheckConstraint("origin IN ('declared_user','llm_sourced')", name="ck_analysis_complements_origin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    need_id: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str] = mapped_column(String(20), nullable=False)
    field_key: Mapped[str] = mapped_column(String(80), nullable=False)
    field_label: Mapped[str] = mapped_column(String(200), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONType, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Lot 52 (additive, migration 0016): 'declared_user' (default — a plain typed value, the entire lot
    # 49/49 bis contract unchanged) or 'llm_sourced' (the user accepted, UNMODIFIED, a citation-verified
    # proposal from a search of their own documents — src/agents/fact_search.py). A value the user
    # corrected after seeing a proposal is 'declared_user': its citation no longer supports it, so it is
    # never recorded as sourced. `source_json` is the frozen provenance for an 'llm_sourced' row only
    # (document_version_id/chunk_id or dossier piece_id, source label, citation, offsets, offset frame,
    # model id, prompt version, extraction timestamp) — always None for 'declared_user'.
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="declared_user")
    source_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class PlatformOperator(Base):
    __tablename__ = "platform_operators"
    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    credential_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AccessAudit(Base):
    __tablename__ = "access_audit"
    id: Mapped[uuid.UUID] = _uuid_pk()
    operator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_operators.id", ondelete="RESTRICT"), nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
