# Lot 49 — Contrat API : compléter une analyse (nouvelle révision)
> Mis à jour au lot 49 bis (2026-09-24) : le profil prestataire n'est plus relu en direct au moment du calcul — voir §4 et §6. Détails : `docs/qa/lot_49_bis_20260924/RAPPORT_LOT_49_BIS.md`.

Destiné au développeur frontend. Toutes les routes exigent une session authentifiée (`require_active_starter_user`/`require_permission("analysis:create")`) ; les mutations exigent `X-CSRF-Token`. Une analyse qui appartient à quelqu'un d'autre, à une autre organisation, ou dont l'appartenance ne peut plus être vérifiée (organisation révoquée) est **indistinguable d'une analyse inexistante** (404).

## 1. Principe

Une analyse **INCOMPLET** (ou dont un critère manque, même à côté d'un bloqueur confirmé NO-GO) peut être **complétée** : le compte déclare l'information manquante, le serveur **recalcule** une **nouvelle révision**, liée à l'analyse d'origine, jamais à sa place. L'analyse d'origine, son résultat, ses livrables et son historique restent inchangés et consultables.

Rien n'est réextrait, rerecherché en externe, ni resélectionné par un nouvel appel LLM de sélection : la révision réutilise les faits déjà extraits, le profil acheteur déjà enrichi et les références déjà retenues de l'analyse d'origine, et ne relit que la politique de scoring — **exactement la version qui a produit le résultat d'origine**, jamais la politique active actuelle si elle a changé depuis.

## 2. `GET /api/analyze/{job_id}/completion`

```json
{
  "can_complete": true,
  "reason": null,
  "parent_job_id": "abc123",
  "existing_revision_job_id": null,
  "needs": [
    {
      "id": "custom:zone_couverte", "subject": "ao", "kind": "declarable", "label": "Zone d'intervention",
      "criteria": ["zone_couverte"], "field_key": "zone_intervention", "type": "list", "unit": null,
      "reason": "ao_fact_missing", "action": "declare_ao", "current": null, "note": null
    }
  ],
  "capacity": {"frozen": {"...": "CapacityResult gelé"}, "current": {"...": "CapacityResult recalculé avec le plan ACTUEL"}, "changed": true},
  "policy_version": 3,
  "profile_version": 7
}
```
`profile_version` (lot 49 bis) : la version **actuelle** du profil prestataire du compte (`ProviderProfile.version`), au moment de cette prévisualisation — à renvoyer telle quelle comme `expected_profile_version` dans la requête de complétion dès qu'au moins un `declare_prestataire` est confirmé (voir §3). Elle ne décrit PAS le profil gelé dans l'analyse d'origine (celui-là n'est jamais réexposé tel quel par cette route — voir §6) ; elle sert uniquement de jeton de concurrence optimiste pour l'écriture permanente.

`can_complete=false` avec `reason`:
- `"job_not_complete"` — l'analyse n'est pas terminée (rien à compléter).
- `"policy_version_unavailable"` — la politique exacte qui a produit ce résultat n'existe plus pour ce compte : **une nouvelle analyse est nécessaire**, ce résultat ne peut pas être recalculé de façon fiable.
- `"parent_snapshot_unavailable"` (lot 49 bis) — ce résultat a été calculé avant l'introduction de `ScoringResult.provider_snapshot` (aucune trace figée des entrées prestataire utilisées) : **une nouvelle analyse est nécessaire**, jamais une reconstruction à partir du profil actuel.
- `"revision_already_exists"` — une révision existe déjà (`existing_revision_job_id`) : afficher un lien, pas un second formulaire.

