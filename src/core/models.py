from __future__ import annotations
from typing import Any, List, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import date

from src.core.rag_evidence_validation import validate_similarity_score


class ExtractedFact(BaseModel):
    """B05-T3 — one private, sector-neutral business fact extracted from an
    AO, for a fact_key the account's active ScoringPolicy actually asked
    for (via ScoringPolicy.custom_criteria — see src.agents.business_facts
    for the fixed catalogue of types this belongs to). Distinct from every
    fixed AOContext field below: this is generic, per-account-configured
    data, never IT-specific.

    `status` is one of "found" (a usable value was recognized — the AO
    genuinely stated it), "absent" (nothing recognized, never guessed at)
    or "ambiguous" (multiple/conflicting mentions, not reliably
    resolvable) — `value` is None unless status == "found". `provenance`
    mirrors AOContext.field_provenance's own codes ("llm"/"fallback"/
    "rejected"/"absent") so the two extraction mechanisms stay
    consistent."""
    value: Optional[Any] = None
    unit: Optional[str] = None
    status: str = "absent"
    provenance: str = "absent"
    # Lot 41: why a fact is "ambiguous" (e.g. "partial_list_possible",
    # "polarity_unclear_or_contradictory", "conflicting_values") — a safe
    # code, surfaced in the INCOMPLET justification. None for any fact
    # persisted before this field existed.
    reason: Optional[str] = None


class CertificationMention(BaseModel):
    """B17-T1 (DEFECT confirmed): one certification NAME found in one
    CLAUSE of the AO text, with a verdict scoped to THAT clause alone —
    see src/agents/certification_scope.py for the analysis this records.
    The audited bug: a negation could apply to an ENTIRE LINE, so
    "ISO 27001 obligatoire ; Qualiopi non obligatoire" wrongly cancelled
    ISO 27001 too. Clauses (split on ';'/'mais', never on every comma —
    a comma-coordinated list like "ISO 27001, HDS et SecNumCloud
    obligatoires" shares ONE verb) are now independently scoped.

    Multiple mentions of the SAME name are never merged into one entry —
    each occurrence keeps its own clause and verdict; see
    AOContext.certification_contradictions for when they disagree."""
    name: str
    verdict: str  # "obligatoire" | "non_obligatoire" | "ambigu"
    clause: str


