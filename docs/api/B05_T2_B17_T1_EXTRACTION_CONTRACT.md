# B05-T2 / B17-T1 — Contrat : extraction d'AO fiable et obligations de certification à la bonne portée

Destiné au développeur frontend/backend consommant `AOContext`. **Aucun écran n'est requis pour ces tickets.**

## B05-T2 — `deadline_reponse` devient optionnel

**Défaut confirmé** : le prompt d'extraction autorisait `deadline_reponse: null`, mais `AOContext.deadline_reponse` était `str = ""` (jamais `null`). Une réponse LLM avec une date absente faisait échouer la construction PYDANTIC ENTIÈRE (`AOContext(**data)`), perdant silencieusement TOUS les autres champs déjà valides (client, budget, durée, certifications) et forçant le repli local complet.

```json
{"deadline_reponse": null}
```
`deadline_reponse: Optional[str] = None` désormais. Tous les consommateurs existants (`ao.deadline_reponse or "..."`, `if ao.deadline_reponse:`) traitent déjà `None` exactement comme l'ancien `""` — aucun changement de comportement pour eux.

## B05-T2 — Résolution par champ : `field_provenance`, `extraction_status`, `extraction_reason`

Chaque champ d'`AOContext` est désormais validé et résolu INDÉPENDAMMENT — un champ malformé ne détruit plus les autres.

```json
{
  "budget_estime": 300000.0,
  "deadline_reponse": null,
  "field_provenance": {"titre": "llm", "client": "llm", "budget_estime": "llm", "deadline_reponse": "absent", "duree_projet_mois": "fallback", "competences_requises": "absent"},
  "extraction_status": "llm_partial",
  "extraction_reason": "some_fields_rejected"
}
```

| `field_provenance[champ]` | Signification |
|---|---|
| `"llm"` | Valeur validée venant directement du modèle (une liste vide `[]` explicite compte comme `"llm"` — "rien détecté" est une réponse, pas une absence de vérification) |
| `"fallback"` | Le modèle n'a rien fourni ou une valeur invalide, mais un repli local (regex/heuristique) a trouvé quelque chose dans le texte |
| `"rejected"` | Le modèle a fourni une valeur pour ce champ, mais elle était invalide (mauvais type, booléen, NaN...), ET aucun repli local n'a rien trouvé non plus |
| `"absent"` | Ni le modèle ni un repli local n'ont rien trouvé — cas normal et fréquent |

**Garanties** : `budget_estime = 0` est distinct de `budget_estime` absent (`null`) — jamais confondu par une vérification de vérité (`if value`). Un booléen ou une valeur non finie (NaN/Infinity) est toujours rejeté, jamais silencieusement coercé. La réponse brute du fournisseur LLM n'est jamais mutée. `competences_requises` n'a **aucun** repli local (jamais substitué par la liste `technologies_demandees` — ce sont deux notions différentes) : sans réponse valide du modèle, ce champ reste une liste vide, provenance `"absent"`.

`extraction_status` : `"llm_full"` (tous les champs `"llm"`) | `"llm_partial"` (mélange, `extraction_reason="some_fields_rejected"`) | `"fallback_local"` (LLM désactivé/aucune réponse/réponse non-objet — `extraction_reason` vaut `"llm_disabled"` | `"no_content"` | `"invalid_response_shape"`) | `"unknown"` (résultat persisté avant ce ticket).

**`extraction_conflicts`** (`List[str]`) : noms de champs où une valeur LLM validée ET UTILISÉE diverge d'un détecteur local existant (`technologies_demandees`, `certifications_obligatoires`) — signalé, jamais résolu automatiquement, jamais utilisé pour écraser la valeur LLM.

Ce champ est **indépendant** de `enrichment_status`/`data_integrity`/`rag_selection_status` du scoring (`docs/api/B06_T3_ENRICHMENT_STATUS_CONTRACT.md`) — ne décrit que l'étape d'EXTRACTION de l'AO, jamais le scoring.

## B17-T1 — `certification_mentions` / `certification_contradictions`

**Défaut confirmé** : une négation appliquée à toute la LIGNE pouvait annuler une obligation énoncée dans une clause différente de cette même ligne. Exemple : `"ISO 27001 obligatoire ; Qualiopi non obligatoire"` faisait perdre ISO 27001 alors qu'elle était bien obligatoire. Corrigé par une analyse à la portée de la CLAUSE (séparée par `;`/`mais`/une nouvelle ligne — jamais par une simple virgule, qui rejoint souvent une liste coordonnée partageant un seul verbe).

```json
{
  "certifications_obligatoires": ["ISO 27001"],
  "certification_mentions": [
    {"name": "ISO 27001", "verdict": "obligatoire", "clause": "ISO 27001 obligatoire"},
    {"name": "Qualiopi", "verdict": "non_obligatoire", "clause": "Qualiopi non obligatoire"}
  ],
  "certification_contradictions": []
}
```

`certification_mentions` : CHAQUE occurrence, jamais fusionnée par nom — `verdict` parmi `"obligatoire"` | `"non_obligatoire"` | `"ambigu"` (formulation conditionnelle : "pourrait", "sous réserve", ...). `clause` est le texte source exact (jamais reformulé), préservant une alternative comme "ou équivalent".

`certification_contradictions` : noms dont les mentions se contredisent (une clause dit obligatoire, une autre dit le contraire, ou le scope reste indéterminé) — ce nom est **exclu** de `certifications_obligatoires` (aucune résolution inventée), jamais silencieusement tranché dans un sens ou l'autre.

**Ce mécanisme local tourne TOUJOURS**, indépendamment de la provenance de `certifications_obligatoires` (LLM ou repli) — il ne l'écrase jamais, il ne fait qu'exposer les preuves sous-jacentes (`field_provenance["certifications_obligatoires"]` reste inchangé par B17-T1).

## Persistance et compatibilité

Aucune migration SQL pour l'un ou l'autre ticket — tous les nouveaux champs sont additifs sur `AOContext`, propagés via `Job.ao`/`Analysis.result_data` (JSON déjà flexible), exactement comme les champs sœurs déjà établis pour `ScoringResult`. Un `AOContext` persisté avant ces tickets se recharge avec `field_provenance={}`, `extraction_status="unknown"`, `extraction_conflicts=[]`, `certification_mentions=[]`, `certification_contradictions=[]` — jamais un historique d'extraction inventé après coup.
