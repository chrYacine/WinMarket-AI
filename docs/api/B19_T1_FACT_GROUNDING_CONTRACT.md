# B19-T1 — Fact grounding of generated candidature content

Scope: `src/livrables/document_generator.py`. The LLM may WRITE the prose;
every fact it states must trace back to something the server actually knows.
This is **not** a full semantic fact-checker — it removes two concrete
fabrication defects and adds one checkable consistency guard.

## Where the prompts live

All four are plain `.txt` templates loaded through the single shared loader
`src/core/prompt_loader.py::load_prompt` (`{{KEY}}` placeholders), the same
mechanism used by `src/rag/prompts/` (B18-T3) and `src/agents/prompts/`
(B05-T2) — never a second ad hoc loader, never an inline f-string prompt.

| File | Used for |
|---|---|
| `src/livrables/prompts/document_system.txt` | system prompt; `{{IDENTITY}}` |
| `src/livrables/prompts/document_no_go.txt` | `decision == "GO"` is false AND `"RESERVE" not in decision` |
| `src/livrables/prompts/document_reserve.txt` | `"RESERVE" in decision` |
| `src/livrables/prompts/document_go.txt` | `decision == "GO"` exactly |

Integration fix (post-B19-T1, when B06-T4 introduced the `"INCOMPLET"`
decision value for a scoring result missing a required business rule):
the NO-GO template's condition is deliberately **not** `decision ==
"NO-GO"` — it is "anything that isn't clearly GO or a RESERVE variant".
A confident GO dossier must never be the silent catch-all for an
unrecognized or incomplete decision value; the most conservative
existing template is used instead. Real `"NO-GO"` still matches exactly
as before. This same three-way condition is applied consistently across
`_build_prompt`, `generate_docx`'s header, and its "CAS NO-GO" section
start — `generate_pdf` has no equivalent structural branch (see below).

`_build_context` stays a Python method: it assembles DATA (AO fields, scoring
values, retained RAG sources), not instructions.

## Company identity (defect 1)

The old class constant `_DOC_SYSTEM` asserted a fictional specialization
("une ESN française spécialisée en transformation digitale, développement
applicatif, data/IA, cloud et cybersécurité") for whichever account was
running. It is gone. `DocumentGenerator._build_doc_system_prompt(provider_profile)`
now fills `{{IDENTITY}}`:

- **profile with a `raison_sociale`** → that real name plus the account's own
  declared `competences`, and an explicit order to attribute nothing else
  (no headcount, ancienneté, or past achievement).
- **no profile, or no `raison_sociale`** → a deliberately specific-free
  sentence that names no company, no size and no specialization.

`_generate_ai_content(ao, result, llm, provider_profile=None)` — the keyword
defaults to `None`, so the frozen `src/core/pipeline.py` call site is
unchanged. `src/web/jobs.py` passes the account's `ProviderProfile` row.

## Anti-invention instruction (defect 2)

Each of the three bodies carries, right after its MISSION framing:

> RÈGLE FACTUELLE : N'invente aucun chiffre, certification, référence client,
> réalisation passée ou fait qui ne figure pas explicitement dans le contexte
> ci-dessus. Si une information n'est pas disponible, formule autour de ce qui
> EST disponible plutôt que de combler le vide par une supposition.

## Certification consistency guard

Applied by `_drop_ungrounded_fields` to the raw dict from the single
`llm.json_complete(...)` call, before it is returned or used.

- **Known-real set** = `ao.certifications_obligatoires` ∪ (when a profile is
  given) the `"nom"` of each entry in `provider_profile.certifications`. Each
  raw string is kept lowercased **and** canonicalized through
  `src/agents/certification_scope.py::_NAME_PATTERNS`, so a free-typed
  `"iso27001"` grounds a generated `"ISO 27001"`.
- **Detection** uses that same `_NAME_PATTERNS` vocabulary — the one the
  extraction pipeline uses. No second copy is defined here. `"PRIS"` alone is
  matched case-sensitively: its pattern `\bpris\b` otherwise matches the
  ordinary French past participle.
- **Failure mode**: any string value, or any string inside a list value, that
  names a certification outside the known-real set makes the **whole field**
  drop out of the returned dict. `generate_docx` / `generate_pdf` then hit
  their pre-existing `ai.get(field) or <neutral fallback>` branch. The drop is
  logged server-side (`winmarket.agents.document_generator`, WARNING, with the
  field name and the ungrounded certification) and is never surfaced in the
  document.

Everything else in the free prose stays governed only by the existing shape
handling plus the prompt instruction above.

## Server-controlled structured facts

`result.decision`, `result.score_global`, `ao.client`, `ao.titre`,
`ao.secteur`, `result.scoring_missing` (surfaced in the NO-GO-shaped
fallback synthesis paragraph when `decision == "INCOMPLET"`) and every
`result.criteres` entry (`nom`/`poids`/`score`/`justification`) are read
directly from the server's own objects in both
`generate_docx` (header, "Scoring détaillé", "Évaluation de l'adéquation",
"Réserves et conditions") and `generate_pdf` (header, "Évaluation détaillée
par critère"). No `ai_content` key is ever substituted for one of them — the
generated text only supplies prose sections alongside them. If the model
returns a `decision`/`score_global` key of its own, nothing reads it.