Chaque `need` :
- `kind` = `"declarable"` (un champ à saisir), `"conflict"` (deux valeurs contradictoires, jamais résolu en tapant une valeur — un lien vers une nouvelle analyse de dossier corrigé est la seule action), `"policy"` (une lacune de configuration de la politique — lien vers `/app/parametres`, jamais un champ de saisie ici) ou `"informational"` (ex. zéro référence retenue — jamais un état manquant fabriqué, ni une note changée).
- `subject` = `"ao"` (donnée propre à cette analyse), `"acheteur"` (le client cité, déclaration non vérifiée, ne relance jamais Pappers), `"prestataire"` (le profil du compte — écrire une valeur ici est un **enregistrement permanent**, gated par `confirm_profile_write`), `"politique"`, `"references"`.
- `criteria` : identifiants des critères de la politique qui partagent CE besoin (dédupliqué — jamais fusionné entre AO et prestataire).
- `action` : `"declare_ao"` | `"declare_acheteur"` | `"declare_prestataire"` | `"configure_policy"` | `"add_reference"` | `"none"`.

`capacity.changed` compare le résultat de capacité **gelé** dans l'analyse d'origine à celui recalculé (sans être appliqué) avec le plan de capacité **actuellement enregistré** du compte — l'appliquer à la révision exige `apply_current_capacity: true` explicite dans la requête de complétion.

## 3. `POST /api/analyze/{job_id}/complete`

```json
{
  "items": [{"need_id": "custom:zone_couverte", "value": ["Lyon", "Villeurbanne"]}],
  "confirm_profile_write": false,
  "apply_current_capacity": false,
  "expected_profile_version": 7
}
```
- `items[].need_id` doit correspondre à un besoin **recalculé côté serveur** au moment de la requête (jamais celui envoyé par le client) — un identifiant inconnu, un besoin non `"declarable"`, ou un doublon est refusé (`422`).
- La valeur est validée selon le `type` du besoin : `number` (fini, jamais booléen), `list` (1 à 50 textes non vides, 200 caractères chacun), `boolean` (strict), `text` (non vide, 500 caractères maximum). L'unité n'est jamais choisie par le client : celle du fait déclaré s'applique telle quelle.
- Un besoin `subject: "prestataire"` sans `confirm_profile_write: true` est refusé (`422 CONFIRMATION_REQUIRED`) — rien n'est écrit dans le profil sans confirmation explicite.
- `expected_profile_version` (lot 49 bis) — **obligatoire** dès qu'au moins un item confirmé est `subject: "prestataire"` : doit être exactement la valeur `profile_version` vue par le client à la dernière prévisualisation (`GET .../completion`). Un profil modifié entre-temps (même sur un champ sans rapport) fait échouer la requête (`409 PROFILE_CHANGED`) **avant toute écriture**, plutôt que de fusionner à l'aveugle dans un profil que le client n'a jamais vu ; le client réaffiche alors l'aperçu actualisé (nouveau `GET`) avant de resoumettre. Ignoré si aucun item n'est `declare_prestataire`.
- Aucun champ hors de `{need_id, value}` par élément, aucune clé hors de `{items, confirm_profile_write, apply_current_capacity, expected_profile_version}` au niveau racine ne modifie quoi que ce soit — refusé (`422 UNKNOWN_FIELD`) plutôt qu'ignoré en silence. Le score, la décision, les poids, les seuils et la provenance d'extraction ne sont **jamais** des champs acceptés ici.

**Réponses d'erreur** :
| Statut | `error_code` | Cas |
|---|---|---|
| 400 | `NOTHING_TO_APPLY` | ni `items`, ni `apply_current_capacity` |
| 404 | — | analyse introuvable/pas la vôtre |
| 409 | `JOB_NOT_COMPLETE` | l'analyse d'origine n'est pas terminée |
| 409 | `REVISION_ALREADY_EXISTS` | une révision existe déjà (`existing_job_id`) — complément fondé sur une révision périmée |
| 409 | `ORIGINAL_POLICY_UNAVAILABLE` | la politique d'origine n'existe plus : nouvelle analyse nécessaire |
| 409 | `PARENT_SNAPSHOT_UNAVAILABLE` (lot 49 bis) | ce résultat n'a pas de `provider_snapshot` figé (antérieur au lot 49 bis) : nouvelle analyse nécessaire |
| 409 | `PROFILE_VERSION_REQUIRED` (lot 49 bis) | un item `declare_prestataire` est confirmé sans `expected_profile_version` |
| 409 | `PROFILE_CHANGED` (lot 49 bis) | `expected_profile_version` ne correspond plus au profil actuel (`current_profile_version` dans la réponse) — rien n'est écrit |
| 409 | `CAPACITY_NOT_CONFIGURED` | `apply_current_capacity: true` mais la capacité n'est plus configurée |
| 422 | `UNKNOWN_NEED` / `NOT_DECLARABLE` / `DUPLICATE_NEED` / `INVALID_VALUE` / `CONFIRMATION_REQUIRED` / `UNKNOWN_FIELD` | requête invalide, rien n'est écrit |
| 429 | `JOB_QUEUE_SATURATED` | file d'attente pleine — la révision n'est pas créée |

