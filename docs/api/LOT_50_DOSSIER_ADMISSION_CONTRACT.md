# Lot 50 — Contrat API/UI : pièces libres, classement/modération assistés, admission explicite

Destiné au développeur frontend. Complète (ne remplace pas) `docs/api/LOT_49_COMPLETION_CONTRACT.md` (révisions) et le contrat implicite du lot 47 bis (`POST /api/analyze mode=dossier`, toujours actif, comportement **inchangé**). Toutes les routes exigent une session authentifiée (`require_permission("analysis:create")`) ; les mutations exigent `X-CSRF-Token`.

## 1. Principe

`POST /api/analyze mode=dossier` reste utilisable tel quel (compatibilité) : soumission directe, tout-ou-rien, comme au lot 47 bis. Le nouveau parcours recommandé par l'interface est en **deux temps** :

1. `POST /api/analyze/dossier-preview` — reçoit et vérifie les pièces (structure, octets, format, sécurité, classement, pertinence) et répond par un **tableau d'admission**, sans démarrer de job.
2. `POST /api/analyze/dossier-preview/{dossier_id}/confirm` — confirme (ou corrige) l'ensemble retenu et démarre l'analyse, exactement comme la route directe à partir de ce point.

Rien n'est analysé entre les deux appels ; l'aperçu **expire** (`DOSSIER_STAGING_TTL_SECONDS`, 30 min par défaut) et doit alors être resoumis.

## 2. Pièces libres (« autres »)

Nouveau champ multipart `autres` (plusieurs fichiers), à côté de `rc`/`cctp`/`ccap`/`acte_engagement`/`annexes` — **aucune des 4 catégories guidées n'est obligatoire** (lot 50 §4). Les pièces libres partagent exactement les mêmes limites que les autres (7 fichiers au total, 100 000 000 octets cumulés, 300 000 caractères extraits) — aucune allocation séparée. `GET /api/analyze/dossier-limits` expose désormais `autre_examples` (libellés d'exemple, jamais une liste fermée) et `main_categories` (les 4 catégories guidées, pour calculer une éventuelle limitation de périmètre côté client).

## 3. `POST /api/analyze/dossier-preview`

