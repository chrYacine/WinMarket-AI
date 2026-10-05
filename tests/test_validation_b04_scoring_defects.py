"""New diagnostic tests added during the 2026-09-14 self-validation of B04
(docs/qa/validation_b04_20260914/), NOT part of B04's own delivered suite
(tests/test_scoring_engine.py is left as-is except for one recharacterized
test — see test_scoring_engine.py::TestRealisticScenarios::
test_duree_projet_mois_has_no_effect_on_delai_criterion and this same
folder's RAPPORT_VALIDATION_B04.md for why).

These tests document three NEWLY confirmed defects in
src/agents/scoring_engine.py that CONTRAT_SCORING_ACTUEL.md previously
described incorrectly or incompletely. src/agents/scoring_engine.py itself
is NOT modified anywhere in this file or this validation campaign — these
are read-only characterization tests against the engine as it exists today.
"""
from __future__ import annotations

import math

import pytest

from src.agents.scoring_engine import ScoringEngine
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence, ScoringResult
from tests.synthetic_scoring import score_with_synthetic_policy


def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="AO synthétique", client="Client Synthétique", secteur="Retail",
        budget_estime=200_000, deadline_reponse="", duree_projet_mois=None,
        technologies_demandees=[], competences_requises=[], questions_client=[],
        livrables=[], contraintes=[], certifications_obligatoires=[],
        texte_source="Marché de développement logiciel.",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=50, capacite_restante_pct=50, equipe_disponible=True, commentaire="Équipe disponible.")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def make_evidence(**overrides) -> RAGEvidence:
    defaults = dict(query="q", source="ref.md", score=0.5, content="Contenu de référence.")
    defaults.update(overrides)
    return RAGEvidence(**defaults)


def criterion(result: ScoringResult, label_display: str):
    match = next((c for c in result.criteres if c.nom == label_display), None)
    assert match is not None, f"critère {label_display!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


# ---------------------------------------------------------------------------
# DEFECT-B04-04 — CLOSED by B18-T1 (src/core/rag_evidence_validation.py +
# src/core/models.py::RAGEvidence + ScoringEngine.score()'s explicit
# ensure_valid_evidences() call). These three tests used to characterize
# the bug (a negative/non-finite RAGEvidence.score leaking straight through
# to CriterionScore.score/score_global) — they now assert the FIX: such a
# value is refused before it can ever reach a calculation. See
# tests/test_rag_evidence_validation.py for the dedicated, parametrized
# domain tests (valid range, every rejected category, and the
# construction-bypass case) — this class keeps only the
# scoring-engine-level assertions specific to this file's history.
# ---------------------------------------------------------------------------

class TestScoreBoundaryViolations:
    def test_negative_evidence_score_is_refused_at_construction(self):
        with pytest.raises(Exception):  # pydantic.ValidationError
            make_evidence(score=-5.0)

    def test_negative_infinity_evidence_score_is_refused_at_construction(self):
        with pytest.raises(Exception):
            make_evidence(score=-math.inf)

    def test_mixed_valid_and_invalid_evidences_refuses_the_whole_calculation(self):
        """B18-T1 section 3: one invalid evidence among otherwise-valid
        ones interrupts the WHOLE calculation — never silently dropped to
        still produce a decision, never a partial result. The invalid
        entry is built via model_construct() (bypassing RAGEvidence's own
        Pydantic validator entirely) — proving ScoringEngine.score()'s own
        explicit ensure_valid_evidences() call is what actually catches
        it, not just the model's constructor."""
        from src.core.rag_evidence_validation import InvalidRAGEvidenceError

        ao = make_ao()
        valid = make_evidence(score=0.8)
        bypassed_invalid = RAGEvidence.model_construct(query="q", source="ref2.md", score=-5.0, content="c")
        with pytest.raises(InvalidRAGEvidenceError):
            score_with_synthetic_policy(ao, CompanyProfile(), [valid, bypassed_invalid], make_capacity())