class AOContext(BaseModel):
    titre: str = ""
    client: str = ""
    secteur: str = ""
    budget_estime: Optional[float] = None
    # B05-T2 (DEFECT confirmed): the extraction prompt has always told the
    # model "null si absent" for this field, but this was `str = ""` — a
    # null response made the WHOLE `AOContext(**data)` construction raise,
    # discarding every other already-valid field and forcing the full
    # local-regex fallback. An absent deadline is a normal, common case
    # (many AOs have no fixed date), never an error. Every consumer already
    # does `ao.deadline_reponse or "..."` (src/agents/scoring_engine.py,
    # src/livrables/document_generator.py) or
    # `if ao.deadline_reponse:` — None is falsy exactly like "" was, so
    # this widening needs no consumer change.
    deadline_reponse: Optional[str] = None
    duree_projet_mois: Optional[int] = None
    technologies_demandees: List[str] = Field(default_factory=list)
    competences_requises: List[str] = Field(default_factory=list)
    questions_client: List[str] = Field(default_factory=list)
    livrables: List[str] = Field(default_factory=list)
    contraintes: List[str] = Field(default_factory=list)
    certifications_obligatoires: List[str] = Field(default_factory=list)
    texte_source: str = ""
    # B05-T2: per-field provenance and an overall status/reason, following
    # the same sibling status/reason convention already established for
    # scoring (enrichment_status/data_integrity/rag_selection_status,
    # docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md) — deliberately a
    # SEPARATE set of fields, never reusing/overloading enrichment_status
    # to describe extraction (ticket: "ne pas modifier enrichment_status
    # du scoring pour décrire l'extraction").
    #
    # `field_provenance[field_name]` is one of:
    #   "llm"      — a validated, well-formed value came directly from the
    #                model (an explicit `[]`/`0`/empty answer counts as
    #                "llm" too — a deliberate "nothing found" is not the
    #                same as the field being absent from the response).
    #   "fallback" — the model's value for this field was absent or
    #                invalid, but a local regex/heuristic found something
    #                usable in the raw text.
    #   "rejected" — the model provided a value for this field but it
    #                failed validation (wrong type, NaN, boolean where a
    #                number was expected, ...), AND no local fallback
    #                could recover anything either — the field keeps its
    #                normal empty/None default, but this code makes that
    #                distinguishable from a field the model simply never
    #                addressed.
    #   "absent"   — neither the model nor a local fallback found
    #                anything for this field — a normal, unremarkable
    #                case for most AOs (see B05-T2 "absence de valeur !=
    #                erreur").
    # Missing from the dict entirely (pre-B05-T2 persisted AOContext, or
    # any field this ticket does not track) means "unknown", never
    # fabricated as one of the four codes above.
    field_provenance: Dict[str, str] = Field(default_factory=dict)
    # Fields where a validated LLM value was actually used AS-IS, but an
    # independent local detector (an existing, deliberately narrow
    # regex/vocabulary check — src/agents/ao_extractor.py::
    # _extract_certifications / _extract_technologies) found a
    # meaningfully different answer for the SAME field. Signaled, never
    # silently resolved in either direction — resolving it well is B17-T1's
    # job for certifications specifically, not this ticket's.
    extraction_conflicts: List[str] = Field(default_factory=list)
    # Coarse status of the extraction ATTEMPT as a whole (distinct from
    # field_provenance, which is per field): "llm_full" (every field came
    # from a validated LLM value), "llm_partial" (a mix of "llm" and
    # fallback/rejected/absent fields — see `extraction_reason=
    # "some_fields_rejected"`, reusing the exact reason string
    # enrichment_status already uses for its own analogous "partial"
    # case), "fallback_local" (LLM disabled, no usable response, or a
    # non-object response — every field is fallback/absent, `extraction_
    # reason` names which of the three applied), "unknown" (this
    # AOContext was persisted before this ticket, no metadata available —
    # never fabricated after the fact).
    extraction_status: str = "unknown"
    extraction_reason: Optional[str] = None
    # B17-T1 (DEFECT confirmed, complement of B05-T2): the LOCAL, clause-
    # scoped certification analysis (src/agents/certification_scope.py) is
    # always computed and recorded here as a diagnostic overlay,
    # regardless of whether `certifications_obligatoires` itself came from
    # a validated LLM value or the local fallback — it NEVER overwrites
    # `certifications_obligatoires` (ticket: "ne pas écraser un champ LLM
    # valable avec un repli plus pauvre"), it only makes the underlying
    # per-clause evidence inspectable. `certification_mentions` preserves
    # every occurrence with its own exact source clause and verdict
    # ("obligatoire" | "non_obligatoire" | "ambigu") — never merged, never
    # deduplicated by name. `certification_contradictions` names any
    # certification whose mentions disagree (one clause says obligatoire,
    # another says otherwise, or the scope is genuinely undetermined) —
    # such a name is deliberately EXCLUDED from `certifications_
    # obligatoires` (no fabricated resolution), never silently dropped
    # from view.
    certification_mentions: List[CertificationMention] = Field(default_factory=list)
    certification_contradictions: List[str] = Field(default_factory=list)
    # B05-T3: private, sector-neutral facts the active ScoringPolicy's
    # custom_criteria actually requested (src.agents.ao_extractor.
    # AOExtractor.extract's `requested_facts` parameter) — keyed by the
    # SAME fact_key the account declared in ProviderProfile.business_facts.
    # Empty for every AO analyzed with no custom criterion configured, and
    # for every AOContext built before this ticket — never fabricated
    # after the fact. See src.core.models.ExtractedFact and
    # src.agents.business_facts for the framework this feeds.
    extracted_facts: Dict[str, ExtractedFact] = Field(default_factory=dict)
    # Lot 47 bis (additive; None on every analysis of a single text/file and on every result stored before it):
    # the AO DOSSIER this analysis was made from — the pieces (id, category, display name, format, size, hash,
    # pages), the SOURCED observations (piece id + passage for each value read), the conflicts kept as
    # ambiguous (`field_provenance[field] == "conflict"`, value None) and the documented limits. Never a path.
    dossier: Optional[Dict[str, Any]] = None

