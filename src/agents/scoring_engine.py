from dataclasses import dataclass, field
from pathlib import Path

from src.agents import business_facts, criteria_catalogue, criteria_evaluators
from src.agents.criteria_evaluators import fold_accents as _fold_accents
from src.core.models import AOContext, CompanyProfile, CapacityResult, CriterionScore, ScoringResult
from src.core.config import LLM_TEMPERATURE_FACTUAL
from src.core.private_configuration import PrivateConfigurationRequired
from src.core.prompt_loader import PromptLoadError, load_prompt
from typing import Optional

# B16-T1: the two remaining prompts this module used to keep inline as
# Python f-strings, now in their own text files — same shared loader
# already used by src/livrables/document_generator.py (document_*.txt) and
# src/rag/reference_selection.py (reference_selection.txt), never a second
# ad hoc mechanism. A missing file raises FileNotFoundError from
# load_prompt's own `path.read_text(...)` — a safe, visible error, never a
# silent fallback to invented content.
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SCORING_SYSTEM_PATH = _PROMPTS_DIR / "scoring_system.txt"
_SCORING_ENRICHMENT_USER_PATH = _PROMPTS_DIR / "scoring_enrichment_user.txt"


@dataclass
class ScoringPolicySnapshot:
    """The engine's ONLY configuration input: a plain, DB-decoupled copy of
    one private policy + provider profile, resolved for one organization +
    owner (src/web/scoring_context.py).

    Lot 44: the policy is a LIST OF EXPLICIT CRITERIA (`criteria`, schema
    `criteria_version`, see src/agents/criteria_catalogue.py) plus the
    decision thresholds and a few policy-level settings. Nothing about the
    twelve historical criteria, their scales, their unknown-data notes or
    their blocking rules lives in the engine any more: a historical policy
    carries them as explicit parameters, a new policy starts with none.

    `mastered_technologies` / `certifications_held` / `declared_facts` are the
    account's DECLARED data (profile), read by the evaluators that need them.
    `from_legacy` builds a snapshot from the pre-lot-44 field set (weights,
    business rules, custom criteria) through the same materialization the
    migration uses — a representation adapter, not a second engine."""
    criteria: list
    threshold_go: float
    threshold_sous_reserve: float
    mastered_technologies: frozenset[str] = field(default_factory=frozenset)
    certifications_held: frozenset[str] = field(default_factory=frozenset)
    declared_facts: dict[str, dict] = field(default_factory=dict)
    settings: dict = field(default_factory=criteria_catalogue.default_settings)
    version: Optional[int] = None
    criteria_version: Optional[int] = criteria_catalogue.SCHEMA_VERSION
    origin: Optional[str] = None

    @classmethod
    def from_legacy(
        cls, *, weights, threshold_go, threshold_sous_reserve, mastered_technologies=frozenset(),
        certifications_held=frozenset(), version=None, budget_minimum_eur=None, max_charge_pct=None,
        max_unmastered_technologies=None, certification_penalty_score=None, custom_criteria=None, declared_facts=None,
    ) -> "ScoringPolicySnapshot":
        rules = {k: v for k, v in (
            ("budget_minimum_eur", budget_minimum_eur), ("max_charge_pct", max_charge_pct),
            ("max_unmastered_technologies", max_unmastered_technologies),
            ("certification_penalty_score", certification_penalty_score),
        ) if v is not None}
        criteria, settings = criteria_catalogue.materialize_legacy(
            weights=weights, business_rules=rules, custom_criteria=custom_criteria if custom_criteria is not None else [],
        )
        return cls(
            criteria=criteria, threshold_go=threshold_go, threshold_sous_reserve=threshold_sous_reserve,
            mastered_technologies=frozenset(mastered_technologies), certifications_held=frozenset(certifications_held),
            declared_facts=declared_facts if declared_facts is not None else {}, settings=settings, version=version,
            origin="legacy",
        )


