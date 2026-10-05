# B06-T1 — Contrat API : configuration privée du scoring (profil prestataire + politique)

Destiné au développeur frontend. L'écran `/app/parametres` existe (lots 38 et 41) et consomme ce contrat ; ce document décrit le code réel. Toutes les routes ci-dessous exigent une session authentifiée (cookie `wm_session`) et un abonnement Starter actif.

**CSRF (B14-T1)** : toute requête qui modifie l'état (`PUT /profile`, `PUT /policy`, `POST /policy/validate`, `POST /policy/activate`, `POST /simulate`) doit envoyer l'en-tête `X-CSRF-Token` (valeur de `<meta name="csrf-token">`, cookie httponly non lisible en JS) — sinon `403` `"Requête invalide (jeton de sécurité manquant ou expiré). Rechargez la page et réessayez."`, vérifié avant toute autre validation. Les `GET` n'en exigent pas.

**Organisation** : chaque route résout l'organisation côté serveur (`get_access_context`). Un compte à une seule organisation n'a rien à préciser ; un compte à plusieurs peut passer `?organization_id=<uuid>` (prioritaire) ou avoir le cookie `wm_org_id` (mémorisation, jamais un droit). Le formulaire `/app/parametres` envoie **toujours** `organization_id` explicitement (un changement d'organisation dans un autre onglet ne peut ainsi jamais rediriger une écriture) et refuse d'écrire si le cookie diffère de l'organisation affichée.

**Codes d'erreur réels** (le corps est `{"detail": <chaîne>}` ou `{"detail": {"error_code", "message", ...}}`) :

| Statut | Cas | `detail` |
|---|---|---|
| 401 | pas de session | `"Authentification requise."` |
| 403 | CSRF manquant/invalide ; rôle sans `scoring:configure` (`viewer`) ; organisation non autorisée/révoquée/indisponible | chaîne explicite |
| 404 | `validate`/`activate`/`simulate` sans brouillon | `"Aucun brouillon …"` |
| 409 | plusieurs organisations sans sélection (`"Plusieurs organisations sont disponibles…"`) ; `SCORING_POLICY_ACTIVATION_CONFLICT` (`current_active_version`) ; `simulate` sans capacité : `CAPACITY_NOT_CONFIGURED` | objet ou chaîne |
| 400 | `activate` sans `expected_active_version` | chaîne |
| 413 | corps de `simulate` trop gros : `REQUEST_BODY_TOO_LARGE` ; fichier/texte refusés par la validation d'entrée (`docs/api/B13_T1_INPUT_VALIDATION_CONTRACT.md`) | objet |
| 422 | `SCORING_POLICY_INVALID` (`errors`: `weights`, `thresholds`, `profile`, `business_rules`, `business_facts`, `custom_criteria` → listes de messages) ; entrée de simulation invalide ; `invalid_rag_evidence` | objet |

Aucune limitation de débit n'est appliquée à ces routes aujourd'hui (pas de `429` ; le frontend gère néanmoins `429`/`503` génériquement). Les corps d'erreur ne contiennent jamais de chemin, de secret ni de trace ; ils peuvent en revanche **citer des identifiants/libellés saisis par l'utilisateur** — un client doit les afficher en texte brut (jamais en HTML).

## 1. Ce que ce ticket change pour un compte

Avant B06-T1, `ScoringEngine` utilisait des constantes globales (`mastered`, `certs_ok`, poids, seuils `SCORING_THRESHOLD_GO`/`SOUS_RESERVE`) — partagées par tout le déploiement. Depuis B06-T1, **`POST /api/analyze` refuse avec `409 SCORING_NOT_CONFIGURED`** tant que le compte n'a pas activé sa propre politique de scoring, exactement comme il refuse déjà avec `409 CAPACITY_NOT_CONFIGURED` (B03) tant que la capacité n'est pas configurée. Un compte neuf (ou un compte B01-B04 déjà existant) n'a **aucune politique active par défaut** — aucune migration ne le recopie depuis les anciennes constantes globales.

Parcours : **À CONFIGURER → BROUILLON → VALIDATION → ACTIVATION → ANALYSES**. Ce backend persiste 3 états réels (`a_configurer`, `brouillon`, `analyses` — voir `state` ci-dessous) ; VALIDATION et ACTIVATION sont des actions (`POST .../validate`, `POST .../activate`), pas des états stockés séparément.

## 2. Trois objets privés, jamais partagés entre collègues

| Objet | Route | Portée |
|---|---|---|
| Profil prestataire (déclaratif : raison sociale, effectif, compétences, certifications déclarées) | `/api/scoring-config/profile` | `(organization_id, owner_user_id)` — jamais le `CompanyProfile` de l'acheteur de l'AO |
| Politique de scoring (poids, seuils, versionnée) | `/api/scoring-config/policy*` | `(organization_id, owner_user_id)` |
| Capacité privée (inchangée, B03) | `/api/capacity` | `(organization_id, owner_user_id)` |

Deux collègues de la même organisation ont chacun leur propre ligne pour ces trois objets — aucun partage automatique, même entre `organization_admin` et `analyst`.

## 3. Matrice des permissions

| Permission | viewer | analyst | organization_admin | Portée effective |
|---|:---:|:---:|:---:|---|
| `resource:read` (lecture GET) | ✅ | ✅ | ✅ | — |
| `capacity:configure` (`POST /api/capacity`) | ❌ | ✅ **(nouveau, B06-T1)** | ✅ | uniquement SON propre plan |
| `scoring:configure` (toutes les mutations `/api/scoring-config/*`) | ❌ | ✅ **(nouveau, B06-T1)** | ✅ | uniquement SA propre configuration |
| `analysis:create` (`POST /api/analyze`) | ❌ | ✅ | ✅ | — |
| `knowledge:write` | ❌ | ✅ | ✅ | — |
| `org:manage_members`, `org:configure` | ❌ | ❌ | ✅ | organisation entière (inchangé) |

**Changement de permission notable** : `POST /api/capacity` exigeait `org:configure` (organization_admin uniquement) avant B06-T1 — un analyst ne pouvait pas configurer sa propre capacité et ne pouvait donc jamais compléter seul son parcours À CONFIGURER → ANALYSES. Remplacé par `capacity:configure`, accordé à `analyst` ET `organization_admin`. C'est sûr car `private_capacity_repo.save_for_owner`/`scoring_policy_repo.*`/`provider_profile_repo.save_for_owner` prennent tous `owner_user_id` en paramètre explicite, toujours rempli par le serveur avec `ctx.user.id` — jamais une valeur envoyée par le client — un analyst ne peut donc structurellement jamais écrire la ligne d'un collègue, quel que soit son rôle.

## 4. `GET /api/scoring-config`

```json
{
  "status": "configuration_required",
  "state": "a_configurer",
  "score_global": null,
  "profile": {"status": "incomplete", "raison_sociale": null, "effectif": null, "competences": [], "certifications": []},
  "policy": {"active": null, "draft": null},
  "draft_validation": null,
  "can_configure": true
}
```
`status` vaut `"configured"` dès qu'une politique est active (même si un brouillon existe en parallèle pour la version suivante). `state` vaut `"analyses"` dans ce cas — `/api/analyze` fonctionne. `draft_validation` (présent seulement si un brouillon existe) donne un aperçu de ce que renverrait `POST .../policy/validate`, sans appel supplémentaire.

**Lot 48** : le champ `available_criteria` (les 12 critères avec un `default_weight` "modèle informatique") a été **retiré** — code confirmé mort (lot 44 l'avait déjà signalé comme non consommé par l'écran ; aucun script front, aucun test ne le lisait). Le seul brouillon proposé réellement chargé par le formulaire, sur action explicite, est `criteria_templates` (§ lot 44 ci-dessous, `proposed_templates()`).

## 5. Profil prestataire — `PUT /api/scoring-config/profile`

```json
{"raison_sociale": "Ma Petite ESN", "effectif": "10-50",
 "competences": ["python", "aws", "docker"],
 "certifications": [{"nom": "ISO 27001", "statut": "declaree", "preuve_reference": null}]}
```
`statut` ∈ `"declaree"` (aucune preuve jointe) | `"verifiee"` (un `preuve_reference` est fourni). **Seul `raison_sociale` non vide est requis** avant activation — tout le reste est facultatif et ne bloque jamais (ticket section 4). `preuve_reference`, s'il est fourni, doit être l'`id` d'un document réellement possédé par l'appelant (`GET /api/knowledge/documents`) — une référence étrangère ou fabriquée est refusée à la validation/activation (`422`, voir §7), jamais à la sauvegarde du profil lui-même.

**Point produit non tranché, à afficher clairement au frontend** : une certification "declaree" (sans preuve) est aujourd'hui traitée comme une certification détenue pour le calcul du bloqueur de certification obligatoire (`ScoringEngine`) — le champ `statut` est stocké et exposé, mais n'est pas encore utilisé pour distinguer un niveau de confiance différent. Ne pas présenter "declaree" comme "vérifiée" dans l'UI.

## 6. Politique de scoring — brouillon, validation, activation, versions

### `PUT /api/scoring-config/policy` (créer ou reprendre le brouillon)
```json
{"weights": {"Adequation expertise": 20, "...": "...", "Valeur strategique": 2}, "threshold_go": 88, "threshold_sous_reserve": 60}
```
Les 12 clés de `weights` doivent être exactement celles de `available_criteria[].key`. **Cette route n'exige jamais des valeurs déjà cohérentes** — un brouillon incomplet ou invalide se sauvegarde sans erreur (seuls `/validate` et `/activate` appliquent les règles), pour ne jamais faire perdre une saisie en cours. Répondre plusieurs fois à cette route édite le MÊME brouillon (`version` ne change pas) tant qu'aucune activation n'a eu lieu.

### `POST /api/scoring-config/policy/validate`
Aucun corps. Valide le brouillon courant (poids + seuils + profil + preuves de certification) sans rien persister ni activer.
```json
{"valid": false, "errors": {
  "weights": ["La somme des poids doit être exactement 100 (obtenu : 98)."],
  "thresholds": ["Le seuil « GO SOUS RÉSERVE » doit être strictement inférieur au seuil « GO »."],
  "profile": ["Le nom de votre structure (raison sociale) est requis avant activation."]
}}
```
Une clé n'apparaît que si elle porte au moins une erreur ; `errors: {}` et `valid: true` quand tout est correct.

### `POST /api/scoring-config/policy/activate`
```json
{"expected_active_version": null}
```
Revalide EXACTEMENT comme `/validate` — une validation en échec renvoie `422` avec le même corps `errors` et **ne touche à rien** (le brouillon reste un brouillon, aucune activation partielle). `expected_active_version` est **obligatoire** (entier ou `null`) : passez la `version` de `policy.active` telle que retournée par votre dernier `GET /api/scoring-config` (`null` si elle était `null`). Un conflit (quelqu'un a activé une autre version entretemps) renvoie :
```json
409 {"detail": {"error_code": "SCORING_POLICY_ACTIVATION_CONFLICT", "message": "...", "current_active_version": 3}}
```
Dans ce cas : relire `GET /api/scoring-config`, réafficher l'état réel, laisser l'utilisateur décider s'il veut réactiver sa version par-dessus. Un succès renvoie la politique nouvellement active (`status: "active"`) et archive l'ancienne — **jamais deux politiques actives en même temps** pour le même compte, garanti par un index unique partiel côté base de données, pas seulement par ce contrôle applicatif.

### `GET /api/scoring-config/policy/versions`
Historique complet (`draft`/`active`/`archived`), le plus récent d'abord — utile pour un écran d'audit/historique, non prioritaire pour le MVP du formulaire.

## 7. Simulation — `POST /api/scoring-config/simulate`

Mêmes champs que `POST /api/analyze` (`mode`, `text`/`file`/`example_id`, multipart). **N'active jamais le brouillon**, exige un brouillon qui validerait avec succès (même erreur `422 SCORING_POLICY_INVALID` sinon), et exige aussi une capacité déjà configurée (`409 CAPACITY_NOT_CONFIGURED` sinon). Réponse : un objet `ScoringResult` complet **avec deux champs en plus** :
```json
{"decision": "...", "score_global": ..., "criteres": [...], "simulation": true, "policy_version": 1,
 "note": "Simulation déterministe (sans réordonnancement RAG ni enrichissement LLM) — ne préjuge pas de l'analyse réelle une fois cette configuration activée."}
```
**Toujours afficher `simulation: true` visuellement** (bandeau, filigrane) — ce n'est jamais un résultat d'analyse réelle et il n'est jamais persisté dans l'historique. La simulation n'appelle jamais de LLM (ni Claude ni Pappers réel) : c'est un choix délibéré pour rester rapide et déterministe, pas une limitation technique à corriger plus tard sans décision produit.

**B18-T1** ajoute un troisième refus possible, avant tout calcul :
```json
422 {"detail": {"error_code": "invalid_rag_evidence", "message": "Une preuve de référence interne présente une valeur numérique invalide et empêche le calcul fiable du score. Réessayez ; si cela persiste, contactez le support."}}
```
Ne devrait normalement jamais se produire (les deux producteurs internes du corpus RAG garantissent une similarité dans `[0, 1]`) — un garde-fou de robustesse, pas un cas attendu du parcours normal.

## 8. `POST /api/analyze` — le nouveau refus

Voir `docs/api/B03_KNOWLEDGE_CONTRACT.md` section "Lancement d'analyse", mise à jour par ce ticket. Résumé :
```json
409 {"detail": {"error_code": "SCORING_NOT_CONFIGURED", "message": "Configurez et activez votre politique de scoring avant de lancer une analyse."}}
```
Vérifié **avant tout appel LLM/fournisseur**, comme `CAPACITY_NOT_CONFIGURED`. Une analyse réellement lancée pointe (en interne, `Analysis.result_data.scoring_policy_version`) vers la version EXACTE de la politique active au moment du lancement — activer une nouvelle version plus tard ne change jamais le résultat d'une analyse déjà terminée ou déjà en cours.

**B18-T1** ajoute un champ `error_code` (string ou `null`) à la réponse de `GET /api/analyze/{job_id}/status`, à côté du champ `error` (texte libre) déjà existant :
```json
{"job_id": "...", "status": "error", "error": "Une preuve de référence interne présente une valeur numérique invalide...", "error_code": "invalid_rag_evidence", "...": "..."}
```
`error_code` reste `null` pour toute erreur générique/inattendue (comportement inchangé, seul `error` est renseigné dans ce cas) — seules les erreurs contrôlées et anticipées (pour l'instant : `invalid_rag_evidence`) portent un code stable. Aucun contenu de document ni détail technique brut n'apparaît jamais dans `error` pour ce code.

## 10. B06-T5 — Faits métier privés et critères sectoriels additifs

**Objectif** : un poids configurable sur les 12 critères fixes de `ScoringEngine` ne rend pas la FORMULE elle-même pertinente pour un métier non informatique (nettoyage, BTP, ...). Ce ticket ajoute deux objets ADDITIFS, jamais un second moteur ni un remplacement de `ScoringPolicy` — un compte qui n'en configure aucun garde exactement l'ancienne formule à 12 critères, à l'octet près.

**Catalogue fixe** (jamais configurable, `src/agents/business_facts.py`), exposé dans `GET /api/scoring-config` sous `business_facts_catalogue` :
```json
{"fact_types": ["boolean", "list", "number", "text"],
 "operators": ["equality", "list_coverage", "numeric_threshold"],
 "numeric_comparisons": ["provider_gte_ao", "provider_lte_ao"]}
```

**Faits métier privés déclarés** — `PUT /api/scoring-config/profile` accepte désormais un champ additif `business_facts` (absent du payload = inchangé ; `{}` explicite = tout effacer) :
```json
{"business_facts": {
  "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": null, "value": ["Lyon", "Villeurbanne"]},
  "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 3}
}}
```
`key` (forcé serveur-side à correspondre à la clé du dict), `label`, `type` (∈ `fact_types` ci-dessus), `unit` (uniquement si `type == "number"`), `value` (la valeur DÉCLARÉE par le compte — jamais déduite d'un document). Aucune validation de forme au moment de la sauvegarde (même logique que `weights`/`business_rules` : un profil peut temporairement contenir un fait incomplet) — la validation complète a lieu à `/validate`/`/activate`, voir plus bas. Exposé en lecture dans `GET /api/scoring-config`'s `profile.business_facts`.

**Critères personnalisés** — `PUT /api/scoring-config/policy` accepte un champ additif `custom_criteria` (même contrat "absent = inchangé") :
```json
{"custom_criteria": [
  {"id": "zone_couverte", "label": "Zone d'intervention couverte", "fact_key": "zone_intervention",
   "operator": "list_coverage", "weight": 10, "blocking": true, "pass_score": 100, "fail_score": 0},
  {"id": "frequence_ok", "label": "Fréquence de nettoyage compatible", "fact_key": "frequence_nettoyage",
   "operator": "numeric_threshold", "comparison": "provider_gte_ao",
   "weight": 5, "blocking": false, "pass_score": 100, "fail_score": 30}
]}
```
Chaque critère compare le fait EXTRAIT de l'AO (même `fact_key`) au fait DÉCLARÉ par le compte : `list_coverage` (le AO requiert-il un sous-ensemble de ce que le compte couvre ?), `numeric_threshold` avec `comparison` (`provider_gte_ao` : la valeur déclarée doit être ≥ celle de l'AO ; `provider_lte_ao` : l'inverse), `equality` (égalité stricte). `pass_score`/`fail_score` sont le barème — deux nombres explicites entre 0 et 100, jamais une formule libre. `blocking: true` fait échouer l'analyse en NO-GO si la condition n'est pas satisfaite, **quel que soit le poids** (un poids à 0 ne neutralise jamais une exigence bloquante).

**Budget de poids unique** : le total (poids des 12 critères fixes + poids de tous les `custom_criteria`) doit toujours valoir exactement 100 à l'activation — un compte non informatique peut mettre à 0 les critères fixes qui ne le concernent pas (ex. "Complexité technique", "Valeur stratégique") pour libérer du budget pour ses propres critères.

**Validation** (`/validate` et `/activate`, même fonction `validate_for_activation`) — nouvelle clé d'erreur `custom_criteria` :
```json
{"valid": false, "errors": {"custom_criteria": [
  "Le critère « zone_couverte » référence un fait métier inconnu ou non déclaré : 'zone_intervention'.",
  "Le critère « frequence_ok » : la comparaison « list_coverage » n'est pas compatible avec le type « number » du fait « frequence_nettoyage »."
]}}
```
Un critère référençant un fait non déclaré, un opérateur incompatible avec le type du fait, ou un barème hors de `[0, 100]` est refusé à l'activation — jamais silencieusement ignoré.

**Extraction (B05-T3)** : quand la politique active configure des `custom_criteria`, le vrai job (`jobs.py::_run_analysis`) demande en plus à l'extracteur les faits correspondants pour l'AO en cours — un appel LLM séparé, ciblé, additif (jamais fusionné au prompt d'extraction principal), avec un repli local déterministe si le LLM est désactivé/indisponible/incohérent. Un fait non trouvé dans l'AO (`extracted_facts[key].status != "found"`) rend le critère correspondant impossible à calculer honnêtement : `scoring_missing` contient `"custom:<id_du_critère>"`, et la décision devient `INCOMPLET` — jamais un résultat favorable ou défavorable fabriqué. Un décalage d'unité entre le fait déclaré et le fait extrait (ex. le compte a déclaré `"heures"`, l'AO exprime en `"jours"`) n'est **jamais converti automatiquement** — même traitement : incomplet, jamais deviné.

**Frontend (B27-T2)** : raccordé au lot 41 sur `/app/parametres` (éditeur de faits, constructeur de critères, total des poids, sauvegarde/reprise du brouillon, validation, simulation, activation confirmée avec `expected_active_version`, gestion 401/403/409/422 et changement d'organisation) ; ce qui a été prouvé dans un vrai navigateur est détaillé dans `docs/qa/lot_41_20260918/RAPPORT_LOT_41.md`.

### Compléments du lot 41 (comportement réel)

- **Catalogue** `business_facts_catalogue` : en plus de `fact_types`, `operators`, `numeric_comparisons`, il expose `operator_fact_types` (`{opérateur: [types compatibles]}`) et `unit_fact_types` (`["number"]`). C'est une **aide** à la saisie ; `/validate` et `/activate` revalident tout.
- **Identifiants** : `key` (fait) et `id` (critère) sont des slugs `^[a-z][a-z0-9_]{0,63}$`.
- **Valeurs déclarées** (`business_facts[*].value`, optionnelle dans un brouillon) : `number` = entier/flottant **fini** (jamais un texte, un booléen, `NaN`/`Infinity`) ; `list` = liste de textes non vides ; `boolean` = strictement `true`/`false` ; `text` = texte non vide. `0`, `false` sont des valeurs réelles ; un fait **référencé par un critère** doit avoir une valeur (`null` ou `[]` → erreur `custom_criteria`). Une valeur JSON non finie (`NaN`, `Infinity`) reçue dans `business_facts`/`custom_criteria` est **conservée sous forme de texte** (`"nan"`, `"inf"`) puis refusée à la validation : la sauvegarde ne plante pas et rien n'est stocké comme nombre.
- **Critères** : `weight` fini dans `[0, 100]` ; `pass_score`/`fail_score` finis dans `[0, 100]` ; `blocking` strictement booléen ; `comparison` obligatoire pour `numeric_threshold`.
- **Unités (lot 42, constaté avec claude-sonnet-4-6)** : l'extraction restitue l'unité **telle qu'écrite dans le document** (« par semaine ») alors qu'un compte peut avoir déclaré un slug (`par_semaine`). La comparaison normalise **l'orthographe** d'une même unité (casse, accents, espaces/`_`/`-`) ; deux unités différentes (« par mois » ≠ « par semaine ») restent incompatibles et **aucune conversion** n'est jamais faite (critère incalculable → `INCOMPLET`).
- **Calcul défensif** : une donnée historique incohérente (critère non objet, poids infini, valeur non numérique…) n'est jamais convertie ni favorisée : le critère est déclaré incalculable (`scoring_missing` contient `custom:<id>` ou `custom:configuration`) et la décision est `INCOMPLET`.
- **Extraction locale** (repli sans LLM) : `ExtractedFact.status` ∈ `found` / `absent` / `ambiguous`, avec `reason` (`partial_list_possible`, `polarity_unclear_or_contradictory`, `conflicting_values`). Une liste reconnue mais possiblement incomplète (« Lyon et Marseille » face à un vocabulaire `["Lyon"]`) est `ambiguous`, jamais une couverture complète ; un booléen n'est jamais déduit de la seule présence du libellé (« Travail de nuit : non » donne `false`). `requested_facts[*].recognition_vocabulary` (ex-`known_values`) est un vocabulaire de **reconnaissance**, pas l'exigence de l'AO.
- **Simulation** (`POST /simulate`) : mêmes faits/critères que l'analyse réelle, appliqués au **brouillon** et au profil du propriétaire ; extraction 100 % locale (jamais Claude), aucune recherche externe de société, rien n'est écrit dans l'historique, le brouillon n'est pas activé. Une extraction locale insuffisante donne `INCOMPLET`.
- **Ordre des faits** : un objet JSON n'a pas d'ordre garanti ; le formulaire les trie par libellé.
- **Sans brouillon** : le formulaire part de la **version active** de l'utilisateur (jamais de valeurs par défaut) ; enregistrer crée un nouveau brouillon, la version active n'est modifiée qu'à l'activation.
- **Compte à plusieurs organisations sans sélection** : les pages `/app/*` répondent `409` avec une page de choix (`org_choice.html`) au lieu d'une erreur sans issue.

### Compléments du lot 43 (comportement réel)

- **Configuration privée obligatoire** : une NOUVELLE analyse ou simulation n'utilise que la configuration résolue côté serveur pour la même paire organisation + propriétaire (profil et faits, politique active — ou brouillon pour la simulation —, plan de capacité). `ScoringEngine.score(..., policy)` exige un `ScoringPolicySnapshot` : `policy=None` lève `PrivateConfigurationRequired` (`missing = "scoring_policy"`), de même `CapacityAnalyzer.analyze(ao, plan)` avec `plan=None` (`missing = "capacity_plan"`). Les routes refusent en amont comme avant (`409 CAPACITY_NOT_CONFIGURED`, `409 SCORING_NOT_CONFIGURED`, `404` pour une simulation sans brouillon) ; un job qui atteindrait ces états malgré tout s'arrête en erreur contrôlée, sans résultat. **Aucune politique n'est créée automatiquement.**
- **Valeurs de démonstration retirées** : poids, technologies maîtrisées, certifications détenues, seuils GO/SOUS RÉSERVE (88/60), littéraux de règles (50 000 €, 95 %, 4 technologies, pénalité 20) qui remplaçaient une politique absente ; `SCORING_THRESHOLD_GO`/`SCORING_THRESHOLD_SOUS_RESERVE` et `CAPACITY_LOAD_THRESHOLD_*` de `config.py` ; plan de capacité par défaut (78 %, six pôles informatiques, minimum 10 %) et fichier de capacité global. `default_weight` du catalogue `GET /api/scoring-config` reste un **modèle proposé** par l'écran sur action explicite de l'utilisateur, jamais appliqué par le moteur.
- **Ce qui n'a PAS changé** : les formules des douze critères fixes, les politiques actives, les résultats historiques, le calcul des critères personnalisés, `_same_unit`, la distinction absent/`0`/`false`. Les barèmes fixes restants (notes par défaut, paliers de budget, mots-clés) sont inventoriés dans `docs/qa/lot_43_20260919/RAPPORT_LOT_43.md` (passation) : ce ne sont pas des paramètres du compte.
- **Historique global JSON** : `data/historique/historique_ao.json` (partagé entre comptes, écrit seulement pour l'ancienne interface Streamlit) n'est plus écrit ; le fichier existant est une donnée conservée. L'historique de référence reste la base ; l'analyse durable par job (`data/historique/analyses/<job_id>.json`) et sa relecture sont inchangées.

**Contrôle d'omission des listes extraites par le LLM** (`ao_extractor._llm_list_incomplete_reason`, sans appel LLM supplémentaire). Pour un fait de type `list` renvoyé par le modèle, la liste est rapprochée du document ; toute discordance donne `status="ambiguous"`, `provenance="llm"`, `value=null` et une `reason`, **sans repli local** (qui pourrait reconstruire une liste favorable), donc critère incalculable → `INCOMPLET`.

| `reason` | Détectée quand |
|---|---|
| `llm_list_may_omit: <élément>` | un passage **qui reprend un mot du libellé/identifiant du fait** énumère un élément que le modèle n'a pas renvoyé |
| `llm_list_non_exhaustive_marker` | un tel passage annonce une liste non exhaustive (« etc. », « notamment », « entre autres », « par exemple », « … ») |
| `llm_value_not_in_document` | une valeur renvoyée par le modèle n'apparaît pas dans le document (comparaison sans accent, casse ni ponctuation) |

Formulations couvertes, dans un passage ancré sur le libellé : éléments capitalisés reliés par `,` `;` `/` `&` `et` `ou` `ainsi que` (« sites de Lyon et de Marseille ») ; puces sous un en-tête finissant par `:` ; éléments courts (≤ 3 mots, minuscules acceptées) après le `:` du libellé (« Certifications exigées : ISO 27001, HDS et SecNumCloud ») ; les parenthèses (« Lyon (69) ») sont ignorées.
Ce qui n'est **pas** fait : une exigence formulée sans aucun mot du libellé du fait n'est pas recoupée (le résultat du modèle est alors pris tel quel) ; une ville citée hors d'un passage ancré (adresse ou siège de l'acheteur) n'est jamais une exigence ; l'exigence n'est jamais construite à partir du vocabulaire déclaré par le compte ; une citation ne prouve pas la complétude d'une liste. Incertitudes : un passage ancré contenant un élément capitalisé étranger à l'exigence (« sites de Lyon, France ») donne un faux `INCOMPLET` (prudent, jamais favorable) ; un nom normalisé par le modèle (« IDF » pour « Île-de-France ») donne `llm_value_not_in_document`. Aucune promesse de détection universelle.

### Compléments du lot 44 — critères explicites et versionnés (comportement réel)

**Modèle.** Une politique est une **liste de critères** évalués par UN moteur technique (`ScoringEngine.score`) ; chaque critère nomme un évaluateur du **catalogue fermé** (`src/agents/criteria_catalogue.py`) et porte ses paramètres. Aucun paramètre n'est exécutable (nombres, listes de textes, paliers, choix dans une liste). Reste technique et codé : bornes 0–100, arrondi à une décimale, somme pondérée, ordre de décision *bloqueur > INCOMPLET > seuils*, catalogue et implémentations.

```json
{"id": "budget", "label": "Budget estimé (comparaison au seul budget)", "evaluator": "numeric_tiers",
 "params": {"source": "budget", "fact_key": null, "tiers": [{"at_least": 100000, "score": 90}], "below_score": 20,
            "zero_score": null, "minimum_blocking": null},
 "weight": 50, "blocking": false, "on_missing": {"mode": "incomplete"}, "enabled": true, "disabled_reason": null}
```

**Évaluateurs** (`GET /api/scoring-config` → `criteria_catalogue`, avec schéma des paramètres, données requises et ce qui n'est pas calculable) : `list_coverage`, `numeric_threshold`, `equality` (faits métier, unités jamais converties) ; `numeric_tiers` (budget **seul** ou fait numérique), `technology_coverage`, `technology_count_tiers`, `keyword_set_match`, `reference_evidence`, `capacity_availability`, `certifications_required` ; évaluateurs de compatibilité `legacy_tight_deadline_v1`, `legacy_contract_clauses_v1`, `legacy_sector_known_v1`, `legacy_client_solvency_v1` (règles historiques par mots-clés ou libellés, identifiées par version, non proposées pour un nouveau critère). Non disponible : rentabilité/marge (coûts et périmètre non modélisés), délai/secteur/stratégie (à exprimer par des faits métier).

**Nouvelle politique.** `PUT /api/scoring-config/policy` avec `{"criteria": [...], "settings": {...}, "threshold_go", "threshold_sous_reserve"}` crée/reprend le brouillon au format explicite (`origin="user"`), **vide par défaut** : aucun des douze critères n'est obligatoire, aucune note de remplacement, aucun seuil. Un enregistrement au format historique (`weights`/`business_rules`/`custom_criteria`) sur un brouillon au nouveau format répond `409 DRAFT_FORMAT_CONFLICT`. Activation (`/validate`, `/activate`, mêmes règles) : au moins un critère actif, poids des critères actifs = 100 exactement, seuils fournis, raison sociale ; erreurs sous les clés `criteria`, `settings`, `thresholds`, `profile`, `business_facts`. Un critère bloquant ne peut avoir ni note d'hypothèse ni « non applicable » ; un critère désactivé exige un motif et un poids 0 (réallocation explicite).

**Inconnu, non applicable, bloqueurs.**
- Donnée requise absente ou ambiguë ⇒ critère `manquant` (score 0 dans la somme, jamais une note) ⇒ décision `INCOMPLET`, `score_provisoire=true`, jamais un NO-GO ni une probabilité de succès. `0` et `false` sont des valeurs réelles.
- `on_missing.mode="explicit_score"` : note choisie par l'utilisateur, affichée comme **hypothèse** (`etat="hypothese"`, `scoring_assumptions`). `"not_applicable"` : critère exclu avec son motif (`etat="non_applicable"`), ni satisfait ni échoué ni poids 0 ; `settings.not_applicable_rule="incomplete"` (défaut) ⇒ INCOMPLET, aucune redistribution ; `"renormalize"` + `not_applicable_rule_confirmed=true` (règle validée explicitement) ⇒ score calculé sur les seuls poids applicables. Le critère écarté ne reçoit jamais 100.
- Un bloqueur confirmé donne NO-GO même avec d'autres inconnues ; un poids nul ne le désactive pas. Les certifications sont une règle visible (`block_when_missing`, `when_extraction_unknown` = `missing` | `treat_as_none`) : l'absence d'extraction n'est pas une preuve de conformité pour une nouvelle politique (l'historique garde `treat_as_none`). Toute certification déclarée compte comme détenue (la distinction déclarée/vérifiée n'est pas exploitée, limite connue).

**Migration 0010 et compatibilité.** Colonnes `criteria_version` (schéma des critères, **distinct** de la `version` métier), `criteria`, `settings`, `origin` (`legacy` | `user`). Le backfill matérialise les règles historiques des politiques existantes (mêmes poids, seuils, notes, règles de manque — règle jamais configurée = `settings.legacy_unconfigured_rules`, toujours INCOMPLET) avec `origin="legacy"` : **représentation, pas approbation** ; statuts/propriétaires/portées conservés, rien n'est activé ; idempotent ; `analyses.result_data` jamais touché. Pour une politique `legacy`, les colonnes historiques restent autoritatives (le brouillon les re-matérialise à chaque enregistrement) ; le downgrade est **refusé** dès qu'une politique `user` existe, sinon sans perte. L'API renvoie `criteria`, `settings`, `criteria_version`, `origin`, `origin_label` (« Politique historique migrée » / « Nouvelle politique »).

**Résultats.** Additif : `CriterionScore.etat/evaluateur/critere_id/bloquant/motif`, `ScoringResult.criteria_version/policy_origin/score_provisoire/scoring_assumptions/scoring_not_applicable` (`None`/vide sur un résultat antérieur, jamais fabriqués). Nombre de critères variable dans la page, le PDF et le DOCX, avec états évalué / hypothèse / non évalué / non applicable. Textes (justifications, recommandations, prompts) sans identité ESN, seuils forces/faiblesses 78/60 ou délais inventés : `strengths_at_least` / `weaknesses_below` sont des réglages facultatifs de la politique. Message des technologies non maîtrisées trié (déterministe pour les nouveaux résultats).

**Une seule configuration.** `src/web/scoring_context.py::resolve_scoring_context(db, organization_id, owner_user_id, source)` est appelée par le job (`"active"`) et la simulation (`"draft"`) : une lecture cohérente (politique, profil, capacité), copiée en données simples ; `ScoringConfigurationMissing` sinon, aucun repli global. La simulation appelle toujours zéro fournisseur et n'écrit pas dans l'historique.

**Reliquats identifiés (compatibilité v1, non transposables fidèlement).** Détection par mots-clés du délai (« 4/6/8 semaines », « impératif »…) et des clauses (« pénalité », « garantie », « sla »), test « secteur de l'acheteur renseigné », libellés de solidité (« Bonne »/« A verifier » sans accent ; « À vérifier » accentué reste dans la branche prudente), et la note par défaut « aucun mot-clé détecté ». Ils restent des évaluateurs `legacy_*_v1` tant que l'utilisateur ne les remplace pas par des faits métier. Les anciennes valeurs de notes de repli (65/65/40/80/60…) sont désormais des paramètres visibles, marqués « hypothèse » quand ils s'appliquent à une donnée absente.

## 9. Ce qui reste explicitement ouvert (ne pas présenter comme résolu)

- L'écran `/app/parametres` couvre désormais profil, faits métier, critères personnalisés, poids fixes, validation, simulation et activation (lot 41) ; voir le rapport du lot pour ses limites (recette visuelle sur Chrome headless uniquement, pas de test de bout en bout automatisé du frontend).
- La distinction "declaree" vs "verifiee" pour une certification est stockée mais pas encore exploitée différemment par le moteur (§5).
- `duree_projet_mois` reste sans effet sur "Faisabilité délai" (décision produit non tranchée, héritée de B04 — voir `docs/qa/b04_20260914/BACKLOG_CORRECTIONS_SCORING.md`).
- Le cas "À vérifier" (solidité financière) reste dans sa branche prudente actuelle — ne pas le normaliser vers la branche favorable sans décision produit explicite (même dossier).
- Lot 44 : les barèmes des douze critères historiques sont devenus des paramètres explicites, mais les politiques migrées gardent leurs valeurs historiques (compatibilité) jusqu'au choix de l'utilisateur ; rien ne prouve que ces valeurs conviennent à un métier donné.
- DEFECT-B04-04 (absence de plancher à 0 pour un score de preuve RAG négatif/infini) n'a pas été retouché par ce ticket.
- La concurrence multi-processus réelle (deux activations strictement simultanées sur des connexions DB séparées) n'a pas été testée avec PostgreSQL — seul le scénario séquentiel contrôlé (§6) l'a été ; l'index unique partiel en base est le filet de sécurité pour le cas non testé.

### Complément du lot 45 — nom du critère dans les messages d'incomplétude

`scoring_missing` reste la liste **technique** (`criterion:<id>`, `custom:<id>`, `not_applicable:<id>`, `criteria:configuration`, clés de règles historiques) : contrat inchangé, jamais réinterprété. Champ additif `scoring_missing_labels` (`{code: libellé}`, vide sur un résultat antérieur) : libellé du critère concerné **dans la version de politique qui a produit le résultat**, figé au calcul (une politique plus récente ne le change jamais). La page résultat, le PDF/DOCX et la simulation affichent ces libellés ; pour un résultat antérieur au lot 45, `ScoringResult.scoring_missing_display()` retombe sur les lignes `criteres` du **même** résultat (`critere_id` → `nom`) puis, faute de correspondance, sur le code lui-même. Le texte de la recommandation « Compléter les données… » des **nouveaux** résultats nomme les critères ; les anciens textes stockés ne sont jamais réécrits. Aucun score, aucune décision ni aucun barème n'est modifié.