class CompanyProfile(BaseModel):
    raison_sociale: str = ""
    siret: str = ""
    effectif: str = "Non renseigné"
    ca: str = "Non renseigné"
    ville: str = "Non renseigné"
    secteur: str = "Non renseigné"
    anciennete: str = "Non renseigné"
    # B07-T1 (DEFECT confirmed): "Moyenne" looked like a genuinely assessed,
    # plausible middle-ground rating — exactly the kind of fabricated-
    # looking-but-unlabeled business fact this ticket removes. Aligned with
    # every sibling field's own honest "Non renseigné" default. No scoring
    # behavior changes: src/agents/scoring_engine.py's solidity check only
    # branches on "Bonne"/"A verifier" for its favorable case — both
    # "Moyenne" and "Non renseigné" already fell into the same neutral
    # else-branch, so this is a label-only fix.
    solidite_financiere: str = "Non renseigné"
    source: str = "mock"

class RAGEvidence(BaseModel):
    # B18-T1 (DEFECT-B04-04): validate_assignment=True closes the "built
    # valid, then mutated invalid" path — a validator only at construction
    # time would miss `evidence.score = -5` on an already-created instance.
    model_config = ConfigDict(validate_assignment=True)

    query: str
    source: str
    score: float
    content: str
    # B18-T4 (DEFECT F09/F12/E-5): other sources found to have EXACTLY the
    # same indexed content as this evidence (server-computed fingerprint,
    # src/core/reference_identity.py) — populated only when a duplicate was
    # actually found and merged into this representative. Traceability
    # only: the original files/chunks are never deleted, never have their
    # ownership changed; this just records that they were not double-
    # counted in the "Références similaires" calculation.
    duplicate_sources: List[str] = Field(default_factory=list)
    # B18-T5 (DEFECT F11/F12/E-6): provenance for `content` as an EXACT
    # slice of the canonical (full, untruncated) text actually indexed —
    # never invented. `document_version_id` is the private/SaaS path's own
    # KnowledgeDocumentVersion id (src/rag/private_rag_manager.py) — always
    # None for an evidence that has no such row at all (never fabricated). `content_fingerprint` reuses the T4 identity hash
    # of the FULL canonical text (src/core/reference_identity.py), computed
    # once before any passage was located. `start_char`/`end_char` are a
    # [start, end) interval in Unicode codepoints of that canonical text —
    # `content == canonical_text[start_char:end_char]` always holds by
    # construction (src/rag/passage_location.py never copies/rewrites).
    # All four are None for any evidence built before this ticket, or by
    # a path that genuinely has no such data (e.g. a test's synthetic
    # RAGEvidence) — an explicit, honest "provenance unavailable", never a
    # fabricated value.
    document_version_id: Optional[str] = None
    content_fingerprint: Optional[str] = None
    start_char: Optional[int] = None
    end_char: Optional[int] = None
    # Lot 51 — the specific KnowledgeChunk this evidence represents (distinct
    # from document_version_id: one version can have many chunks). None for
    # any evidence built before this lot or by a path with no such row
    # (e.g. a synthetic test evidence) — never fabricated. This is the
    # identity src/rag/hybrid_search.py fuses lexical and vector candidates
    # on, so a chunk found by BOTH signals is never double-counted as two
    # separate pieces of evidence.
    chunk_id: Optional[str] = None

    @field_validator("score", mode="before")
    @classmethod
    def _validate_score(cls, value):
        # mode="before" runs on the RAW input, ahead of Pydantic's own
        # float coercion — required to actually reject a bool (which would
        # otherwise silently coerce to 0.0/1.0) or a numeric string.
        return validate_similarity_score(value)

    @model_validator(mode="after")
    def _check_position_matches_content_length(self) -> "RAGEvidence":
        # A self-contained sanity check (this model alone cannot verify
        # `content == canonical_text[start_char:end_char]` — that needs the
        # canonical text, asserted instead by tests against a real
        # corpus/producer) — but a well-formed [start, end) interval must
        # at least have the same length as `content` itself.
        if self.start_char is not None and self.end_char is not None:
            if self.end_char - self.start_char != len(self.content):
                raise ValueError("start_char/end_char span must match len(content) exactly")
            if self.start_char < 0 or self.end_char < self.start_char:
                raise ValueError("start_char/end_char must form a valid, non-negative [start, end) interval")
        return self