**Succès (200)** : `{"job_id": "<nouveau job>", "parent_job_id": "<job d'origine>", "changes": [{"field", "before", "after"}, ...]}`. Le nouveau job se suit exactement comme une analyse normale (`GET /api/analyze/{job_id}/status`, puis `/app/resultats/{job_id}`).

## 4. Ce que la révision réutilise, ce qu'elle recalcule

| Donnée | Origine dans la révision |
|---|---|
| Faits extraits de l'AO (`AOContext.extracted_facts`, champs fixes) | Copie de l'analyse d'origine, augmentée des compléments `ao` déclarés — **aucune réextraction** |
| Profil acheteur (`CompanyProfile`) | Copie figée de l'analyse d'origine, augmentée d'un éventuel complément `acheteur` (ex. secteur) — **aucun nouvel appel Pappers** |
| Références internes (`evidence_pack`, synthèse RAG) | Copie figée telle quelle — **aucune nouvelle recherche ni sélection LLM** |
| Politique de scoring (critères, seuils) | La version **exacte** qui a produit le résultat d'origine (`scoring_policy_version`), jamais la politique active actuelle |
| Profil prestataire (compétences, certifications, faits déclarés) | **Gelé** (`ScoringResult.provider_snapshot`, figé à l'analyse d'origine), amendé **uniquement** par les faits `declare_prestataire` **explicitement confirmés de cette complétion** — voir §6 (lot 49 bis ; avant, lecture fraîche du profil actuel à chaque révision) |
| Capacité de l'équipe | Gelée par défaut ; recalculée avec le plan **actuellement enregistré** seulement si `apply_current_capacity: true` |
| Enrichissement des justifications (LLM) | Ré-exécuté (texte seulement — jamais la décision/le score) : c'est l'étape normale de tout calcul, pas une resélection |

## 5. Dossier d'AO (lot 47 bis) — lien corrigé

