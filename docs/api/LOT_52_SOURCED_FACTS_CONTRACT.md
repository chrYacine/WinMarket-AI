# Lot 52 — Propositions de faits sourcés (contrat)

Construit sur les contrats RAG (`LOT_51_HYBRID_RAG_CONTRACT.md`) et complétion
(`LOT_49_COMPLETION_CONTRACT.md`, §6 pour le snapshot prestataire du lot 49
bis). Le RAG trouve des passages depuis le lot 51 ; ce lot construit leur
usage **contrôlé** comme faits métier — jamais une auto-complétion, jamais un
score recalculé à partir d'un passage sans acceptation explicite.

## 1. Contrat de fait proposé

Une proposition (`src/agents/fact_search.py::FactSearchResult`) porte :
`status`, `value`, `unit`, `citation`, `reason`, `source` (provenance
kind-spécifique — voir §2), `provider`, `prompt_version`, `extracted_at`,
`search_mode` (informationnel).

États explicites (`status`) :
- `proposed` — une valeur typée, avec citation **vérifiée** contre le
  passage précis qui l'a produite.
- `absent` — le modèle a lu les passages fournis et n'y a rien trouvé
  d'exploitable. Résultat normal, jamais une erreur.
- `no_source` — rien d'éligible à chercher (besoin non déclarable/non
  couvert, ou aucun dossier/texte AO/aucune base de connaissances) : le
  formulaire de saisie manuelle reste seul disponible, **aucun appel LLM**
  n'est tenté.
- `no_candidates` — une source existe mais rien d'assez proche n'a été
  retrouvé pour solliciter le modèle.
- `llm_unavailable` — fournisseur non configuré/injoignable.
- `llm_invalid_response` — réponse malformée, citation non vérifiable,
  numéro de passage hors limites, ou valeur d'un type différent de celui
  attendu — traitée comme non fiable dans son ensemble, jamais partiellement
  acceptée (même contrat que `document_llm_support.LLMJudgment`).
- `unknown_need` (niveau route) — l'identifiant de besoin fourni ne
  correspond à aucun besoin **recalculé à neuf** pour cette analyse.

Une négation explicite dans un passage (« aucune certification ISO ») est un
`found=true` avec une valeur reflétant la négation (ex. `false` pour un fait
booléen) — jamais un `found=false` qui la ferait disparaître silencieusement
(prompt `fact_search_system.txt`).

Une valeur dont le périmètre (entité/site/période/lot) diffère clairement de
celui demandé, ou deux passages numérotés donnant des valeurs différentes et
non réconciliables, donnent `found=false` (`reason="conflicting_values"` ou
équivalent) — jamais un choix arbitraire entre les deux.

## 2. Recherche : deux sujets, deux sources, un seul appel LLM partagé

`src/agents/fact_search.py::search_fact_for_need` dispatche par `need["action"]` :

- **`declare_prestataire`** → `build_prestataire_candidates` : réutilise
  `src.rag.hybrid_search.search` (le **même** index que toute autre
  recherche privée — jamais un second accès parallèle aux documents, jamais
  limité à `evidence_pack` de l'analyse d'origine : une attestation admise
  mais jamais retenue comme référence de projet reste cherchable ici).
  Fonctionne sur SQLite (mode lexical déclaré) comme sur PostgreSQL
  (hybride ou non) — le mode réellement utilisé est renvoyé
  (`search_mode`), jamais fabriqué.
- **`declare_ao`/`declare_acheteur`** → `build_ao_candidates` : réutilise
  les pièces **du même dossier** (si l'analyse en a un — pièces admises
  uniquement, jamais une pièce exclue) ou le `texte_source` figé de l'AO
  (mode fichier unique/texte collé) — **jamais** un autre AO, **jamais** la
  base de connaissances du prestataire, **jamais** une recherche externe.
  Si aucune des deux sources n'existe, `status="no_source"` sans appel LLM.

`propose_fact` (partagé) : chaque passage candidat est numéroté et présenté
comme donnée (jamais une instruction — `wrap_untrusted_content`), plafonné à
`MAX_CANDIDATES=6` passages et `MAX_LLM_INPUT_CHARS` caractères au total
(`document_llm_support.bound_for_llm`, réutilisé, jamais réimplémenté). La
citation renvoyée est vérifiée (`document_llm_support.verify_citation`)
contre le contenu du **passage précis** que le modèle désigne
(`passage_number`) — jamais contre la concaténation entière, ce qui
empêcherait une attribution erronée d'une citation réelle à la mauvaise
provenance. Le score vectoriel/lexical ne sert **qu'à sélectionner** les
candidats montrés au modèle ; il ne certifie jamais un fait — seule la
citation vérifiée le fait, dans les limites documentées ci-dessous (§5).

