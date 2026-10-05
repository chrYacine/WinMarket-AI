# Lot 51 — RAG hybride (lexical + vectoriel), contrat technique

Additif : ne remplace ni ne modifie le RAG lexical existant (`src/rag/private_rag_manager.py`,
inchangé) ni le reranking LLM existant (`src/rag/semantic_rerank.py`, inchangé). Réutilise B03
(admission/versionnement des documents privés) — aucun second pipeline d'admission.

## 1. Portée structurelle : quand le mode hybride existe-t-il ?

Le mode vectoriel n'est JAMAIS actif sauf si **les deux conditions suivantes sont vraies en même
temps** :
1. Le dialecte de la base cible est `postgresql` (`db.get_bind().dialect.name`).
2. `config.RAG_HYBRID_MODE_ENABLED` vaut `True` (variable d'environnement `RAG_HYBRID_MODE_ENABLED`,
   défaut `false` — jamais activé silencieusement par un simple changement de `DATABASE_URL`).

Sur SQLite, ou sur PostgreSQL sans ce drapeau, `src/rag/hybrid_search.py::search()` retourne
**exactement** le résultat de `private_rag_manager.search()` (mode `"lexical"`), inchangé bit à
bit par rapport à avant ce lot.

## 2. Modèle d'embeddings