# ---------------------------------------------------------------------------
# DEFECT-B04-05 — enrich_with_llm's try/except only wraps the
# llm.json_complete() call itself; a validly-parsed-but-unexpected-shape
# JSON response crashes uncaught. Uses a minimal FakeLLM exposing only
# `.enabled` and `.json_complete(...)` — no real provider, no network I/O,
# no dependency on tests/test_llm_fallback.py's own FakeProvider.
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Stands in for the `llm` argument of enrich_with_llm — bypasses
    LLMClient/AnthropicProvider entirely, simulating exactly what a parsed
    JSON response looks like by the time it reaches enrich_with_llm."""

    def __init__(self, payload):
        self.enabled = True
        self._payload = payload

    def json_complete(self, *_args, **_kwargs):
        return self._payload


class _FakeLLMRaising:
    """A provider whose json_complete() itself raises — exercises
    enrich_with_llm's own `try/except Exception: return result` directly,
    as opposed to LLMClient.json_complete()'s own internal exception
    handling (which already swallows a provider failure into a `None`
    return before enrich_with_llm's try/except ever sees an exception —
    see test_full_pipeline_survives_a_failing_llm_provider below for that
    other, equally real path)."""

    def __init__(self, exc: Exception):
        self.enabled = True
        self._exc = exc

    def json_complete(self, *_args, **_kwargs):
        raise self._exc


class TestEnrichWithLlmMalformedShape:
    def test_provider_exception_leaves_result_intact(self):
        """B05-T1/B06-T2 validation, control 4: an exception raised directly
        by the LLM call (not a malformed-but-returned payload) is caught by
        enrich_with_llm's own try/except and leaves the deterministic
        result untouched — same guarantee as the malformed-shape cases
        above, different trigger."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        decision_before = result.decision
        score_before = result.score_global
        before_justification = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLMRaising(RuntimeError("simulated provider failure")))
        assert returned.decision == decision_before
        assert returned.score_global == score_before
        assert returned.criteres[0].justification == before_justification

    def test_fresh_result_is_not_attempted_before_any_enrichment_call(self):
        """B06-T3: a brand-new score_with_synthetic_policy() output — before
        enrich_with_llm is ever called — is explicitly "not_attempted",
        never the model's own "unknown" default (that default is reserved
        for pre-B06-T3 persisted data missing the field entirely)."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        assert result.enrichment_status == "not_attempted"
        assert result.enrichment_reason is None

    def test_disabled_llm_is_not_attempted_with_llm_disabled_reason_and_zero_calls(self):
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())

        class _DisabledLLM:
            enabled = False
            def json_complete(self, *a, **kw):
                raise AssertionError("must never be called when llm.enabled is False")

        returned = ScoringEngine().enrich_with_llm(ao, result, _DisabledLLM())
        assert returned.enrichment_status == "not_attempted"
        assert returned.enrichment_reason == "llm_disabled"

    def test_provider_exception_sets_failed_status_with_safe_reason_code(self):
        """B06-T3: replaces the earlier 'no signal exists' characterization
        (now closed) with a positive assertion on the real contract — a
        provider exception now sets enrichment_status='failed' with a safe,
        fixed reason code (never the exception's own message/type), while
        every business field stays byte-identical to a never-enriched
        result — enrichment_status/enrichment_reason are the ONLY fields
        allowed to differ."""
        ao = make_ao()
        result_never_enriched = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        result_to_enrich = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        enriched_after_failure = ScoringEngine().enrich_with_llm(
            ao, result_to_enrich, _FakeLLMRaising(RuntimeError("this message must never leak into enrichment_reason")),
        )
        assert enriched_after_failure.enrichment_status == "failed"
        assert enriched_after_failure.enrichment_reason == "provider_exception"
        assert "never leak" not in (enriched_after_failure.enrichment_reason or "")

        before = result_never_enriched.model_dump(exclude={"enrichment_status", "enrichment_reason"})
        after = enriched_after_failure.model_dump(exclude={"enrichment_status", "enrichment_reason"})
        assert before == after, "every business field (not just decision/score) must stay byte-identical on failure"

    def test_old_result_without_the_field_defaults_to_unknown(self):
        """B06-T3: a result deserialized from data that predates this
        ticket (no enrichment_status/enrichment_reason key at all) must
        land on 'unknown' — never a fabricated enrichment history."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        old_style_dict = result.model_dump(exclude={"enrichment_status", "enrichment_reason"})
        reloaded = type(result)(**old_style_dict)
        assert reloaded.enrichment_status == "unknown"
        assert reloaded.enrichment_reason is None
        # Reloading an "unknown" result must never rewrite its scores.
        assert reloaded.score_global == result.score_global
        assert reloaded.decision == result.decision

    @pytest.mark.parametrize("payload,expected_status,expected_reason", [
        pytest.param(None, "failed", "no_content", id="invalid_json_none"),
        pytest.param([], "failed", "no_content", id="empty_json_list"),
        pytest.param([1, 2, 3], "failed", "invalid_response_shape", id="non_empty_json_list"),
        pytest.param({}, "failed", "no_content", id="empty_dict_no_known_fields"),
        pytest.param({"foo": "bar"}, "failed", "no_content", id="dict_with_only_unknown_keys"),
        pytest.param({"forces": "pas une liste"}, "failed", "invalid_response_shape", id="single_field_wrong_type"),
        pytest.param({"justifications": {}}, "failed", "no_content", id="justifications_valid_but_empty"),
    ])
    def test_enrichment_status_for_unusable_payloads(self, payload, expected_status, expected_reason):
        """B06-T3 point 1, paramétré: toute réponse sans contenu exploitable
        (JSON invalide, liste, dict vide/sans clé connue, un seul champ mal
        typé, ou un champ valide mais vide) doit produire enrichment_status
        exactement 'failed', avec un code de raison stable et prévisible —
        jamais 'applied'/'partial' pour un no-op."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.model_dump(exclude={"enrichment_status", "enrichment_reason"})
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM(payload))
        assert returned.enrichment_status == expected_status
        assert returned.enrichment_reason == expected_reason
        after = returned.model_dump(exclude={"enrichment_status", "enrichment_reason"})
        assert before == after, "no business field may move when nothing usable was applied"

    @pytest.mark.parametrize("payload,expected_status,expect_forces_applied", [
        pytest.param(
            {"justifications": {"CRITERION": "texte reel"}, "forces": ["Force reelle"]},
            "applied", True, id="fully_valid_response",
        ),
        pytest.param(
            {"justifications": {"CRITERION": "texte reel"}, "forces": "pas une liste"},
            "partial", False, id="mixed_valid_and_invalid_fields",
        ),
    ])
    def test_enrichment_status_for_usable_payloads(self, payload, expected_status, expect_forces_applied):
        """B06-T3 point 1, paramétré: une réponse qui applique réellement au
        moins un champ autorisé et utile est 'applied' si rien n'est rejeté,
        'partial' si un autre champ fourni est rejeté — jamais l'inverse."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        original_forces = list(result.forces)
        original_criterion_name = result.criteres[0].nom
        payload = {**payload, "justifications": {original_criterion_name: payload["justifications"]["CRITERION"]}}
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM(payload))
        assert returned.enrichment_status == expected_status
        assert returned.criteres[0].justification == "texte reel"
        if expect_forces_applied:
            assert returned.forces == ["Force reelle"]
        else:
            assert returned.forces == original_forces

    def test_syntactically_invalid_json_leaves_result_intact(self):
        """The ORIGINAL contract claim, still correct for this specific
        case: LLMClient._parse_json_response returns None on a
        JSONDecodeError, and `if not data: return result` catches it
        cleanly. No crash, no mutation."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM(None))
        assert returned is result
        assert returned.criteres[0].justification == before

    def test_empty_json_list_leaves_result_intact(self):
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM([]))
        assert returned.criteres[0].justification == before

    def test_non_empty_json_list_no_longer_crashes(self):
        """CLOSED by B06-T1 (was DEFECT-B04-05): a validly-parsed JSON array
        (e.g. the LLM answered with a bare list instead of an object) is
        NOT syntactically invalid and is NOT falsy — `data.get(...)` used
        to run on it OUTSIDE the try/except that only wraps
        json_complete(), raising AttributeError straight out of
        enrich_with_llm. scoring_engine.py now checks `isinstance(data,
        dict)` before touching it — a malformed shape is treated exactly
        like "no usable enrichment", never a crash, never a favorable
        score. Minimal fix for this ticket's path — see B05/B19 for a
        fuller LLM-response-shape validation pass (not claimed closed
        here)."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM([1, 2, 3]))
        assert returned.criteres[0].justification == before

    def test_justifications_field_as_list_instead_of_dict_no_longer_crashes(self):
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM({"justifications": ["a", "b"]}))
        assert returned.criteres[0].justification == before

    def test_justifications_field_as_string_instead_of_dict_no_longer_crashes(self):
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        before = result.criteres[0].justification
        returned = ScoringEngine().enrich_with_llm(ao, result, _FakeLLM({"justifications": "oops"}))
        assert returned.criteres[0].justification == before

    def test_mixed_valid_and_malformed_fields_apply_valid_ones_without_partial_corruption(self):
        """B06-T2 point 1 — explicit re-verification requested before T2:
        a response with a VALID justifications dict alongside a malformed
        "forces" (string instead of list) must apply the valid part and
        cleanly ignore the malformed one — never crash, never leave forces
        in a half-written/wrong-typed state, never silently corrupt
        unrelated fields. This is the "sans mutation partielle" guarantee
        checked field-by-field, not just "no crash" at the top level."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        original_forces = list(result.forces)
        original_criterion_name = result.criteres[0].nom
        returned = ScoringEngine().enrich_with_llm(
            ao, result,
            _FakeLLM({
                "justifications": {original_criterion_name: "Justification de test mise à jour."},
                "forces": "ceci n'est pas une liste",  # malformed — must be ignored, not crash
                "risques": ["Risque réel"],  # valid — must still apply
            }),
        )
        assert returned.criteres[0].justification == "Justification de test mise à jour.", (
            "the valid justification must be applied even though a later field is malformed"
        )
        assert returned.forces == original_forces, "a malformed 'forces' must leave the field UNCHANGED, not corrupted"
        assert returned.risques == ["Risque réel"], "a valid field after a malformed one must still apply"

    def test_well_formed_dict_still_enriches_correctly(self):
        """Control — proves the three crashes above are shape-specific, not
        a general break in enrich_with_llm."""
        ao = make_ao()
        result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
        decision_before = result.decision
        score_before = result.score_global
        returned = ScoringEngine().enrich_with_llm(
            ao, result, _FakeLLM({"justifications": {}, "forces": ["Force 1"], "risques": ["Risque 1"]}),
        )
        assert returned.forces == ["Force 1"]
        assert returned.risques == ["Risque 1"]
        # score_global/decision/criteres_bloquants must never move via enrichment.
        assert returned.score_global == score_before
        assert returned.decision == decision_before

    def test_llm_supplied_decision_key_is_ignored_on_a_blocked_ao(self):
        """Regression guard added after fault-injection M05 (see
        docs/qa/validation_b04_20260914/PREUVES_SENSIBILITE.md): the
        current enrich_with_llm() has NO code path that reads a "decision"
        key at all, so this passes today by construction. Nothing in the
        suite before this test sent a "decision" key in the fake payload,
        so a future regression that added
        `if data.get("decision"): result.decision = data["decision"]`
        would go undetected until this test exists. `justifications` must
        also remain untouched (empty dict) — the FakeLLM sends no matching
        key, so no criterion should change from its algorithmic value."""
        ao = make_ao(
            budget_estime=500_000, technologies_demandees=["Python", "React", "AWS"],
            certifications_obligatoires=["SecNumCloud"],
        )
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=20)
        result = score_with_synthetic_policy(ao, CompanyProfile(), [make_evidence(score=0.9)] * 5, capacity)
        decision_before = result.decision
        blockers_before = list(result.criteres_bloquants)
        assert decision_before == "NO-GO"

        returned = ScoringEngine().enrich_with_llm(
            ao, result, _FakeLLM({"justifications": {}, "decision": "GO", "score_global": 100}),
        )
        assert returned.decision == decision_before
        assert returned.criteres_bloquants == blockers_before
        # This specific payload is a universal no-op (empty justifications,
        # no other authorized field, decision/score_global are foreign keys
        # that are never even read) — B06-T3: it must NOT be reported as
        # "applied", precisely so a green enrichment test can't hide behind
        # a payload that changed nothing at all.
        assert returned.enrichment_status == "failed"
        assert returned.enrichment_reason == "no_content"

    def test_go_100_payload_with_real_content_still_protects_decision_and_score(self):
        """Same GO/100 override attempt as above, but paired with a REAL,
        useful justification — proves decision/score protection holds even
        when enrichment genuinely applies something (status='applied'),
        not just in the earlier no-op case where nothing happened anyway."""
        ao = make_ao(
            budget_estime=500_000, technologies_demandees=["Python", "React", "AWS"],
            certifications_obligatoires=["SecNumCloud"],
        )
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=20)
        result = score_with_synthetic_policy(ao, CompanyProfile(), [make_evidence(score=0.9)] * 5, capacity)
        decision_before = result.decision
        score_before = result.score_global
        blockers_before = list(result.criteres_bloquants)
        weights_before = [(c.nom, c.poids) for c in result.criteres]
        original_criterion_name = result.criteres[0].nom
        assert decision_before == "NO-GO"

        returned = ScoringEngine().enrich_with_llm(
            ao, result,
            _FakeLLM({
                "justifications": {original_criterion_name: "Justification réelle et spécifique à cet AO."},
                "decision": "GO", "score_global": 100,
            }),
        )
        assert returned.enrichment_status == "applied"
        assert returned.criteres[0].justification == "Justification réelle et spécifique à cet AO."
        assert returned.decision == decision_before
        assert returned.score_global == score_before
        assert returned.criteres_bloquants == blockers_before
        assert [(c.nom, c.poids) for c in returned.criteres] == weights_before


# ---------------------------------------------------------------------------
# Related to DEFECT-B04-03 — confirms the actual, real-world impact of the
# missing accent, using the value src/agents/company_enrichment.py really
# produces for a live Pappers profile ("À vérifier", with accent). No
# Pappers call is made — CompanyProfile is built directly, synthetically.
# ---------------------------------------------------------------------------

class TestSolidityAccentMismatchRealValue:
    def test_real_pappers_value_with_accent_falls_in_unfavorable_branch(self):
        """'À vérifier' (the value a real Pappers-backed CompanyProfile
        actually carries) does not match scoring_engine.py's unaccented
        "A verifier" literal — it falls through to the SAME unfavorable
        branch as an explicitly "Faible" company (score 60), not the
        favorable branch alongside "Bonne" (85). An unverified company is
        therefore currently treated with caution, not favorably — anyone
        "fixing" the accent via symmetric normalization must not silently
        invert this into a favorable treatment without a business
        decision — see PROMPT_PROCHAIN_TICKET.txt's added warning."""
        ao = make_ao()
        company_accented = CompanyProfile(solidite_financiere="À vérifier")
        company_faible = CompanyProfile(solidite_financiere="Faible")
        company_bonne = CompanyProfile(solidite_financiere="Bonne")

        result_accented = score_with_synthetic_policy(ao, company_accented, [], make_capacity())
        result_faible = score_with_synthetic_policy(ao, company_faible, [], make_capacity())
        result_bonne = score_with_synthetic_policy(ao, company_bonne, [], make_capacity())

        score_accented = criterion(result_accented, "Solidité client").score
        assert score_accented == criterion(result_faible, "Solidité client").score == 60.0
        assert criterion(result_bonne, "Solidité client").score == 85.0
        assert score_accented != criterion(result_bonne, "Solidité client").score
