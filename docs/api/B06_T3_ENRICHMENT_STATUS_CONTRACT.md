# B06-T3 / B18-T2 / B18-T3 — Contrat : états d'enrichissement, d'intégrité et de sélection RAG du scoring

Destiné au développeur frontend. **Aucun écran n'est requis pour ces tickets** — objectif : pouvoir un jour afficher « analyse disponible, enrichissement indisponible/partiel », « analyse disponible malgré une preuve dégradée » et « références sélectionnées automatiquement/repli » sans redemander de décision produit sur les noms/valeurs, qui sont déjà figés ci-dessous.

## Où le trouver

Deux nouveaux champs sur tout objet `ScoringResult` sérialisé (JSON d'un job/historique, `Analysis.result_data`) :
```json
{"decision": "...", "score_global": ..., "...": "...", "enrichment_status": "applied", "enrichment_reason": null}
```
Portée : **uniquement l'étape d'enrichissement LLM du scoring** (`ScoringEngine.enrich_with_llm`) — ne dit rien des autres appels LLM du pipeline (extraction de l'AO, génération des documents).

## Valeurs de `enrichment_status`

| Valeur | Signification | `enrichment_reason` possible |
|---|---|---|
| `not_attempted` | Résultat neuf jamais soumis à l'enrichissement, OU LLM désactivé (aucun appel réseau effectué) | `null` (jamais tenté) ou `"llm_disabled"` |
| `applied` | Au moins un champ explicatif autorisé et utile appliqué, aucun champ fourni rejeté | `null` |
| `partial` | Au moins un champ valide appliqué ET au moins un autre champ fourni par le LLM rejeté (forme invalide) | `"some_fields_rejected"` |
| `failed` | Exception fournisseur, réponse de forme invalide, ou aucun contenu utilisable — **aucun champ métier modifié** | `"provider_exception"` \| `"invalid_response_shape"` \| `"no_content"` |
| `unknown` | Résultat persisté avant ce ticket, sans cette métadonnée — ne pas lui inventer un historique | `null` |

`enrichment_reason` est toujours un code fixe et sûr parmi la liste ci-dessus (ou `null`) — **jamais** un message d'exception, une clé de payload, ou du contenu de prompt.

## Garanties (valables pour `failed` ET `partial`)

- `decision`, `score_global`, le poids et le score de chaque critère, `criteres_bloquants`, `company_profile`, `capacity`, `evidence_pack` — **jamais modifiés** par l'enrichissement, quel que soit son état.
- Une réponse LLM contenant des clés étrangères au schéma (`decision`, `score_global`, ...) : ces clés sont ignorées, jamais appliquées, et ne comptent ni comme champ accepté ni comme champ rejeté pour le calcul du statut.
- `failed` : zéro champ métier explicatif modifié — le résultat algorithmique reste identique, champ par champ, à un résultat jamais enrichi (seuls `enrichment_status`/`enrichment_reason` diffèrent).
- `partial` : les champs explicatifs **valides** fournis sont réellement appliqués (ce n'est **pas** un repli intégral vers l'état d'avant enrichichissement) — seuls les champs rejetés restent inchangés. Chaque champ est validé dans son intégralité avant d'être appliqué (jamais une moitié de structure incohérente).

## Persistance et compatibilité

Aucune migration SQL : `Analysis.result_data` est déjà une colonne JSON flexible, les nouveaux champs y apparaissent simplement dès qu'une analyse est (re)calculée. Un résultat déjà stocké avant ce ticket se recharge avec `enrichment_status="unknown"` automatiquement (valeur par défaut du modèle), sans qu'aucun score ne soit réécrit à cette occasion.

## Suggestion d'affichage (non contraignante, aucun design imposé)

- `applied` : rien de spécial à afficher, ou un badge discret "enrichi".
- `partial` : "Analyse disponible — enrichissement partiel."
- `failed` / `not_attempted` (llm_disabled) : "Analyse disponible — enrichissement indisponible." Le résultat métier (score, décision) reste toujours pleinement exploitable dans les deux cas.
- `unknown` : ne rien afficher de spécifique (résultat antérieur à cette fonctionnalité).

---

## B18-T2 — `data_integrity` / `data_integrity_reason` (champs sœurs, DEFECT-B04-04)

Deux champs supplémentaires sur `ScoringResult`, **indépendants** de `enrichment_status` — celui-ci décrit l'enrichissement LLM, `data_integrity` décrit la fiabilité **numérique** des données du résultat lui-même (preuves RAG, score global, scores de critères) :
```json
{"decision": "...", "score_global": ..., "...": "...", "data_integrity": "degraded", "data_integrity_reason": "invalid_evidence_removed"}
```

| Valeur de `data_integrity` | Signification | `data_integrity_reason` |
|---|---|---|
| `ok` | Vérifié activement (jamais supposé) — aucune preuve invalide, `score_global`/scores de critères tous finis | `null` |
| `degraded` | Une ou plusieurs preuves de `evidence_pack` étaient invalides et ont été retirées à la relecture d'une analyse antérieure à ce correctif — `decision`/`score_global`/critères sont les valeurs **exactement stockées**, jamais recalculées | `"invalid_evidence_removed"` |

**Ne jamais présenter un résultat `degraded` comme entièrement vérifié** — c'est précisément ce que ce champ existe pour éviter (un simple log serveur ne suffisait pas, il fallait un signal exposé au consommateur de l'API). Suggestion d'affichage : "Certaines références de cette analyse n'ont pas pu être vérifiées — le score affiché reste celui calculé à l'origine."

### Cas plus grave : `score_global`/un score de critère lui-même non fini

Si l'ancien défaut a contaminé `score_global` ou le `score`/`poids` d'un critère (pas seulement une preuve), l'analyse concernée n'est **pas** reconstruite : `GET /api/analyze/{job_id}/status` renvoie
```json
{"status": "error", "error": "Cette analyse historique contient une valeur numérique invalide et ne peut plus être affichée de façon fiable. Contactez le support si vous avez besoin de la retrouver.", "error_code": "historical_score_unavailable"}
```
Jamais de `NaN`/`Infinity` dans une réponse JSON, jamais une valeur de repli favorable. **Seule cette analyse est concernée** — toute autre analyse historique (y compris celles du même compte) reste consultable normalement, avec les mêmes contrôles de propriétaire qu'aujourd'hui.

### Persistance

Aucune migration SQL. `data_integrity`/`data_integrity_reason` sont recalculés à **chaque lecture** d'un résultat persisté avant ce correctif (jamais mis en cache dans le fichier/la ligne d'origine, jamais supposés "ok" par défaut sans vérification) — un résultat calculé après ce correctif est toujours `"ok"` par construction (`ensure_valid_evidences` a déjà validé les preuves avant le calcul).

---

## B18-T3 — `rag_selection_status` / `rag_selection_reason` (DEFECT F12/E-4)

Troisième champ sœur, indépendant des deux précédents — décrit uniquement l'étape de **sélection** des références RAG (laquelle des preuves retrouvées sont réellement retenues avant transmission au moteur de scoring), pas leur ordre d'affichage ni leur contenu :
```json
{"decision": "...", "score_global": ..., "evidence_pack": [...], "rag_selection_status": "applied", "rag_selection_reason": null}
```

| Valeur | Signification | `rag_selection_reason` possible |
|---|---|---|
| `not_attempted` | LLM désactivé, ou aucune preuve candidate à sélectionner — zéro appel | `null` |
| `applied` | Réponse valide appliquée — **inclut une sélection vide** (aucune référence jugée pertinente est un résultat normal, pas un échec) | `null` |
| `fallback` | Réponse invalide ou exception fournisseur — **toutes** les preuves candidates initiales sont conservées telles quelles, aucune sélection partielle | `"provider_exception"` \| `"no_content"` \| `"invalid_response_shape"` \| `"missing_selected_ids"` \| `"selected_ids_not_a_list"` \| `"invalid_id_type"` \| `"unknown_id"` |
| `unknown` | Résultat persisté avant ce ticket, sans cette métadonnée | `null` |

**Correctif de fond (défaut F12/E-4 confirmé)** : avant ce ticket, une référence explicitement exclue par le modèle (absente de sa liste) était quand même réinjectée en fin de traitement — la sélection n'avait aucun effet réel. `evidence_pack` ne contient désormais QUE les références effectivement retenues lors d'un `applied` ; en cas de `fallback`, `evidence_pack` correspond aux preuves candidates initiales, inchangées.

Un identifiant renvoyé par le modèle n'est résolu que parmi les candidats réellement présentés à CET appel (au plus 6) — un identifiant hors de cette liste (par exemple 7 ou 8 si 8 preuves ont été retrouvées mais seules 6 présentées) est traité comme invalide, jamais résolu contre une preuve plus large ou une autre organisation/compte.

**Une synthèse vide accompagne systématiquement une sélection vide ou une réponse rejetée** — même si le modèle a renvoyé du texte dans ce cas, il est ignoré. La vérification factuelle du contenu de `rag_synthesis` (la prose elle-même, pas la liste d'identifiants) reste hors périmètre de ce champ — voir B19.

### Persistance

Aucune migration SQL. Champ recalculé/renseigné à chaque analyse ; un résultat antérieur à ce ticket se recharge avec `rag_selection_status="unknown"` automatiquement, sans recalcul ni changement de décision.

---

## B18-T4 — `duplicate_sources` (par preuve, DEFECT F09/F12/E-5)

Chaque élément de `evidence_pack` porte désormais un champ `duplicate_sources` (liste de chaînes, vide par défaut) :
```json
{"evidence_pack": [{"source": "renamed_copy.md", "score": 0.9, "content": "...", "duplicate_sources": ["original.md"]}], "...": "..."}
```
Une copie exacte (même contenu intégral, avant troncature) d'une autre preuve n'apparaît plus comme une entrée séparée dans `evidence_pack` — la meilleure similarité validée parmi les copies est conservée sous UNE seule entrée, dont `duplicate_sources` liste les autres sources fusionnées (traçabilité uniquement : les fichiers ne sont ni supprimés, ni leur propriété modifiée). `n_ev`/similarité moyenne du critère « Références similaires » sont calculés sur ces unités uniques — répéter une preuve ne modifie plus ce critère, `score_global`, ni la décision.

**Limite explicite, à ne jamais présenter comme résolue** : seule l'identité de CONTENU EXACT est reconnue (empreinte serveur du texte intégral). Deux documents décrivant le même projet réel avec des mots différents restent deux entrées distinctes — aucun rapprochement par LLM, titre approchant ou similarité floue n'est fait par ce mécanisme.

### Persistance

Aucune migration SQL — `duplicate_sources` est un champ additif sur `RAGEvidence`, propagé comme tout le reste via `Analysis.result_data` (JSON déjà flexible). Un résultat antérieur à ce ticket se recharge avec `duplicate_sources: []` par défaut sur chaque preuve, sans recalcul des scores déjà stockés.

---

## B18-T5 — `document_version_id` / `content_fingerprint` / `start_char` / `end_char` (par preuve, DEFECT F11/F12/E-6)

Quatre champs supplémentaires sur chaque élément de `evidence_pack`, tous optionnels (`null` par défaut) :
```json
{"evidence_pack": [{
  "source": "ref.md", "score": 0.62, "content": "…passage localisé…",
  "start_char": 4021, "end_char": 4780,
  "content_fingerprint": "3f9c…", "document_version_id": "b1e4b6b0-…",
  "duplicate_sources": []
}]}
```

**Défaut corrigé** : `content` n'est plus les 3500 premiers caractères aveugles du document, mais un **extrait localisé** — le passage jugé le plus pertinent pour la requête au sein du texte intégral, qui peut se situer n'importe où dans le document. Le prompt de reranking (`src/rag/prompts/reference_selection.txt`) n'applique plus de troncature supplémentaire à 800 caractères sur cet extrait : le second demi-défaut audité (perte d'un passage pourtant correctement retrouvé) est fermé en même temps que le premier.

| Champ | Signification |
|---|---|
| `start_char` / `end_char` | Intervalle `[start, end)` en caractères Unicode du texte canonique intégral indexé — invariant garanti par construction : `content == texte_canonique[start_char:end_char]`. `null`/`null` pour une preuve sans localisation connue (chemin historique/Streamlit, ou résultat antérieur à ce ticket) — jamais une position inventée. |
| `content_fingerprint` | Réutilise l'empreinte serveur B18-T4 (`src/core/reference_identity.py`) du texte **intégral, avant toute localisation/troncature** — c'est cette valeur, jamais `content` seul, qui sert de clé de déduplication (voir plus bas). |
| `document_version_id` | Identifiant de la `KnowledgeDocumentVersion` d'origine (chemin privé/SaaS uniquement, `src/rag/private_rag_manager.py`) — toujours `null` pour le chemin historique/Streamlit (`src/rag/rag_manager.py`), qui n'a pas cette notion ; jamais fabriqué. |

**`score` reste la similarité document-niveau** (cosinus TF-IDF calculé sur le texte intégral) — jamais remplacée par une pertinence de passage, qui n'existe qu'en interne à la localisation et n'est exposée nulle part comme un score concurrent.

### Déduplication : jamais recalculée sur le seul extrait

Point de vigilance corrigé pendant ce ticket : `deduplicate_evidences` (moteur de scoring, `src/agents/scoring_engine.py`) regroupe désormais par `content_fingerprint` (identité du document complet) quand ce champ est renseigné, et seulement par hash de `content` en repli (preuves construites sans ce champ, notamment certains fixtures de test antérieurs à ce ticket). Deux documents différents partageant un passage localisé identique restent deux entrées distinctes ; à l'inverse, une copie renommée du même document reste fusionnée même si son passage localisé diffère de celui de l'original.

### Persistance et compatibilité

Aucune migration SQL — les quatre champs sont additifs sur `RAGEvidence`, propagés via `Analysis.result_data` (JSON déjà flexible) exactement comme `duplicate_sources` (B18-T4). Un résultat persisté avant ce ticket se recharge avec les quatre champs à `null` — une « provenance indisponible » explicite, jamais une position ou une empreinte recalculée après coup. Une analyse déjà terminée conserve l'extrait qu'elle a réellement utilisé même si le document source est modifié ensuite (nouvelle version) : la relecture ne relance jamais de recherche, elle ne fait que désérialiser ce qui a été stocké au moment du calcul.

---

## B18-T6 — bornage du bloc documentaire de sélection (complément de B18-T5, lié à B15)

**Écart corrigé** : en supprimant la retroncature `[:800]`, B18-T5 avait rouvert le budget textuel que ce plafond imposait implicitement — avec `content` pouvant atteindre 3500 caractères et jusqu'à 6 candidats, le bloc assemblé pouvait atteindre 21000 caractères au lieu des ~4800 prévus. `src/rag/context_budget.py` centralise et valide trois limites (au plus `RAG_SELECTION_MAX_CANDIDATES` candidats, `RAG_SELECTION_MAX_EXCERPT_CHARS` caractères par extrait, `RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS` caractères pour le bloc assemblé, en-têtes compris — 6/800/4800 par défaut, `docs/api` comme `src/core/config.py`). **Ce sont des plafonds de texte, jamais un compte de tokens ni un coût réel.**

### Effet sur `content` / `start_char` / `end_char` d'un `evidence_pack` en `rag_selection_status="applied"`

Pour une sélection réussie, ces trois champs décrivent désormais l'**extrait effectivement présenté au LLM** (une seule représentation bornée, jamais une seconde copie parallèle « ce qui a été montré ») :
```json
{"evidence_pack": [{"source": "ref.md", "content": "…extrait ≤ 800 caracteres…", "start_char": 4021, "end_char": 4780, "content_fingerprint": "3f9c…"}], "rag_selection_status": "applied"}
```
L'invariant `content == texte_canonique[start_char:end_char]` reste garanti — les positions sont retraduites en coordonnées du texte canonique intégral, jamais relatives au passage T5 intermédiaire. `content_fingerprint`/`document_version_id`/`duplicate_sources`/`score` ne sont **jamais** recalculés à ce stade : l'identité documentaire et la similarité restent celles du texte intégral, jamais celles de l'extrait réduit.

En `rag_selection_status="fallback"` ou `"not_attempted"`, ce comportement est **inchangé** : `evidence_pack` reste les preuves d'origine telles que produites par B18-T5 (jusqu'à 3500 caractères), sans bornage supplémentaire — un repli conserve volontairement le contexte le plus riche disponible, la borne de prompt ne s'applique qu'à ce qui est réellement envoyé lors d'une tentative de sélection.

### Persistance et compatibilité

Aucune migration SQL, aucun nouveau champ — B18-T6 ne fait que borner plus tôt les valeurs déjà existantes de `content`/`start_char`/`end_char` avant leur persistance, uniquement sur le chemin `applied`. Un résultat antérieur à ce ticket n'est pas affecté rétroactivement (aucun recalcul à la lecture).