Même multipart que la route directe (`rc`, `cctp`, `ccap`, `acte_engagement`, `annexes`, `autres`). Une erreur **structurelle** (catégorie inconnue, trop de fichiers, dossier vide, budget d'octets dépassé) refuse **tout** exactement comme la route directe (mêmes codes : `UNKNOWN_CATEGORY`, `TOO_MANY_FILES_FOR_SLOT`, `TOO_MANY_ANNEXES`, `DOSSIER_EMPTY`, `TOO_MANY_FILES`, `DOSSIER_TOO_LARGE`). Un problème **par pièce** (format non supporté, fichier vide, illisible, injection détectée, hors sujet) ne bloque plus tout le dossier : la pièce est incluse dans le tableau, proposée exclue par défaut, avec son motif.

Réponse `200` :
```json
{
  "dossier_id": "…",
  "expires_at": "2026-09-25T10:00:00+00:00",
  "pieces": [
    {
      "id": "…", "categorie": "autre", "categorie_libelle": "Autres pièces liées à cet appel d'offres",
      "categorie_proposee": null, "categorie_finale": "autre",
      "nom": "notes_reunion.txt", "format": ".txt", "taille": "1,2 Ko", "empreinte": "…",
      "securite": "authorized", "securite_code": "no_known_pattern", "securite_motif": "…",
      "pertinence": "lie", "pertinence_motif": "…",
      "sera_pris_en_compte": true, "raison_exclusion": null, "lien_declare": null,
      "doublon_de": null
    }
  ],
  "rejetees": [{"category": "autre", "category_label": "…", "piece": "scan.jpg", "error_code": "UNSUPPORTED_CONTENT", "message": "…"}],
  "au_moins_une_piece_exploitable": true
}
```
- `categorie` : la catégorie **déclarée** (le champ d'envoi choisi) — jamais réécrite.
- `categorie_proposee` : la suggestion du classeur (§2.B), ou `null` (« indéterminé » — jamais une supposition présentée comme sûre). Motif/preuves disponibles côté serveur, non dans cette réponse compacte (voir `securite_motif`/`pertinence_motif` pour le même principe côté sécurité/pertinence).
- `securite` : `"authorized"` | `"to_verify"` | `"blocked"` — `"blocked"` ne peut **jamais** être forcé à la confirmation.
- `pertinence` : `"lie"` | `"incertain"` | `"hors_sujet"` | `null` (pièce dupliquée, jamais relue) — jamais fondé sur le seul « ≥2 termes de marché ».
- `sera_pris_en_compte` : la proposition du serveur — `false` par défaut pour une pièce bloquée ou hors sujet ; l'utilisateur peut corriger (sauf `securite: "blocked"`) à la confirmation.
- `rejetees` : pièces au format non supporté — **jamais stockées**, rien à confirmer les concernant.

## 4. `POST /api/analyze/dossier-preview/{dossier_id}/confirm`

```json
{
  "decisions": [
    {"piece_id": "…", "content_hash": "…", "include": true, "category_final": "autre", "link_note": "texte optionnel"}
  ]
}
```
- **Chaque** pièce actuellement dans l'aperçu doit apparaître **exactement une fois**, avec le `content_hash` vu à l'aperçu — sinon `409 STALE_PREVIEW` (rien n'est écrit). C'est la vérification d'intégrité du §3 (« lié aux octets exacts »).
- `include: false` sur une pièce non bloquée l'exclut, avec le motif éventuel (`reason`, sinon celui déjà proposé).
- `category_final` corrige la catégorie retenue pour cette analyse (doit être l'une des 6 catégories connues).
- `link_note` (texte, 500 caractères maximum) : la propre justification de l'utilisateur pour une pièce incertaine/hors sujet confirmée incluse — une **déclaration**, jamais une preuve.
- Une pièce `securite: "blocked"` reste exclue **quoi que le client envoie** — le serveur l'impose, jamais une confirmation qui la lèverait.

**Réponses d'erreur** :
| Statut | `error_code` | Cas |
|---|---|---|
| 404 | — | aperçu introuvable / pas le vôtre |
| 410 | `PREVIEW_EXPIRED` | aperçu expiré — supprimé, à resoumettre |
| 409 | `STALE_PREVIEW` | décision manquante ou empreinte différente de l'aperçu — rien n'est écrit |
| 422 | `DOSSIER_NO_USABLE_PIECE` | aucune pièce sûre et exploitable retenue après application des décisions |
| 422 | `INVALID_VALUE` | `decisions` absent/vide, catégorie inconnue, `link_note` trop long |
| 409 | `SCORING_NOT_CONFIGURED` / `CAPACITY_NOT_CONFIGURED` | comme la route directe |
| 429 | `JOB_QUEUE_SATURATED` | comme la route directe |

**Succès (200)** : `{"job_id": "…", "categories_manquantes": ["ccap", "rc", …], "perimetre_limite": true}`. Le job se suit ensuite exactement comme une analyse normale.

## 5. Trois rôles, droits limités (§2)

- **Agent de sécurité** (`src/agents/document_security_agent.py`) : réutilise `ContentSecurityGate` (le motif d'injection existant reste la SEULE cause de `blocked`) et ajoute un état `to_verify` (vocabulaire suspect co-occurrant avec un verbe impératif, sans motif strict) — `authorized` n'est jamais une garantie d'innocuité, énoncé comme tel.
- **Agent de classement** (`src/agents/document_classifier_agent.py`) : vocabulaire par catégorie + indice de nom de fichier (jamais seul) → une catégorie proposée ou `null` (indéterminé). Pas de score de confiance inventé.
- **Agent modérateur** (`src/agents/document_moderator_agent.py`) : `lie` si la pièce partage un terme significatif avec les AUTRES pièces du dossier (même client/site/référence — une pièce sans vocabulaire de marché peut ainsi être « liée ») ou avec le lien déclaré par l'utilisateur, ou si elle porte elle-même du vocabulaire de marché ; `hors_sujet` sinon ; `incertain` si le contenu est trop pauvre pour trancher.

Aucun des trois n'a d'accès shell/fichier/réseau arbitraire, ne lit un autre compte, n'écrit de politique ni de décision de scoring. Un avis d'agent ne contourne jamais un contrôle serveur déterministe (ex. un blocage de sécurité).

## 6. Manifeste, traçabilité (§5)

Chaque pièce de dossier porte désormais (colonnes additives, migration 0013) : `category_proposed`, `category_final`, `security_state/code/reason`, `moderation_verdict/reason`, `admitted`, `exclusion_reason`, `user_link_note`. `AoDossier` porte `categories_missing`, `scope_limited`, `confirmed_by_user_id`, `confirmed_at`, `staging_expires_at`. `GET /api/analyze/{job_id}/dossier` (inchangé dans sa forme) inclut maintenant ces champs par pièce (`categorie_proposee`, `securite`, `pertinence`, `sera_pris_en_compte`, `raison_exclusion`, `lien_declare`) — une pièce exclue reste **listée**, jamais effacée du manifeste.

`AOContext.dossier` (le payload consolidé, déjà utilisé par le résultat/les livrables) porte désormais `categories_manquantes` et `perimetre_limite`, figés à la confirmation — jamais recalculés plus tard à partir de l'état courant du dossier. Le résultat (`templates/app_result.html`, bandeau `#dossier-scope-banner`) et les livrables PDF/DOCX (`document_generator.py`) affichent une indication **distincte** du statut INCOMPLET : « Analyse limitée aux pièces fournies », les catégories non fournies, jamais présentée comme une validation exhaustive du marché.

## 7. Ce qui N'A PAS changé

- La route directe `POST /api/analyze mode=dossier` : comportement, codes d'erreur et contrat identiques au lot 47 bis — aucun client existant n'est cassé.
- Le moteur de scoring : aucune règle, aucun poids, aucune pénalité documentaire nouvelle. Un bloqueur confirmé reste NO-GO ; une donnée requise absente reste INCOMPLET.
- `/api/analyze/{job_id}/completion|complete` (lot 49/49 bis) : inchangé, toujours fondé sur le snapshot figé de la politique/du profil.
- La base de connaissances (B03) : toujours un seul pipeline d'ingestion (`POST /api/knowledge/documents`, un fichier) ; la page permet maintenant une **sélection multiple** côté client, qui rappelle cette même route une fois par fichier (aucune route batch nouvelle), avec un résumé qui nomme chaque succès/échec.

## 8. Limites connues (lot 50, résolues ou reportées par lot 50 bis — voir §9-§13)

Les trois limites listées ici au lot 50 sont désormais résolues par le lot 50 bis (jugement LLM réel, balayage d'expiration réel, « Ajouter les pièces restantes ») — détail ci-dessous. Le classement/la modération restent **toujours** un résultat honnête, jamais présenté comme "validé par IA" à lui seul : `*_origine` nomme la voie qui a réellement produit chaque valeur.

---

## 9. Lot 50 bis §1 — jugement LLM réel (agents documentaires)

Les trois agents (sécurité, classement, modération) peuvent désormais consulter le fournisseur LLM déjà configuré de l'organisation (même client que `ao_extractor.py`, `src/agents/llm_client.py`), en plus de leur heuristique — jamais à la place. Chaque piece du tableau d'admission (§3) porte trois champs additionnels nommant QUI a produit la valeur :

```json
{"categorie_origine": "heuristic|llm|heuristic_llm_unavailable|heuristic_llm_invalid",
 "securite_origine": "heuristic|llm|heuristic_llm_unavailable|heuristic_llm_invalid",
 "pertinence_origine": "heuristic|llm|heuristic_llm_unavailable|heuristic_llm_invalid"}
```
- `heuristic` : aucun appel LLM tenté ou pertinent pour ce cas.
- `llm` : réponse du fournisseur reçue, JSON valide, **citation vérifiée** (sous-chaîne exacte, tolérante à la casse/aux espaces, du texte réellement montré au modèle — jamais le document complet non tronqué) — la seule valeur qui reflète un réel avis de modèle.
- `heuristic_llm_unavailable` : aucun fournisseur configuré/activé, ou tous ont échoué (réseau, quota, authentification) — repli sur l'heuristique, jamais une erreur utilisateur.
- `heuristic_llm_invalid` : le fournisseur a répondu mais la forme, une valeur d'énumération ou la citation n'a pas pu être validée — la réponse entière est alors non fiable et écartée (jamais partiellement retenue) au profit de l'heuristique.

**Invariants de sécurité, inchangés et vérifiés sous fournisseur réel (§13)** :
- Le LLM n'est **jamais** consulté pour une pièce `securite: "authorized"` ni `"blocked"` — uniquement pour `"to_verify"` (vocabulaire suspect co-occurrant avec un verbe impératif, sans motif strict).
- Le schéma de sortie du second avis de sécurité ne contient **pas** la valeur `"authorized"` : structurellement, un avis LLM ne peut jamais faire passer une pièce à l'état autorisé.
- Un `"blocked"` (injection confirmée) n'est **jamais** reconsidéré par un second avis — le code n'appelle même pas le LLM dans ce cas.
- Le simple mot « exemple » (ou toute autre marque de citation) trouvé n'IMPORTE OÙ dans le document ne suffit plus à assouplir un signal suspect : la marque doit apparaître dans une fenêtre de 120 caractères autour du passage suspect (`_CITATION_WINDOW_CHARS`) — correction d'un vrai faux-négatif du lot 50 (une marque de citation lointaine, sans rapport, pouvait auparavant adoucir à tort le motif retourné).

**Aucune troncature cachée** : le texte transmis au modèle est borné à `MAX_LLM_INPUT_CHARS` (45 000 caractères, ≈3 fenêtres de dossier) ; si une coupe a réellement eu lieu, le motif renvoyé le dit explicitement (jamais silencieuse).

**Garde-fou distinct, corrigé dans le même lot** : le contrôle global du dossier « ≥2 termes de marché » (`ContentSecurityGate`, déjà utilisé par la route directe à la réception) est désormais **aussi** appliqué par `POST /api/analyze/dossier-preview/{id}/confirm`, sur le texte final RETENU (après décisions de l'utilisateur), avant de créer le job. Avant cette correction, un dossier confirmé avec succès (200) dont le texte final retenu tombait sous ce seuil pouvait voir son JOB échouer plus tard, silencieusement, avec un message de sécurité surprenant après une confirmation déjà acceptée — la route directe et la route aperçu/confirmation partagent maintenant ce contrôle. Nouvelle réponse d'erreur possible sur `.../confirm` :

| Statut | `error_code` | Cas |
|---|---|---|
| 422 | `CONTENT_BLOCKED` | le texte des pièces retenues ne comporte pas assez de vocabulaire de marché (ou une instruction d'injection est présente dans le texte consolidé) — rien n'est écrit, `reasons` liste les codes techniques |

## 10. Lot 50 bis §2 — classement du contenu de la base privée

`POST /api/knowledge/documents` et `POST /api/knowledge/documents/{id}/versions` classent désormais chaque version (référence / certification / présentation / autre / indéterminé), jamais une exigence de citer un AO. Champs additifs sur chaque version (`_version_summary`) :
```json
{"content_category_proposed": "certification", "content_category_final": "certification",
 "content_category_source": "llm", "content_category_reason": "…"}
```
`content_category_source` suit la même convention que §9, plus deux valeurs propres à la base privée : `"user"` (correction explicite, tracée) et `"unknown"` (version antérieure au lot 50 bis — jamais réétiquetée « validée par IA » rétroactivement). **Une classification « certification » n'est pas une certification vérifiée** : elle n'est jamais écrite dans le profil ni utilisée par le scoring. Seules les versions admises restent cherchables ; un remplacement refusé conserve l'ancienne version active. Nouvelle route : `POST /api/knowledge/documents/{id}/versions/{version_id}/category` — corrige `content_category_final` (une des 5 catégories), enregistre `content_category_source="user"`, exige `X-CSRF-Token`.

**Lot 50 ter** : cette route et ces champs étaient sans interface avant ce lot (défaut trouvé par la recette navigateur, voir `docs/qa/lot_50_ter_20260927/RAPPORT_CLOTURE.md` §3 bis). `templates/app_knowledge.html`/`static/js/knowledge.js` affichent désormais, par document, la catégorie finale et son origine, avec (pour un compte autorisé à écrire) un sélecteur + bouton de correction relié directement à cette route.

## 11. Lot 50 bis §3 — « Ajouter les pièces restantes »

Depuis un résultat issu d'un dossier, `POST /api/analyze/{job_id}/add-pieces/preview` prépare une **nouvelle analyse documentaire**, distincte d'une révision déclarative (lot 49 bis) : extraction et scoring **entièrement recalculés**, avec la politique/le profil/la capacité **courants** (jamais figés). Chaque pièce déjà admise de l'analyse d'origine est revérifiée (fichier toujours présent, empreinte SHA-256 inchangée) avant d'être proposée pour report — une pièce manquante ou modifiée n'est **jamais** reconstruite (`error_code: "ORIGINAL_PIECE_MODIFIED"` / `"ORIGINAL_PIECE_MISSING"` dans `rejetees`). Corps multipart identique à `dossier-preview`, plus un champ `keep_piece_ids` (JSON, liste des identifiants de pièces d'origine à conserver — les autres sont abandonnées, jamais réintroduites implicitement) ; `keep_piece_ids` seul (sans nouveau fichier) est un cas valide (retrait pur). Réponse identique à `dossier-preview`, avec `origin_job_id` en plus. La confirmation réutilise **la même route générique** `POST /api/analyze/dossier-preview/{id}/confirm` (le dossier de staging porte `origin_job_id`, propagé au nouveau job). Lignage : `Analysis.origin_job_id` (colonne additive, **pas unique** — distincte de `parent_job_id`, unique, réservé à la révision déclarative) ; une analyse peut en théorie porter l'un ou l'autre mais jamais les deux par construction applicative. L'ancien job/résultat/livrables restent **intacts, jamais modifiés**. Erreurs propres :

| Statut | `error_code` | Cas |
|---|---|---|
| 404 | `NOT_A_DOSSIER_ANALYSIS` | l'analyse d'origine ne vient pas d'un dossier (mode texte/fichier unique) |
| 409 | `JOB_NOT_COMPLETE` | l'analyse d'origine n'est pas terminée |

## 12. Lot 50 bis §4 — expiration réelle des aperçus abandonnés

Un aperçu (`status='staging'`) expiré n'est plus nettoyé uniquement au prochain `confirm` tenté dessus : un balayage réel (`src/web/ao_dossier/expiry_sweeper.py`) tourne au démarrage (rattrapage, `main.py`'s lifespan) puis périodiquement sur un thread démon (`DOSSIER_STAGING_SWEEP_INTERVAL_SECONDS`, 300 s par défaut), par lots bornés (`DOSSIER_STAGING_SWEEP_BATCH_SIZE`, 50 par défaut). Ne touche **jamais** un dossier `validated`/`submitted`, même appelé avec une horloge très avancée (la clause `WHERE status='staging'` est la frontière d'atomicité contre un `confirm` concurrent). Le stockage n'est supprimé qu'après la suppression BDD validée (jamais l'inverse). Aucun contenu de pièce dans les journaux — uniquement identifiants/compteurs.

## 13 bis. Lot 50 ter — pertinence découplée du seuil lexical

Le contrôle « ≥2 termes de marché » (§9) n'est plus un veto absolu : `src/web/ao_dossier/scope.py::assess_dossier_scope` l'utilise comme signal de repli UNIQUEMENT quand aucune pièce n'a été jugée `lie` par le modérateur (heuristique par jeton partagé — client/site/référence/lot — ou LLM réel). Une pièce reconnue pertinente par le contexte (sans le vocabulaire classique d'un marché) n'est donc plus rejetée. La sécurité (injection) reste un veto absolu, vérifiée en premier, jamais influencée par la pertinence. Cette même règle s'applique désormais aux TROIS points qui appliquaient l'ancien contrôle séparément : la route directe (intake), la confirmation (`check_admitted_scope`) et le worker (`src/web/jobs.py`, qui ne revérifie plus qu'une injection en texte intégral au moment du job — jamais la pertinence, déjà tranchée à l'admission). `error_code: "CONTENT_BLOCKED"` reste inchangé dans sa forme ; le message ne revendique plus un seuil de 2 termes strict. Voir `docs/qa/lot_50_ter_20260927/RAPPORT_CLOTURE.md` §2.

## 13 ter. Lot 50 ter — résolution de cible de base de données (Alembic/tests)

`src/core/db_target.py` est désormais l'unique point de résolution de l'URL de base de données pour `migrations/env.py` (et, en garde de profondeur, `src/web/database/session.py::get_engine()`). Une URL explicitement posée par un appelant sur l'objet `Config` Alembic gagne toujours sur `DATABASE_URL` applicatif (avant ce lot, c'était l'inverse — défaut à l'origine d'un incident réel, voir le rapport). En mode test (`PYTEST_CURRENT_TEST` ou `WM_DB_TEST_MODE=1`), la cible résolue est en plus structurellement validée avant toute connexion : jamais le nom de fichier de la base réelle, jamais un chemin à l'intérieur du dépôt, jamais hors des racines jetables autorisées (le dossier temp système par défaut ; `WM_TEST_DB_EXTRA_ROOTS` pour déclarer explicitement un `--basetemp` personnalisé). Voir `docs/qa/lot_50_ter_20260927/RAPPORT_CLOTURE.md` §1.

## 13 quater. Lot 50 ter — recette navigateur exécutée (24/24)

Après résolution d'un incident d'environnement externe (Smart App Control Windows refusant temporairement plusieurs bibliothèques compilées de `scipy`, dépendance transitive de `main.py` via le RAG — sans rapport avec le code livré, voir le rapport §0/§0 bis), la recette navigateur complète a été rejouée : documents privés classés + catégorie corrigée via un contrôle UI désormais existant, pièce pertinente sans vocabulaire classique admise (jeton partagé), annonce préalable de politique/profil/capacité actuels avant confirmation d'un ajout de pièces, nouvelle analyse liée avec dossier d'origine inchangé, retrait sans ajout, refus de sécurité non forçable, expiration honnêtement expliquée, aucune fuite entre organisations. 24/24 vérifications passées, captures dans `docs/qa/lot_50_ter_20260927/recette/shots/`. Deux défauts d'interface réels (catégorie de la base privée jamais affichée/corrigeable ; annonce préalable de l'ajout de pièces incomplète) ont été trouvés PAR cette recette et corrigés (`static/js/knowledge.js`, `static/js/add_pieces.js`) — voir le rapport §3 bis.

## 13 quinquies. Essai réel fournisseur (qualification, non répété inutilement)

Un essai avec le fournisseur/modèle déjà configuré de l'organisation (clé réelle, jamais copiée dans un rapport) a été mené sur un dossier synthétique isolé (base/stockage jetables) : 20 appels réels aboutis (aucun échec), latences ~1,8 s à 36 s, classement/pertinence/second-avis de sécurité tous produits avec citation vérifiée (`*_origine: "llm"`), et une pièce « à vérifier » (vocabulaire suspect + marque de citation) confirmée **jamais** remontée à `"authorized"` par le second avis — invariant tenu sous fournisseur réel, pas seulement en test simulé. Le nombre de jetons consommés n'est pas exposé par `LLMClient` (seuls fournisseur/durée/nombre d'appels sont journalisés) — non inventé ici.