class CapacityResult(BaseModel):
    charge_actuelle_pct: int
    capacite_restante_pct: int
    equipe_disponible: bool
    commentaire: str

class CriterionScore(BaseModel):
    nom: str
    poids: float
    score: float
    justification: str
    # Lot 44 (additive; a result stored before it has none of these, so every
    # default is None — never fabricated for a historical result):
    # `etat`: "evalue" | "hypothese" (a note chosen by the policy for an
    # absent datum, visible as an hypothesis) | "manquant" (not evaluated,
    # score 0 in the provisional sum) | "non_applicable" (excluded, with a
    # `motif`). `bloquant` = this criterion fired a blocker.
    etat: Optional[str] = None
    evaluateur: Optional[str] = None
    critere_id: Optional[str] = None
    bloquant: Optional[bool] = None
    motif: Optional[str] = None

class ScoringResult(BaseModel):
    decision: str
    score_global: float
    criteres: List[CriterionScore]
    criteres_bloquants: List[str] = Field(default_factory=list)
    forces: List[str] = Field(default_factory=list)
    faiblesses: List[str] = Field(default_factory=list)
    risques: List[str] = Field(default_factory=list)
    recommandations: List[str] = Field(default_factory=list)
    evidence_pack: List[RAGEvidence] = Field(default_factory=list)
    company_profile: Optional[CompanyProfile] = None
    capacity: Optional[CapacityResult] = None
    rag_synthesis: str = ""
    ai_content: dict = Field(default_factory=dict)
    # B06-T3: describes ONLY the LLM enrichment step of scoring
    # (ScoringEngine.enrich_with_llm) — not every LLM call in the pipeline
    # (extraction, document generation, ...). "unknown" is the field's own
    # default so that a pre-B06-T3 persisted result, deserialized from a
    # dict that never had this key at all, lands on "unknown" without any
    # code needing to special-case it — ScoringEngine.score() and
    # enrich_with_llm always set this explicitly instead of relying on the
    # default for anything they produce themselves. See
    # docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md for the full contract.
    enrichment_status: str = "unknown"
    enrichment_reason: Optional[str] = None
    # B18-T2: describes the NUMERIC integrity of THIS result's own data —
    # distinct from enrichment_status (which is only about the LLM
    # enrichment step). "ok" is actively verified, never assumed: a freshly
    # computed result is "ok" because ensure_valid_evidences() already
    # guaranteed every evidence was valid before the calculation ran; a
    # historical result reloaded from a pre-B18-T1/T2 JSON file is "ok"
    # only if re-checking it finds nothing wrong (see
    # src/web/jobs.py::_sanitize_legacy_evidence_pack). "degraded" means at
    # least one RAG evidence entry was invalid and has been removed from
    # `evidence_pack` — decision/score_global/criteria are the exact values
    # already computed/stored, never recomputed. See
    # docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md for the full contract
    # (documented alongside enrichment_status, the sibling field).
    data_integrity: str = "ok"
    data_integrity_reason: Optional[str] = None
    # B18-T3 (DEFECT F12/E-4): describes the reference-SELECTION step of
    # RAG reranking (src/rag/semantic_rerank.py::SemanticReranker.
    # semantic_rerank) — distinct from both enrichment_status (LLM
    # enrichment of scoring justifications) and data_integrity (numeric
    # soundness of stored data). "unknown" is the field's own default for
    # a result persisted before this field existed — never fabricated as
    # "applied"/"fallback" after the fact. See
    # docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md for the full contract.
    rag_selection_status: str = "unknown"
    rag_selection_reason: Optional[str] = None
    # B06-T4: whether every business rule needed to fully compute THIS
    # result's blockers/decision was actually configured on the active
    # ScoringPolicy for this account. "complete" (the default) is what
    # every result before this ticket implicitly was — a fresh calculation
    # only sets "incomplete" when a specific configured policy is missing
    # one of the newly-extracted business values (see ScoringEngine.score
    # for exactly which). `decision` is set to "INCOMPLET" (never a
    # fabricated GO/GO SOUS RESERVE/NO-GO) whenever this is "incomplete" —
    # `scoring_missing` names each missing rule by its business_rules key
    # so the account knows exactly what to configure. `score_global` still
    # reflects the criteria that COULD be computed — never renormalized to
    # hide the gap, never silently treated as favorable or neutral.
    scoring_completeness: str = "complete"
    scoring_missing: List[str] = Field(default_factory=list)
    # Lot 44 (additive; None/empty on every result stored before it): the
    # criteria schema version and the origin of the policy that produced this
    # result ("legacy" | "user"); whether `score_global` is
    # PROVISIONAL (some criterion was not evaluated — never a success
    # probability); the hypotheses the policy applied for absent data and the
    # criteria found not applicable.
    criteria_version: Optional[int] = None
    policy_origin: Optional[str] = None
    score_provisoire: Optional[bool] = None
    scoring_assumptions: List[str] = Field(default_factory=list)
    scoring_not_applicable: List[str] = Field(default_factory=list)
    # Lot 45 (additive; empty on every result stored before it): the label —
    # in the policy version that PRODUCED this result, frozen at scoring time —
    # of the criterion each `scoring_missing` code comes from. The codes
    # themselves ("criterion:<id>", "custom:<id>", "not_applicable:<id>",
    # legacy rule keys) stay the technical contract; this only lets a screen
    # or a document say which criterion is meant without a newer policy version.
    scoring_missing_labels: Dict[str, str] = Field(default_factory=dict)
    # Lot 49 bis (additive; None for every result stored before it, and for any result scored before this
    # field existed — never fabricated after the fact): the account's declared identity/competences/
    # certifications/business facts EXACTLY as they were when THIS result was computed —
    # {"raison_sociale", "competences", "certifications", "business_facts"} (the same shape
    # src.web.scoring_context.ProviderIdentity carries, serialized). Scoring itself never reads this field
    # (ScoringPolicySnapshot is built once, upstream, from a live read at analysis time) — it exists solely
    # so a LATER completion of this result (src/web/completion_service.py) can freeze the exact provider
    # inputs used here instead of silently re-reading whatever the profile happens to be at completion time.
    # A result with `provider_snapshot=None` cannot be completed reliably: completion refuses explicitly
    # ("nouvelle analyse nécessaire") rather than reconstructing a snapshot from the current profile.
    provider_snapshot: Optional[Dict[str, Any]] = None
    # Lot 53 (additive; empty for every result stored before it, and for any ordinary — non-revision —
    # analysis): the EXACT list of complements applied to produce THIS revision, frozen at submission time
    # by src/web/completion_service.py::apply_completion (never recomputed later, never re-derived from the
    # parent's CURRENT state — the parent may since have been superseded again). Each entry:
    # {"field", "subject", "field_key", "before", "after", "origin", "source_json"} — "before"/"after" are
    # plain display values (already the exact ones apply_completion computed against the frozen parent), and
    # "origin"/"source_json" mirror the SAME complement's own AnalysisComplement row (see src/web/completion_
    # service.py::_resolve_sourced_origin) so a revision's result page/PDF/DOCX can show what changed and
    # why without a second query or any new business logic. A capacity-only change (apply_current_capacity)
    # has "subject": "capacite", "field_key": None. Reused as-is for display — never a second computation of
    # "what changed".
    completion_changes: List[Dict[str, Any]] = Field(default_factory=list)

    def scoring_missing_display(self) -> List[str]:
        """Human-readable names of what `scoring_missing` refers to, in the same
        order, without duplicates. Uses the labels frozen with the result; for a
        result stored before they existed it falls back to the SAME result's own
        rows (`critere_id` → `nom`), and finally to the technical code itself —
        it never consults the account's current policy and never invents a name."""
        labels = self.scoring_missing_labels or {}
        by_id = {c.critere_id: c.nom for c in self.criteres if c.critere_id}
        shown: List[str] = []
        for code in self.scoring_missing or []:
            name = labels.get(code)
            if not name:
                prefix, _, cid = str(code).partition(":")
                if prefix in ("criterion", "custom") and cid in by_id:
                    name = by_id[cid]
                elif prefix == "not_applicable" and cid in by_id:
                    name = f"{by_id[cid]} (non applicable)"
            name = name or str(code)
            if name not in shown:
                shown.append(name)
        return shown
