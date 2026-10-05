# B11-T1 — La base SQL est la référence des analyses enregistrées

Défaut corrigé : `jobs.get_job()` lisait `_JOBS` (mémoire du process) puis un
FICHIER JSON LOCAL, et n'interrogeait **jamais** la table `analyses` — alors que
`_persist_to_database` y écrivait déjà une ligne complète. Après un redémarrage
(ou sur une autre instance), une analyse présente en base devenait illisible via
`/api/analyze/{job_id}/status`, `/api/download/{job_id}/{kind}` et
`/app/resultats/{job_id}`. La base était en écriture seule pour la lecture.

## 1. Chaîne de lecture (`jobs.get_job(job_id)`)

| Ordre | Source | Rôle |
|---|---|---|
| 1 | `_JOBS` (mémoire) | Cache in-process. Seule source valable pour un job **en cours** (aucune ligne durable n'existe encore). |
| 2 | ligne SQL `analyses` (`_load_from_database`) | **La référence durable.** |
| 3 | JSON local (`_load_persisted`) | Dernier recours **uniquement** pour un job sans `user_id` (jamais persisté en base, par conception). |

- **Aucun fallback global.** Pas de ligne → `get_job` renvoie `None` → le 404 des
  routes s'applique, exactement comme pour un job réellement inexistant. Aucun
  substitut plausible n'est fabriqué.
- Le `Job` reconstruit porte les `user_id`/`organization_id` **de la ligne**, donc
  le contrôle inchangé `job.user_id != current_user.id` des routes continue de
  refuser l'analyse d'autrui. Les routes n'ont pas été modifiées.
- Une base absente/injoignable n'est pas une erreur de lecture : retour `None`,
  le JSON est tenté ensuite, aucune exception ne remonte dans une route.
- **Une seule** fonction de reconstruction, `_job_from_snapshot`, est partagée par
  les deux sources : la dégradation B18-T2 (`_sanitize_legacy_result_data`,
  `HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE`) s'applique donc identiquement à un
  `result_data` corrompu venu de SQL et à un venu du JSON.
- La lecture ne réécrit jamais ce qu'elle lit (copie privée via `deepcopy`), et
  ne **recalcule** rien : `decision`, `score_global`, `scoring_completeness`,
  `scoring_missing`, `data_integrity` et le `scoring_policy_version` épinglé sont
  relus tels quels. Activer une nouvelle `ScoringPolicy` ne change donc jamais le
  sens d'une analyse déjà exécutée. `INCOMPLET` (B06-T4) survit verbatim : jamais
  promu en décision réelle, jamais remis à `0`/`"GO"`.
- Une dégradation est à sens unique : une vérification fraîche ne peut pas
  **promouvoir** un `data_integrity` déjà enregistré comme non-"ok" vers "ok".

## 2. Contrat d'upsert (`src/web/database/repositories/analyses.py`)

```python
upsert_analysis(db, *, user_id, organization_id, job_id,          # job_id OBLIGATOIRE
                result_data, title=None, client_name=None, sector=None,
                score=None, decision=None, budget=None,
                technologies=None, summary_data=None) -> Analysis

upsert_document(db, *, analysis_id, user_id, organization_id, filename,
                original_filename, storage_path, mime_type, file_size) -> AnalysisDocument
```

- `upsert_analysis` est idempotent par `job_id` : deux appels ⇒ **une** ligne,
  portant les valeurs du **second**. Si la ligne existe avec un `user_id` ou un
  `organization_id` différent, elle lève `AnalysisOwnershipConflict` (exporté par
  le même module) — jamais d'écriture silencieuse sur la ligne d'autrui. Les
  colonnes de propriété d'une ligne existante ne sont jamais réécrites.
- `upsert_document` est idempotent par `(analysis_id, mime_type)` : un nouveau
  rendu **met à jour** la ligne existante de ce type au lieu d'en créer une
  seconde. **C'est la fonction que la régénération de document (B19-T2) doit
  appeler.** `mime_type=None` ne peut pas être réconcilié et est toujours inséré.
- `create_analysis` (INSERT simple) reste pour l'outillage qui sait que la ligne
  ne peut pas exister (`scripts/migrate_history_to_postgresql.py`) et les
  fixtures. La normalisation des colonnes (troncatures, `""`→`None`) est définie
  une seule fois (`_row_fields`), partagée INSERT/UPDATE : pas de dérive.

## 3. Séquence d'enregistrement réordonnée (`src/web/jobs.py`)

```
scoring + enrichissement → job.result = result
  → (a) _save_analysis_snapshot(job)      ← LIGNE D'ANALYSE EN SQL, AVANT tout rendu
  → _generate_ai_content → _generate_documents (I/O disque)
  → (a') _save_analysis_snapshot(job)     ← même ligne rafraîchie (ai_content, files)
  → (b) _attach_documents_to_database(job) ← lignes AnalysisDocument, seulement si fichiers
```

Pourquoi : le rendu n'a aucun rapport avec la réussite du calcul. Avant, la
ligne n'était écrite qu'**après** le rendu — un crash entre "score calculé" et
"PDF écrit" perdait définitivement le résultat pour la base de référence.
(a') est possible sans risque de doublon précisément parce que l'écriture est un
upsert sur `job_id` ; il rattrape aussi un échec transitoire de (a).
Un `session_scope()` par opération, **jamais** une transaction ouverte à travers
l'étape de rendu (qui est de l'I/O disque, pas du travail base).

`_persist_to_database(job)` subsiste comme point d'entrée unique "tout écrire"
(chemin de réparation/rejeu, et le test de révocation de membership existant) —
`_run_analysis` ne l'appelle plus, il appelle (a) et (b) à leurs moments propres.

## 4. Codes d'erreur

- Un échec d'écriture SQL — au point de sauvegarde **précoce** comme au
  rattachement des documents — est signalé par le code public **existant**
  `persistence_failed` (`PERSISTENCE_FAILED_ERROR_CODE`), avec `status="done"` :
  la forme établie par B10-T1 (l'analyse a réussi, `job.ao`/`job.result` sont
  réels, seule la durabilité a échoué). **Aucun nouveau code public** n'est
  introduit : côté appelant le fait est identique ; quel magasin a échoué est un
  détail de log (message distinct par opération côté serveur).
- Le rendu reste distinct : `document_generation_failed`, `status="error"`.
- Un refus **délibéré** d'écrire (pas d'`user_id`, pas d'`organization_id`,
  membership révoquée en cours de job — politique B02 préexistante) n'est **pas**
  un échec de stockage et n'est pas signalé comme tel.

## 5. Migration

**Aucune migration ajoutée** (pas de `0007_*`). `upsert_document` réconcilie au
niveau applicatif via `get_document_for_analysis`, déjà scopé
analysis_id/user_id/organization_id/mime_type ; la FK composite
`(analysis_id, organization_id)` interdit déjà au niveau base un document
incohérent ; `analyses.job_id` est déjà `unique=True`, ce qui suffit à
`upsert_analysis`. Une `UniqueConstraint("analysis_id", "mime_type")` ne
fermerait qu'un double rattachement **concurrent** de la même analyse, que
l'application ne produit pas (un thread worker par job, une régénération = une
requête). À ajouter si la régénération devient concurrente/rejouable en
parallèle (alors : migration additive + test de migration sur base peuplée,
sur le modèle de `tests/test_b02_migration.py`).