`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, servi via `fastembed` (ONNX,
`onnxruntime`, **sans torch**) — choix justifié dans `docs/qa/lot_51_20260924/RAPPORT_CLOTURE.md`
§1. Dimension 384, Apache-2.0. Configurable via `EMBEDDING_MODEL_ID` / `EMBEDDING_MODEL_REVISION` /
`EMBEDDING_DIMENSION` (`src/core/config.py`) — un changement de l'un de ces trois invalide (rend
« périmé ») toute version déjà indexée (`src/rag/hybrid_index.py::version_is_stale`), jamais mélangé
silencieusement (filtré au niveau passage, pas seulement au niveau version, dans
`hybrid_search._vector_candidates`). Cache local sous `EMBEDDING_CACHE_DIR` (jamais le dossier temp
système). Aucun téléchargement au démarrage de l'application — chargement paresseux, au premier
appel réel à `EmbeddingAdapter.shared().embed(...)`.

## 3. Schéma (migration 0015, additive)

- `knowledge_chunks` : `UniqueConstraint(id, organization_id, owner_user_id)` ajoutée (même
  garantie composite que le reste de la chaîne).
- `knowledge_document_versions` : `embedding_status` (`not_applicable|pending|ready|failed`,
  défaut `not_applicable`), `embedding_model_id`, `embedding_model_revision`, `embedding_dimension`,
  `embedding_error_code`, `embedding_indexed_at`. Portable, existe sur tous les dialectes.
- `knowledge_passages` (nouvelle table, **portable** — existe sur SQLite ET PostgreSQL, mais reste
  vide sur SQLite puisque l'indexation ne s'y déclenche jamais) : une fenêtre d'un chunk existant
  (`chunk_id`, FK composite scope), `start_char`/`end_char`/`content` (`content ==
  chunk.content[start:end]`, jamais une copie qui pourrait diverger), `content_fingerprint`,
  `embedding_model_id/_revision/_dimension` (par PASSAGE, pas seulement par version — filtre anti-
  mélange redondant).
- **PostgreSQL uniquement** : `CREATE EXTENSION IF NOT EXISTS vector` puis colonne
  `knowledge_passages.embedding vector(384)`, ajoutée par SQL brut conditionné au dialecte, jamais
  déclarée dans le modèle ORM (`src/web/database/models.py::KnowledgePassage`, voir sa docstring) —
  aucune dépendance à `pgvector` n'est nécessaire pour faire tourner l'application sur SQLite.
  Aucun index ANN (ivfflat/hnsw) — balayage séquentiel avec l'opérateur `<=>`, suffisant à cette
  échelle de corpus.

## 4. Indexation (`src/rag/hybrid_index.py`)

Synchrone, à l'intérieur de la même transaction que l'ingestion existante
(`documents_service._ingest_version`), best-effort exactement comme la classification de contenu
du lot 50 bis §2 : un échec (`EmbeddingUnavailableError`) met `embedding_status='failed'` +
`embedding_error_code`, **sans jamais bloquer** la version lexicalement exploitable/`ready`.

- Découpage (`src/rag/chunking.py::window_passages`) : fenêtres de `EMBEDDING_CHUNK_WINDOW_CHARS`
  caractères, chevauchement `EMBEDDING_CHUNK_OVERLAP_CHARS`, appliqué SUR le contenu d'un
  `KnowledgeChunk` existant (jamais un re-découpage du document entier) — un chunk plus court que la
  fenêtre devient une seule passage.
- Réindexation (`index_version`, aussi exposée via `POST
  /api/knowledge/documents/{id}/versions/{id}/reindex`) : **idempotente** — supprime d'abord les
  passages existants de la version (`delete_passages_for_version`), puis réécrit un lot complet ;
  `embedding_status` ne passe à `'ready'` **qu'après** que CHAQUE passage a un vecteur valide —
  jamais un index partiellement visible en recherche.
- Une version encore `pending`/`failed`/périmée ne remplace JAMAIS silencieusement la dernière
  version lexicalement recherchable : le lexical (`extraction_status`/`active_version_id`) est géré
  entièrement indépendamment de l'état d'indexation vectorielle.
- Un résultat d'indexation tardif d'une version supprimée/remplacée ne peut jamais la
  « ressusciter » : la recherche vectorielle (`_vector_candidates`) filtre toujours sur
  `d.active_version_id = v.id` au moment de CHAQUE requête, jamais sur un état mis en cache.

## 5. Recherche hybride (`src/rag/hybrid_search.py`)

1. Le signal lexical (`private_rag_manager.search`, top `RAG_HYBRID_TOP_K_LEXICAL`) est **toujours**
   calculé en premier, y compris quand le mode hybride est inactif — c'est le point d'extension déjà
   substituable par les tests existants (`monkeypatch.setattr(private_rag_manager, "search", ...)`).
2. Si le mode hybride est inactif → résultat lexical seul, mode `"lexical"`.
3. Sinon : embedding de la requête (`EmbeddingAdapter.shared().embed_one`) ; en cas d'échec →
   repli lexical explicite, mode `"hybrid_degraded_vector_unavailable"`, `degraded_reason` renseigné
   — **jamais un « zéro résultat » silencieux se faisant passer pour une réponse normale**.
4. Sinon : candidats vectoriels (`_vector_candidates`, plus proches voisins par `<=>`, filtrés sur
   la configuration d'embedding courante et l'autorisation active), fusionnés avec les candidats
   lexicaux par **Reciprocal Rank Fusion** (`RAG_RRF_K`, défaut 60).
5. **Identité de fusion = le `KnowledgeChunk`**, jamais le passage : plusieurs passages d'un même
   chunk, ou un chunk trouvé par les deux signaux, ne comptent qu'une seule fois
   (`RAGEvidence.chunk_id`, nouveau champ additif — jamais confondu avec `document_version_id`, qui
   identifie une version entière, pas un chunk précis).
6. `RAGEvidence.score` reste **toujours** la définition lexicale existante (cosinus TF-IDF),
   recalculée sur l'ensemble final des candidats retenus, quel que soit le signal qui les a trouvés
   — un candidat trouvé uniquement par le vecteur peut légitimement avoir un score lexical proche de
   zéro (limite documentée, jamais masquée ni renormalisée).
7. `'empty_corpus'` n'est décidé qu'en dernier ressort, sur le résultat final vide, via une lecture
   **sans effet de bord** (`knowledge_repo.active_chunk_ids_for_corpus` — jamais
   `private_rag_manager.corpus_is_empty`, qui crée un `KnowledgeCorpus` comme effet de bord de
   `get_or_create_corpus` : cela avait cassé l'isolation « un dossier d'AO ne touche jamais la base
   de connaissances », voir RAPPORT_CLOTURE.md §2 pour le défaut réel trouvé et corrigé).

## 6. Points de branchement réels (le « vrai parcours »)

- `src/web/jobs.py::_run_analysis` — la recherche RAG d'une analyse réelle (`search_evidences`)
  utilise désormais `hybrid_search.search_evidences`, plus `private_rag_manager.search` en direct.
- `src/web/routes_scoring_policy.py` (`/api/scoring-config/policy/simulate`) — idem.
- `GET /api/knowledge/search` — expose `mode`/`degraded_reason` en plus de `results`/`corpus_empty`.
- `POST /api/knowledge/documents/{id}/versions/{id}/reindex` — nouvelle route, `knowledge:write`,
  CSRF requis ; no-op honnête (`embedding_status` inchangé) si le mode hybride est structurellement
  inactif.
- `static/js/knowledge.js` — affiche l'état d'indexation par document (`embeddingBlock`), un bouton
  « Réessayer l'indexation sémantique » sur échec/attente, et le mode de recherche réellement exécuté
  au-dessus des résultats (`SEARCH_MODE_LABEL`), avec un bandeau d'avertissement visible en cas de
  dégradation.

## 6 bis. Corrections du lot 51 bis (vérification des frontières RAG/scoring)

Une relecture ciblée du code (pas des bugs déclarés sans lecture) a confirmé plusieurs écarts réels
entre ce contrat et le comportement effectif — tous corrigés, avec preuve réelle (`pgserver` +
`fastembed`, aucun mock du moteur) :

1. **Limite de tokens réellement vérifiée, pas supposée.** La fiche Sentence-Transformers annonce
   `max_seq_length=128` ; vérifié empiriquement que la conversion ONNX réellement chargée
   (`qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`) applique bien cette même limite (128
   tokens, dont 2 spéciaux — 126 tokens de contenu réels), via `fastembed`'s propre
   `enable_truncation`. §4 (« fenêtres de `EMBEDDING_CHUNK_WINDOW_CHARS` caractères ») est
   **remplacé** : le découpage est désormais fait par tokens réels
   (`src/rag/embeddings.py::EmbeddingAdapter.content_token_offsets`,
   `src/rag/chunking.py::window_passages_by_tokens`) — jamais par un nombre de caractères pouvant
   dépasser silencieusement ce que le modèle voit réellement. `EMBEDDING_MODEL_REVISION` a été
   bumpée (`...:chunking-v2-tokens`) pour rendre périmé tout index construit sous l'ancien
   découpage par caractères.
2. **`EMBEDDING_MODEL_REVISION` reflète désormais l'artefact réellement chargé** (le hash du
   snapshot HuggingFace résolu, ajouté en suffixe une fois le modèle chargé dans le processus) —
   avant, c'était une étiquette purement déclarative, jamais vérifiée contre ce que `fastembed`
   avait effectivement résolu.
3. **La preuve affichée/persistée pour un chunk trouvé UNIQUEMENT par le signal vectoriel est
   désormais la fenêtre gagnante réelle** (ses propres `content`/`start_char`/`end_char`, stockés
   dans `knowledge_passages`), plus une relocalisation par TF-IDF (`locate_relevant_passage`) sur
   l'ensemble du chunk — qui pouvait montrer un extrait sans rapport avec la raison réelle du
   rapprochement sémantique. `private_rag_manager.evidence_for_chunk` est remplacée par
   `evidence_for_passage_hit`.
4. **Un candidat non validé par un reranker réel ne devient jamais une preuve de scoring
   favorable.** Défaut réel reproduit : sur un corpus entièrement hors sujet, avec le LLM
   désactivé (mode « fallback sans clé API », une configuration réelle supportée), un plus-proche-
   voisin vectoriel sans aucun support lexical atteignait `evidence_pack` — et la note de
   `reference_evidence` — sans qu'aucun mécanisme ne l'ait confirmé. Nouvelle fonction
   `hybrid_search.confirm_evidence_after_rerank(evidences, rerank_status=...)`, appliquée par
   `jobs.py` (après le reranking réel) et par `/api/scoring-config/simulate` (qui ne reranke
   jamais) : réutilise le seuil lexical **déjà existant**
   (`private_rag_manager.LEXICAL_RELEVANCE_FLOOR = 0.01`, jamais un nouveau seuil vectoriel
   inventé) comme seule confirmation restante quand le reranker n'a rien validé pour de vrai — sans
   jamais rejeter une paraphrase légitime dont le score lexical est nul si un reranker réel l'a
   validée. Attention documentée et testée : ce filtre ne doit JAMAIS confondre un score
   invalide (NaN, hors bornes — une vraie corruption de donnée) avec un score valide simplement
   sous le seuil ; un score invalide continue de remonter à
   `src.core.rag_evidence_validation.ensure_valid_evidences` sans être filtré.
5. **`GET /api/knowledge/search` expose `lexically_confirmed`** par résultat (purement
   informationnel, jamais un filtre) — l'UI affiche « pertinence non confirmée » pour un résultat
   sémantique sans support lexical, sans jamais le rejeter de la recherche elle-même.
6. **Mode `hybrid_partial`** : si au moins un document actif n'a pas un index vectoriel `ready` ET
   à jour (config courante), le mode annoncé/persisté n'est plus `hybrid` (qui sous-entendrait une
   couverture complète du corpus) mais `hybrid_partial` — jamais un document `failed` ne devient
   invisible pour autant du signal lexical.
7. **Une réindexation qui échoue ne détruit plus un index déjà bon.** Défaut réel reproduit :
   l'ancienne version supprimait les passages existants AVANT de tenter les nouveaux embeddings —
   un échec (panne fournisseur) laissait la version sans aucun passage et `embedding_status='failed'`,
   alors qu'elle avait un index valide juste avant. La suppression n'intervient désormais
   qu'une fois le nouveau lot de vecteurs entièrement calculé avec succès.
8. **Dimension incompatible avec la colonne refusée clairement.** La colonne pgvector est figée à
   `vector(384)` par la migration 0015 ; si `EMBEDDING_DIMENSION` est configuré différemment,
   `embedding_status='failed'` avec `error_code='dimension_column_mismatch'` est posé AVANT toute
   tentative d'écriture — plus d'exception PostgreSQL non gérée remontant à travers le mauvais
   niveau de capture.
9. **`/api/scoring-config/simulate` reste strictement lexical, jamais hybride** — corrigé après
   relecture : ce lot avait initialement branché cette route sur `hybrid_search`, ce qui pouvait
   déclencher un téléchargement réseau du modèle d'embeddings au premier appel si le mode hybride
   était actif globalement, contredisant le contrat explicite de cette route (« strictement locale,
   déterministe, aucun appel LLM ni Pappers, aucune modification de corpus »). Revenu à
   `private_rag_manager.search` en direct, définitivement, quel que soit `RAG_HYBRID_MODE_ENABLED`.
10. **Agrégation par référence, jamais par preuve brute, dans `reference_evidence`** (recette corpus
    utilisateur, 2026-09-24/25). Défaut réel : `src/agents/criteria_evaluators.py::_reference_evidence`
    comptait `n = len(ctx.evidences)` — un simple compte de preuves retenues, pas de références
    distinctes. Le découpage par tokens réels (point 1 ci-dessus) produit légitimement plusieurs
    passages pour une même longue section d'un même document ; chaque passage retenu comptait alors
    pour une référence supplémentaire et sa similarité entrait séparément dans la moyenne — un bonus
    de nombre de références et de score par le seul effet de la granularité du découpage, jamais
    voulu par aucune politique de compte. Corrigé par regroupement explicite
    (`src/core/reference_identity.py::group_evidences_by_reference`, clé = `document_version_id`,
    repli sur `.source` si absent, jamais une clé constante) : `n` = nombre de groupes (références
    distinctes), le score retenu par groupe est le **maximum** de ce groupe (jamais une somme/moyenne
    intra-groupe), reprenant le principe déjà validé par B18 (« meilleure preuve déjà validée, jamais
    un bonus de répétition ») un niveau au-dessus. Aucune citation n'est perdue (toutes restent
    disponibles pour l'affichage/reranking) ; seul le comptage utilisé par le critère de scoring est
    déduplique. Preuve : `tests/test_recette_corpus_reference_aggregation.py` (6 tests) + preuve en
    situation réelle sur un corpus de 12 documents (`docs/qa/recette_corpus_utilisateur_20260924/
    RAPPORT.md` §4 : `n_refs <= n_documents_distincts_reellement_cites` vérifié après une analyse GO
    réelle).

Vérifié conforme, non corrigé (comportement déjà correct ou hors périmètre assumé) : le score
lexical n'est jamais recalculé par un nouveau TF-IDF sur un sous-ensemble de candidats (le même
vectorizer déjà ajusté sur tout le corpus est toujours réutilisé) ; un contenu dupliqué à l'identique
entre deux documents ne compte jamais deux fois (le mécanisme de déduplication existant, basé sur
l'empreinte de contenu du chunk, filtre déjà les candidats vectoriels de la même façon que les
candidats lexicaux) ; la LISTE de preuves retournée par la recherche peut toujours légitimement
contenir plusieurs passages d'un même document réellement scindé en plusieurs chunks distincts
(comportement hérité, inchangé, du RAG lexical existant) — c'est uniquement le COMPTAGE utilisé pour
le critère `reference_evidence` (point 10 ci-dessus) qui regroupe désormais ces passages par
référence, jamais la liste de preuves elle-même (aucune identité de projet n'est inventée sans
identifiant métier fiable, conformément à la consigne).

## 7. Hors périmètre (explicitement, comme demandé)

- Index ANN (ivfflat/hnsw).
- Un second modèle d'embeddings / reranking sémantique avancé au-delà de la fusion RRF.
- Migration de la base réelle vers PostgreSQL (reste SQLite, inchangé).
- Concurrence multi-processus réelle sur l'index vectoriel (qualifiée uniquement en mono-processus
  via `pgserver`, voir RAPPORT_CLOTURE.md).
- Extraction de nouveaux faits documentaires (lot 52).