`GET /api/analyze/{job_id}/dossier` résout désormais via une table de liaison (`ao_dossier_job_links`) qui garde une ligne pour **chaque** job jamais associé au dossier (soumission d'origine, chaque `resume`, chaque révision) — un ancien job ne perd plus cet accès quand un job plus récent est lié. `POST /api/analyze/{job_id}/resume` (reprise d'un job interrompu) et la complétion (nouvelle révision d'un job **terminé**) restent deux actions distinctes avec leurs propres gardes (`resume` refuse toujours un job `running`/`done` ; la complétion exige au contraire un job `done`).

## 6. Lot 49 bis — le profil prestataire d'une révision est gelé, jamais relu en direct

**Défaut corrigé** : avant ce lot, `_run_revision` appelait `resolve_scoring_context(source="version")`, qui relit le profil prestataire **actuel** (compétences, certifications, faits déclarés) au moment où le worker tourne — un changement de profil **sans rapport** avec la complétion en cours (une compétence ajoutée, un fait métier modifié dans un autre onglet, entre l'analyse d'origine et la complétion, ou même entre la soumission et l'exécution effective du worker) pouvait silencieusement changer le résultat recalculé. Preuve et détails : `docs/qa/lot_49_bis_20260924/RAPPORT_LOT_49_BIS.md`.

**Mécanisme** : chaque `ScoringResult` porte désormais `provider_snapshot` (`raison_sociale`, `competences`, `certifications`, `business_facts` — figé à l'instant du calcul, jamais réécrit après coup). `apply_completion` construit, **à la soumission**, l'entrée effective de la révision : une copie profonde de `provider_snapshot` du **parent**, amendée uniquement des faits `declare_prestataire` explicitement confirmés dans **cette** requête (rien d'autre du profil actuel n'est importé, même s'il a changé entre-temps). Cette entrée est persistée dans `RevisionSpec.provider_snapshot` avant l'enqueue ; `_run_revision` ne lit plus jamais `ProviderProfile` — un changement de profil survenant après la soumission n'a donc aucun effet sur le calcul, quel que soit le moment où le worker s'exécute réellement.

**Écriture permanente vs entrée de calcul** : un fait `declare_prestataire` confirmé continue d'écrire dans le profil **permanent** du compte (comme au lot 49), en ne fusionnant que le champ confirmé dans les faits **actuels** — les autres champs du profil (identité, autres faits) sont préservés tels quels, jamais remplacés par l'ancien snapshot. Cette écriture est gardée par `expected_profile_version` (§3) : un profil modifié depuis le dernier aperçu refuse (`409 PROFILE_CHANGED`) plutôt que d'écraser un changement que le compte vient de faire ailleurs.

**Ce qui n'a pas changé** : le mode "profil complet actuel" (montrer un diff avant soumission, confirmation séparée non pré-cochée) reste possible pour une future itération d'UI mais n'a **pas** été construit dans ce lot — un correctif minimal (faits confirmés uniquement) suffit et couvre le défaut identifié. La capacité conserve sa propre confirmation (`apply_current_capacity`), inchangée.

## 7. Lot 53 — le « avant/après » d'une révision est figé et réutilisé, jamais recalculé à l'affichage

**Constat** : `POST .../complete` renvoie déjà `changes` (le `{field, before, after}` calculé par `apply_completion`) — mais cette liste n'était **jamais persistée** : rechargée plus tard, la page de résultat de la révision ne pouvait plus montrer ce qui avait changé, ni le PDF/DOCX correspondants.

**Mécanisme (additif, aucune migration)** : `changes` porte désormais aussi `subject`/`field_key`/`origin`/`source_json` (mêmes valeurs que la ligne `AnalysisComplement` correspondante, lot 52) et est **frozen** sur `ScoringResult.completion_changes` (nouveau champ Pydantic, JSON déjà flexible — `analyses.result_data` — donc aucune migration SQL) par `_run_revision` au moment du calcul, via `RevisionSpec.changes` (nouveau champ, défaut `[]`). Le worker ne calcule jamais ce champ lui-même : il reçoit exactement ce qu'`apply_completion` a déjà déterminé à la soumission.

**Restitution** : `src/web/result_presentation.py::build_result_view` construit, à partir de CE `ScoringResult` et — pour une révision — du `ScoringResult` du **parent** (relu via `jobs.get_job`, jamais recalculé), une projection typée : libellé de politique (`criteria_version`/`policy_origin`, déjà figés depuis le lot 44), nombre de références **distinctes** (`group_evidences_by_reference`, lot 51 bis/recette corpus, jamais le nombre brut de passages), et la liste des critères dont le score/état diffère réellement entre le parent et la révision. Réutilisée telle quelle par la page de résultat (`templates/app_result.html`) et par `src/livrables/document_generator.py` (PDF et DOCX) — aucune logique dupliquée entre les deux.

**Historique** : un résultat calculé avant ce lot a `completion_changes=[]` par défaut (jamais fabriqué après coup) ; une révision dont le parent n'est plus accessible (`jobs.get_job` renvoie `None`) affiche son propre `completion_changes` (toujours disponible, figé sur elle-même) mais aucun tableau de critères avant/après (rien à comparer, jamais inventé).