## 3. Provenance (`source`), par nature de candidat

- `knowledge_document` : `document_version_id`, `chunk_id`, `start_char`,
  `end_char`, `offset_frame="passage"` (repère du chunk lui-même, jamais
  celui du document entier).
- `dossier_piece` : `dossier_id`, `piece_id`, `chunk_index`, `start_char`,
  `end_char`, `offset_frame="piece_text"` (repère du texte propre à la
  pièce, distinct du document/dossier global).
- `ao_text` : `offset_frame="ao_texte_source"`, aucun identifiant de
  document (le texte AO figé lui-même est la source).

## 4. Acceptation, correction, révision figée

Une proposition seule ne modifie **rien** (ni score, ni profil, ni
politique). Le client réaffiche la proposition avec sa citation et
accepte/rejette explicitement (`static/js/completion.js`) ; à la
soumission (`POST /api/analyze/{job}/complete`), chaque `item` peut porter
un `source_proposal` optionnel — **jamais fait confiance en l'état** :
`src/web/completion_service.py::_resolve_sourced_origin` :

1. Si `source_proposal` absent → `origin="declared_user"` (comportement lot
   49/49 bis inchangé).
2. Si la valeur soumise diffère de `source_proposal.value` → **correction
   silencieuse et normale** : `origin="declared_user"`, `source_json=None`
   — la citation d'origine n'est **jamais** présentée comme preuve d'une
   valeur qu'elle ne soutient plus.
3. Sinon, la source est **relue et revérifiée** :
   - `knowledge_document` : `knowledge_repo.get_active_chunk_by_id` (même
     règle d'autorisation que toute recherche — version encore active,
     document non supprimé) + citation encore vérifiable contre le contenu
     relu.
   - `dossier_piece` : dossier retrouvé par job, pièce encore `admitted`,
     chunk relu depuis le stockage, citation encore vérifiable.
   - `ao_text` : citation vérifiée contre `job.ao.texte_source` figé.
   - Échec à n'importe quelle étape → `409 SOURCE_CHANGED`, **aucune
     écriture partielle** (ni complément, ni révision).
   - Succès → `origin="llm_sourced"`, `source_json` gelé (citation,
     provenance complète, `provider`/`prompt_version`/`extracted_at`).

Le fait accepté est figé dans `AnalysisComplement.origin`/`source_json`
(migration additive **0016**) — jamais dans `ScoringResult` lui-même, jamais
un recalcul rétroactif d'une analyse existante. `_run_revision` (lot 49/49
bis) est **inchangé** : il ne relit jamais le profil courant ni le corpus
courant et ne relance aucune recherche — la recherche de faits précède
**toujours** la soumission, jamais exécutée dans le worker.

Une donnée prestataire acceptée peut compléter la révision sans écriture
permanente ; l'écriture dans le profil reste gardée par
`expected_profile_version` (lot 49 bis, inchangé). Une donnée AO ne rejoint
jamais le profil.

## 5. Limites documentées, jamais masquées

- **Aucune certification vérifiée** : une citation retrouvée dans un
  document (même classé « certification » par le lot 50 bis) prouve
  seulement qu'un texte l'affirme — jamais une vérification indépendante.
  L'interface distingue explicitement « citation retrouvable » d'une
  « certification indépendante ».
- **Aucune interprétation pré-validée** : la citation est vérifiée
  textuellement, jamais sémantiquement — l'utilisateur reste seul juge de
  la pertinence avant acceptation.
- **Le lot 52 ne comble aucun fait automatiquement** : une proposition
  n'est jamais appliquée sans action explicite ; un résultat INCOMPLET reste
  INCOMPLET tant qu'aucune proposition n'est acceptée.
- **Aucun nouveau seuil vectoriel inventé** : la sélection des candidats
  réutilise le retrieval existant (lexical/hybride) sans nouvelle note de
  pertinence ; seule la vérification de citation décide qu'une proposition
  est retenue.
- **Recette locale du corpus utilisateur réel** : jamais qualifiée avec un
  appel LLM réel dans ce lot (autorisation non redemandée) — voir
  `docs/qa/lot_52_20260925/RAPPORT.md` §Corpus A.