class ScoringEngine:
    # Lot 43/44: no weights, mastered technologies, certifications, thresholds
    # or scales live on the class — everything the calculation depends on comes
    # from the ScoringPolicySnapshot passed to score(). `labels` /
    # `label_display` only name the twelve HISTORICAL criteria (used to read
    # and validate historical policies).
    labels = {
        "Adequation expertise":     "Adequation expertise",
        "References similaires":    "References similaires",
        "Disponibilite equipe":     "Disponibilite equipe",
        "Rentabilite estimee":      "Rentabilite estimee",
        "Faisabilite delai":        "Faisabilite delai",
        "Certifications requises":  "Certifications requises",
        "Complexite technique":     "Complexite technique",
        "Connaissance secteur":     "Connaissance secteur",
        "Potentiel commercial":     "Potentiel commercial",
        "Risque contractuel":       "Risque contractuel",
        "Solidite client":          "Solidite client",
        "Valeur strategique":       "Valeur strategique",
    }
    label_display = {
        "Adequation expertise":     "Ad\u00e9quation expertise",
        "References similaires":    "R\u00e9f\u00e9rences similaires",
        "Disponibilite equipe":     "Disponibilit\u00e9 \u00e9quipe",
        "Rentabilite estimee":      "Rentabilit\u00e9 estim\u00e9e",
        "Faisabilite delai":        "Faisabilit\u00e9 d\u00e9lai",
        "Certifications requises":  "Certifications requises",
        "Complexite technique":     "Complexit\u00e9 technique",
        "Connaissance secteur":     "Connaissance secteur",
        "Potentiel commercial":     "Potentiel commercial",
        "Risque contractuel":       "Risque contractuel",
        "Solidite client":          "Solidit\u00e9 client",
        "Valeur strategique":       "Valeur strat\u00e9gique",
    }

    def score(
        self,
        ao: AOContext,
        company: CompanyProfile,
        evidences: list,
        capacity: CapacityResult,
        policy: ScoringPolicySnapshot,
    ) -> ScoringResult:
        """Lot 43: `policy` is REQUIRED (PrivateConfigurationRequired
        otherwise). Lot 44: the calculation is generic — each criterion of the
        policy is evaluated by its catalogue evaluator; the engine only
        applies the technical, coded parts: the 0–100 bounds, the weighted sum
        (over 100, or over the applicable weights when the policy explicitly
        validated that rule), and the decision order
        blocker > INCOMPLET > thresholds."""
        from src.core.rag_evidence_validation import ensure_valid_evidences
        from src.core.reference_identity import deduplicate_evidences

        if policy is None:
            raise PrivateConfigurationRequired("scoring_policy")

        # B18-T1 (DEFECT-B04-04): explicit re-check immediately before
        # evidences are consumed — catches an evidence built through a path
        # that skips Pydantic entirely. Validated FIRST, deduplicated SECOND
        # (B18-T4): an invalid evidence hiding behind a valid twin must still
        # raise.
        ensure_valid_evidences(evidences)
        evidences = deduplicate_evidences(evidences)

        settings = {**criteria_catalogue.default_settings(), **(policy.settings if isinstance(policy.settings, dict) else {})}
        ctx = criteria_evaluators.EvalContext(
            ao=ao, company=company, evidences=evidences, capacity=capacity,
            mastered=policy.mastered_technologies, certifications_held=policy.certifications_held,
            declared_facts=policy.declared_facts if isinstance(policy.declared_facts, dict) else {},
            legacy_unconfigured_rules=frozenset(str(r) for r in (settings.get("legacy_unconfigured_rules") or [])),
        )

        rows: list[CriterionScore] = []
        blockers: list[str] = []
        missing: list[str] = []
        # Lot 45: code -> label of the criterion it comes from, frozen with the
        # result so the screens/documents never print a bare "criterion:<id>"
        # nor consult a newer policy version. The codes stay the contract.
        missing_labels: dict[str, str] = {}
        not_applicable_labels: dict[str, str] = {}
        assumptions: list[str] = []
        not_applicable: list[str] = []
        criteria = policy.criteria
        if not isinstance(criteria, list):
            missing.append("criteria:configuration")
            missing_labels["criteria:configuration"] = "Configuration des critères de la politique"
            criteria = []

        for index, spec in enumerate(criteria):
            spec = spec if isinstance(spec, dict) else {}
            cid = spec.get("id") if isinstance(spec.get("id"), str) and spec.get("id") else f"invalide_{index}"
            label = spec.get("label") if isinstance(spec.get("label"), str) and spec.get("label").strip() else cid
            evaluator = spec.get("evaluator") if isinstance(spec.get("evaluator"), str) else None
            if spec.get("enabled", True) is False:
                reason = spec.get("disabled_reason") if isinstance(spec.get("disabled_reason"), str) else "non précisé"
                rows.append(CriterionScore(
                    nom=label, poids=0.0, score=0.0, justification=f"Critère désactivé par la politique — motif : {reason}.",
                    etat="non_applicable", motif="disabled_by_policy", evaluateur=evaluator, critere_id=cid, bloquant=False,
                ))
                continue
            valid_weight = business_facts.has_valid_weight(spec)
            weight = business_facts.safe_weight(spec)
            outcome = criteria_evaluators.evaluate(spec, ctx)
            if not valid_weight:
                outcome = criteria_evaluators.Outcome(
                    "missing", None, "Poids du critère invalide ou absent : critère non évaluable.", "invalid_weight", False,
                    outcome.blocker,
                )
            if outcome.blocker:
                blockers.append(outcome.blocker)
            missing.extend(r for r in outcome.missing_rules if r not in missing)
            for rule in outcome.missing_rules:
                missing_labels.setdefault(rule, label)
            family = criteria_catalogue.evaluator_family(evaluator)
            name = f"custom:{cid}" if (family == "fact" or evaluator == "invalid") else f"criterion:{cid}"

            etat, score, justification, motif = "evalue", 0.0, outcome.justification, outcome.reason
            if outcome.state == "evaluated":
                score = min(100.0, float(outcome.score))
                if outcome.assumed:
                    etat = "hypothese"
                    assumptions.append(f"{label} : {outcome.justification}")
            else:
                on_missing = spec.get("on_missing") if isinstance(spec.get("on_missing"), dict) else {"mode": "incomplete"}
                mode = on_missing.get("mode") if outcome.recoverable and spec.get("blocking") is not True else "incomplete"
                fallback = on_missing.get("score")
                if mode == "explicit_score" and business_facts._is_finite_number(fallback) and 0 <= fallback <= 100:
                    etat, score, motif = "hypothese", float(fallback), outcome.reason
                    justification = (f"Hypothèse de la politique (donnée absente : {outcome.reason}) : note {fallback:g} appliquée. "
                                     f"{outcome.justification}")
                    assumptions.append(f"{label} : note {fallback:g} appliquée par hypothèse de la politique ({outcome.reason}).")
                elif mode == "not_applicable":
                    etat, score = "non_applicable", 0.0
                    not_applicable.append(cid)
                    not_applicable_labels[cid] = label
                else:
                    etat, score = "manquant", 0.0
                    missing.append(name)
                    missing_labels[name] = label
            rows.append(CriterionScore(
                nom=label, poids=weight, score=score, justification=justification, etat=etat, evaluateur=evaluator,
                critere_id=cid, bloquant=bool(outcome.blocker), motif=motif,
            ))

        # --- weighted sum — technical, coded. Missing criteria contribute 0
        # (never a note) and the score is then reported as PROVISIONAL.
        renormalize = bool(not_applicable) and settings.get("not_applicable_rule") == "renormalize" \
            and settings.get("not_applicable_rule_confirmed") is True
        counted = [r for r in rows if r.etat in ("evalue", "hypothese", "manquant")]
        denominator = sum(r.poids for r in counted) if renormalize else 100.0
        global_score = round(sum(r.score * r.poids for r in counted) / denominator, 1) if denominator > 0 else 0.0
        if not_applicable and not renormalize:
            # No applicable weighting rule was explicitly validated: never a
            # redistribution, never a complete score.
            missing.extend(f"not_applicable:{cid}" for cid in not_applicable)
            for cid in not_applicable:
                missing_labels[f"not_applicable:{cid}"] = f"{not_applicable_labels.get(cid, cid)} (non applicable)"

        incomplete = bool(missing)
        threshold_go, threshold_sous_reserve = policy.threshold_go, policy.threshold_sous_reserve
        if blockers:
            # A blocker that DID fire was fully determined — the NO-GO is
            # confirmed even if other data is unknown, and a null weight never
            # disables it.
            decision = "NO-GO"
        elif incomplete:
            decision = "INCOMPLET"
        elif global_score >= threshold_go:
            decision = "GO"
        elif global_score >= threshold_sous_reserve:
            decision = "GO SOUS RESERVE"
        else:
            decision = "NO-GO"

        strengths_at, weaknesses_below = settings.get("strengths_at_least"), settings.get("weaknesses_below")
        evaluated = [r for r in rows if r.etat in ("evalue", "hypothese")]
        forces = [r.nom for r in evaluated if business_facts._is_finite_number(strengths_at) and r.score >= strengths_at][:5]
        faiblesses = [r.nom for r in evaluated if business_facts._is_finite_number(weaknesses_below) and r.score < weaknesses_below][:5]
        unevaluated = [r.nom for r in rows if r.etat in ("manquant", "non_applicable") and r.motif != "disabled_by_policy"]
        risques = list(blockers) + [f"Critère non évalué : {nom}" for nom in unevaluated[:4]] + assumptions[:3]

        return ScoringResult(
            decision=decision,
            score_global=global_score,
            criteres=rows,
            criteres_bloquants=blockers,
            forces=forces,
            faiblesses=faiblesses,
            risques=risques,
            recommandations=self._build_recommendations(
                ao, decision, blockers,
                list(dict.fromkeys(missing_labels.get(code, code) for code in sorted(missing))),
                assumptions, global_score, threshold_go, threshold_sous_reserve,
            ),
            evidence_pack=evidences,
            company_profile=company,
            capacity=capacity,
            # B06-T3: a freshly computed result has never been through
            # enrich_with_llm yet — explicit, never relying on the model's
            # own "unknown" default (that default is reserved for a result
            # deserialized from data that predates this field entirely).
            enrichment_status="not_attempted",
            enrichment_reason=None,
            # B18-T2: every evidence was already validated above — a
            # freshly-scored result is always "ok" by construction.
            data_integrity="ok",
            data_integrity_reason=None,
            scoring_completeness="incomplete" if incomplete else "complete",
            scoring_missing=sorted(missing) if incomplete else [],
            # Lot 44: what a historical result never carried.
            criteria_version=policy.criteria_version,
            policy_origin=policy.origin,
            score_provisoire=bool(missing),
            scoring_assumptions=assumptions,
            scoring_not_applicable=[r.nom for r in rows if r.etat == "non_applicable"],
            scoring_missing_labels={code: missing_labels[code] for code in sorted(missing) if code in missing_labels} if incomplete else {},
        )

    def _build_recommendations(self, ao, decision, blockers, missing, assumptions, score, threshold_go, threshold_sous_reserve):
        """Lot 44: only statements derived from THIS analysis and THIS
        account's policy — no delay, staffing rule or process is invented.
        `missing` holds the DISPLAY names (criterion labels of the policy that
        produced the result — lot 45), not the technical codes."""
        recs = []
        if decision == "INCOMPLET":
            recs.append("Compléter les données ou la configuration manquantes : " + ", ".join(missing))
            recs.append("Relancer l'analyse une fois ces éléments renseignés : aucune décision GO/NO-GO n'est fiable sans eux")
            recs.append(f"Le score affiché ({score:g}) est provisoire : il ne tient pas compte des critères non évalués")
            return recs[:4]
        if decision == "NO-GO":
            if blockers:
                recs.append("Exigence(s) bloquante(s) constatée(s) : " + " ; ".join(blockers[:3]))
            else:
                recs.append(f"Le score ({score:g}) est inférieur au seuil « GO sous réserve » de la politique ({threshold_sous_reserve:g})")
        elif "RESERVE" in decision:
            recs.append(f"Le score ({score:g}) est compris entre le seuil « GO sous réserve » ({threshold_sous_reserve:g}) et le seuil GO ({threshold_go:g}) de la politique")
        else:
            recs.append(f"Le score ({score:g}) atteint le seuil GO de la politique ({threshold_go:g})")
        if assumptions:
            recs.append("Confirmer les hypothèses de la politique appliquées faute de donnée : " + " ; ".join(assumptions[:2]))
        if ao.deadline_reponse:
            recs.append(f"Date limite de réponse indiquée dans l'AO : {ao.deadline_reponse}")
        return recs[:4]

    # B19-T1: method/tone guidance ONLY — deliberately states no fact about
    # any specific company. The company-identity sentence is prepended at
    # call time by _build_scoring_system_prompt() from the account's real
    # ProviderProfile. The previous version of this constant asserted a
    # headcount range, a tenure in years and a won/lost-tender track record
    # that belonged to no real account: the LLM took them as fact and they
    # leaked into justifications, forces and recommandations presented to
    # the user as their own company's. Nothing of the sort may be
    # reintroduced here — if it isn't in ProviderProfile, it is not said
    # (tests/test_b06_t4_business_rules.py scans this module's source for
    # the exact claims that were removed).
    def _build_scoring_system_prompt(self, provider_profile=None) -> str:
        """B19-T1: the scoring system prompt, built from what is actually
        known about the account's own organization (a src.web.database.models.
        ProviderProfile row) instead of a fabricated biography.

        `provider_profile=None` — any caller that has no profile — gets a
        generic, honest role with no invented specifics at all. Never a
        headcount, a number of years, or a win/loss record: those are not
        fields of ProviderProfile and must not be conjured.

        B16-T1: the method/tone guidance tail (which states no fact about
        any specific company) lives in
        src/agents/prompts/scoring_system.txt; only the dynamic identity
        sentence — which cannot be a static file, its shape genuinely
        varies with whether a profile/competences were declared — is built
        here and substituted in."""
        raison_sociale = (getattr(provider_profile, "raison_sociale", None) or "").strip()
        if not raison_sociale:
            # Lot 44: no assumed role, sector or seniority — the account's own
            # profile is the only source of identity.
            identity = ("Tu analyses un appel d'offres pour le compte du prestataire qui y répond. Son identité, son métier, "
                        "sa taille et ses réalisations ne te sont pas communiqués : n'en invente aucun.")
        else:
            identity = f"Tu analyses un appel d'offres pour le compte de {raison_sociale}."
            competences = [
                str(c).strip() for c in (getattr(provider_profile, "competences", None) or []) if str(c).strip()
            ]
            if competences:
                # Omitted entirely when the profile declares none — an
                # empty competence list is not a reason to invent one.
                identity += f" Ses compétences déclarées incluent : {', '.join(competences)}."
        return load_prompt(_SCORING_SYSTEM_PATH, identity=identity)

    _EXPLANATORY_LIST_FIELDS = ("forces", "faiblesses", "recommandations", "risques")

    def enrich_with_llm(self, ao: AOContext, result: ScoringResult, llm, provider_profile=None) -> ScoringResult:
        """Enrichit les justifications, forces, faiblesses et recommandations
        avec Claude — jamais decision/score_global/poids/criteres_bloquants/
        evidence_pack/company_profile/capacity, qui ne sont même pas dans le
        dictionnaire que cette méthode réassigne.

        B06-T3: renseigne `result.enrichment_status` (et, si utile,
        `enrichment_reason`, un code sûr, jamais un détail d'exception, une
        clé ou du contenu de prompt brut) décrivant UNIQUEMENT cette étape
        d'enrichissement du scoring — pas les autres appels LLM du
        pipeline (extraction, génération de documents). Voir
        docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md pour le contrat
        complet. Chaque champ explicatif fourni par le LLM est validé
        entièrement avant application ("changements candidats" construits
        d'abord, publiés ensuite) — jamais de sous-structure incohérente
        (ex. une moitié de dict `justifications` appliquée).

        B19-T1: `provider_profile` (optionnel, une ligne
        src.web.database.models.ProviderProfile) est l'identité RÉELLE de
        l'organisation du compte, utilisée pour construire le system prompt — voir
        _build_scoring_system_prompt. Absent (défaut), le prompt reste
        générique et n'invente aucun effectif, ancienneté ni palmarès."""
        if not llm.enabled:
            result.enrichment_status = "not_attempted"
            result.enrichment_reason = "llm_disabled"
            return result

        def _criterion_line(c) -> str:
            if c.etat in ("manquant", "non_applicable"):
                statut = "NON ÉVALUÉ" if c.etat == "manquant" else "NON APPLICABLE"
                return f"- {c.nom} ({int(c.poids)}%) : {statut} — {c.justification}"
            hypothese = " [HYPOTHÈSE DE LA POLITIQUE, pas un fait établi]" if c.etat == "hypothese" else ""
            return f"- {c.nom} ({int(c.poids)}%) : {c.score:.0f}/100{hypothese} — {c.justification}"

        criteres_text = "\n".join(_criterion_line(c) for c in result.criteres)
        if result.score_provisoire:
            criteres_text += "\nLe score global est PROVISOIRE : des critères ne sont pas évalués."

        blockers_text = (
            "CRITÈRES BLOQUANTS DÉTECTÉS :\n" + "\n".join(f"  ⛔ {b}" for b in result.criteres_bloquants)
            if result.criteres_bloquants else "Aucun critère bloquant."
        )

        # B16-T1: the static instructions/format now live in
        # src/agents/prompts/scoring_enrichment_user.txt (same shared
        # load_prompt loader as _build_scoring_system_prompt above) — only
        # the per-analysis VALUES are computed here, exactly as the f-string
        # this replaces already computed them (same formatting, same
        # truncation/fallback rules, byte-for-byte).
        #
        # B16-T2 (DEFECT confirmed, reproduced via a real job — campaign 36):
        # a missing/unreadable prompt file used to propagate straight out of
        # this method (nothing here caught it), through analysis_service.
        # score_and_enrich, and out to jobs.py's own generic except clause —
        # which discarded the ALREADY-COMPUTED algorithmic score entirely
        # (job.result was never assigned) and reported error_code=
        # "scoring_failed", indistinguishable from a genuine scoring bug.
        # Both prompt loads below (this one and the system prompt built via
        # _build_scoring_system_prompt) are now caught HERE, at the source,
        # exactly like this method's other enrichment-only failure modes
        # (llm_disabled/provider_exception/no_content below) — the
        # algorithmic result is returned UNCHANGED, never discarded and
        # never replaced with an invented score, only enrichment itself is
        # marked degraded with a distinguishable reason.
        try:
            prompt = load_prompt(
                _SCORING_ENRICHMENT_USER_PATH,
                titre=ao.titre,
                client=ao.client,
                secteur=ao.secteur or "Non précisé",
                budget=f"{ao.budget_estime:,.0f} €" if ao.budget_estime else "Non communiqué",
                duree=f"{ao.duree_projet_mois} mois" if ao.duree_projet_mois else "Non précisée",
                technologies=", ".join(ao.technologies_demandees) or "Non précisées",
                competences=", ".join(ao.competences_requises[:5]) or "Non précisées",
                livrables=", ".join(ao.livrables[:4]) or "Non précisés",
                contraintes=" | ".join(ao.contraintes[:4]) or "Aucune identifiée",
                certifications=", ".join(ao.certifications_obligatoires) or "Aucune",
                criteres_text=criteres_text,
                decision=result.decision,
                score_global=str(result.score_global),
                blockers_text=blockers_text,
                secteur_or_sectoriel=ao.secteur or "sectoriel",
            )
            system_prompt = self._build_scoring_system_prompt(provider_profile)
        except PromptLoadError:
            result.enrichment_status = "failed"
            result.enrichment_reason = "prompt_missing"
            return result
        try:
            data = llm.json_complete(
                prompt, system=system_prompt,
                temperature=LLM_TEMPERATURE_FACTUAL, max_tokens=4000,
            )
        except Exception:
            result.enrichment_status = "failed"
            result.enrichment_reason = "provider_exception"
            return result
        if not data:
            result.enrichment_status = "failed"
            result.enrichment_reason = "no_content"
            return result
        # DEFECT-B04-05 fix (docs/qa/validation_b04_20260914/BACKLOG_CORRECTIONS.md):
        # the try/except above only ever covered json_complete() itself — a
        # syntactically VALID JSON response of unexpected shape (a bare
        # list, or "justifications" not being an object) used to crash here
        # uncaught with AttributeError, propagating out of the whole
        # pipeline for an otherwise-successful analysis. A malformed shape
        # is treated exactly like "no usable enrichment": the algorithmic
        # result is returned unchanged, never silently turned into a
        # favorable score.
        if not isinstance(data, dict):
            result.enrichment_status = "failed"
            result.enrichment_reason = "invalid_response_shape"
            return result

        # --- Build candidate changes first, validating each field ENTIRELY
        # before it counts as accepted — never a half-applied field (e.g. a
        # "justifications" dict where only the well-typed entries would
        # otherwise be kept). Only "justifications", "forces", "faiblesses",
        # "recommandations", "risques" are ever read from `data` — any other
        # key (notably "decision"/"score_global", which the LLM is asked
        # never to send but might anyway) is silently ignored, never counted
        # as a provided/rejected field, and never applied.
        provided_fields: set[str] = set()
        accepted_fields: set[str] = set()
        useful_fields: set[str] = set()
        justification_matches: list[tuple] = []

        if "justifications" in data:
            provided_fields.add("justifications")
            justs = data["justifications"]
            if isinstance(justs, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in justs.items()):
                accepted_fields.add("justifications")
                for c in result.criteres:
                    nom_norm = _fold_accents(c.nom)
                    for key, val in justs.items():
                        key_norm = _fold_accents(key)
                        if key_norm in nom_norm or nom_norm in key_norm:
                            justification_matches.append((c, val))
                            break
                if justification_matches:
                    useful_fields.add("justifications")

        for field in self._EXPLANATORY_LIST_FIELDS:
            if field in data:
                provided_fields.add(field)
                value = data[field]
                if isinstance(value, list) and all(isinstance(x, str) for x in value):
                    accepted_fields.add(field)
                    if value:
                        useful_fields.add(field)

        rejected_fields = provided_fields - accepted_fields

        # --- Decide the overall status BEFORE publishing anything.
        if not provided_fields:
            result.enrichment_status = "failed"
            result.enrichment_reason = "no_content"
            return result
        if not useful_fields:
            # Everything provided was either rejected, or accepted but
            # empty/without a single real match — nothing usable either way.
            result.enrichment_status = "failed"
            result.enrichment_reason = "invalid_response_shape" if rejected_fields else "no_content"
            return result
        if rejected_fields:
            result.enrichment_status = "partial"
            result.enrichment_reason = "some_fields_rejected"
        else:
            result.enrichment_status = "applied"
            result.enrichment_reason = None

        # --- Publish the validated candidate changes (accepted fields only).
        for criterion, new_justification in justification_matches:
            criterion.justification = new_justification
        for field in self._EXPLANATORY_LIST_FIELDS:
            if field in accepted_fields:
                setattr(result, field, data[field])

        return result
