# B12-T1 — File d'analyse bornée + état durable (`analysis_jobs`)

Défaut corrigé : `jobs.start_analysis()` lançait un `threading.Thread(...).start()`
neuf et non borné par analyse soumise — aucune limite de concurrence, aucune
file d'attente, et aucune trace durable qu'un job avait seulement été ACCEPTÉ
tant que `_run_analysis` n'avait pas atteint le calcul du score (le premier
appel à `_save_analysis_snapshot`). Un job merely-queued/running n'avait donc
aucune ligne en base — impossible de distinguer "toujours en cours" de "process
mort" après un redémarrage.

Ce ticket ajoute une file bornée (`queue.Queue` + pool fixe de threads,
stdlib uniquement, aucun broker) et une table SQL miroir durable de l'état de
la file — **distincte** de `analyses` (qui exige un `result_data` NON NULL et
représente une analyse **terminée**, jamais un job simplement en attente).

Fichiers de ce lot : `src/web/database/models.py` (`AnalysisJob`),
`migrations/versions/0007_b12t1_analysis_jobs.py`,
`src/web/database/repositories/analysis_jobs.py`, `src/web/job_executor.py`,
`src/web/jobs.py` (modifié : `start_analysis`, `get_job`). Tests :
`tests/test_b12_t1_job_executor.py`.

## 1. Table `analysis_jobs`

| Colonne | Type | Rôle |
|---|---|---|
| `id` | `String(50)` PK | Réutilise tel quel `Job.id` (12 hex, `jobs.create_job`) — pas un UUID synthétique. |
| `user_id` | UUID, NOT NULL, FK `users.id` ON DELETE CASCADE | Scope privé, comme `analyses.user_id`. |
| `organization_id` | UUID, NOT NULL, FK `organizations.id` ON DELETE RESTRICT | Scope privé, comme `analyses.organization_id`. |
| `status` | `String(20)`, `CheckConstraint IN ('queued','running','done','error','interrupted')` | Machine à états durable. |
| `source_label` | `String(255)`, nullable | Affichage seulement — **jamais** le texte brut de l'AO (aucune relecture LLM automatique n'est un objectif de ce ticket). |
| `worker_instance_id` | `String(64)`, nullable | Identité de l'exécutant au moment du claim — observabilité, **pas** la base de la décision d'abandon (voir §3). |
| `error_code` | `String(50)`, nullable | Erreur **de niveau file** (saturation enregistrée, `job_interrupted`) — distinct de la riche taxonomie par-analyse de `jobs.py`/`analyses`. |
| `analysis_id` | UUID, nullable, FK `analyses.id` ON DELETE SET NULL | Renseigné si/quand le job atteint un résultat calculé. |
| `created_at`, `claimed_at`, `last_heartbeat_at`, `finished_at` | `DateTime(timezone=True)` | Horodatages du cycle de vie. |

Migration `0007` : purement additive (`op.create_table`), testée sur SQLite
vide **et** peuplé (0001→0006 appliquées puis lignes réelles insérées avant
`0007`) — voir `tests/test_b12_t1_job_executor.py::test_migration_0007_applies_on_fresh_and_populated_sqlite`.

## 2. Détection d'abandon : heartbeat, pas seulement `claimed_at`

`src/web/job_executor.py` démarre un thread de heartbeat par job **réclamé**
(`try_claim` réussi), qui rafraîchit `last_heartbeat_at` toutes les
`JOB_HEARTBEAT_INTERVAL_SECONDS` (défaut 10s) tant que `_run_analysis` tourne.

Pourquoi un heartbeat plutôt qu'un simple `claimed_at + timeout` fixe : un
exécutant **vivant** rafraîchit son propre horodatage quel que soit le
processus qui l'exécute — ce qui reste correct même dans un déploiement
multi-instance (non exercé au-delà d'un seul test réel, voir §5), alors que
`claimed_at` seul ne peut jamais distinguer "toujours en cours, ça prend du
temps" de "l'exécutant est mort" sans deviner une durée maximale de job.

Au démarrage, `job_executor.reconcile_on_startup(staleness_seconds=None)` :
- marque **tout** job `queued` comme `interrupted` (inconditionnel — la file
  `queue.Queue` en mémoire qui l'aurait contenu ne survit à aucun redémarrage,
  donc une ligne encore `queued` à un démarrage frais ne peut structurellement
  plus être en attente nulle part) ;
- marque tout job `running` dont `last_heartbeat_at` est `NULL` ou plus vieux
  que `JOB_HEARTBEAT_STALE_SECONDS` (défaut 60s) comme `interrupted`.

Un job `interrupted` **n'est jamais relancé automatiquement** — il reste dans
cet état terminal ; seule une nouvelle soumission explicite via
`/api/analyze` (un nouveau job) reprend l'analyse.

## 3. Ce qui est prouvé, et ce qui ne l'est PAS

- **Prouvé** (`tests/test_b12_t1_job_executor.py::test_real_process_restart_is_reconciled_as_interrupted`) :
  un **vrai second process Python** (`subprocess`) crée et réclame un job sur
  un fichier SQLite dédié puis se termine sans jamais finaliser ; ce process
  de test (le "process qui redémarre") relance ensuite
  `reconcile_on_startup()` sur ce même fichier et confirme le passage à
  `interrupted`, visible via `jobs.get_job()` sans jamais construire de
  client LLM (fake `ClaudeClient` qui lève si instancié, même idiome que le
  test de régénération B19-T2).
- **PAS prouvé, et non revendiqué** : sécurité multi-instance réelle (deux
  processus **simultanément vivants** sur la même base), comportement
  spécifique PostgreSQL (seul SQLite est exercé ici), et exactly-once côté
  fournisseur LLM. Le mécanisme de heartbeat est *conçu* pour rester correct
  dans ces cas (un exécutant vivant garde son heartbeat frais, donc
  `reconcile_on_startup` le laisserait tranquille), mais ceci n'est
  qu'un raisonnement, pas un test avec un second process **toujours vivant**
  pendant la réconciliation.
- Le double-claim concurrent (`try_claim`, `UPDATE ... WHERE status='queued'`)
  est en revanche prouvé au niveau DB réel (deux threads, une vraie
  transaction chacun) — c'est ce mécanisme, pas le heartbeat, qui garantit
  qu'un job n'est jamais traité deux fois par deux exécutants qui le
  réclament au même instant, y compris entre deux instances distinctes
  pointant vers la même base (le raisonnement est le même que pour deux
  threads : l'UPDATE conditionnel est sérialisé par la base elle-même).

## 4. Nouvelles constantes (`src/core/config.py`, additives)

```python
JOB_EXECUTOR_MAX_CONCURRENCY = int(os.getenv("JOB_EXECUTOR_MAX_CONCURRENCY", "4"))
JOB_QUEUE_MAX_DEPTH = int(os.getenv("JOB_QUEUE_MAX_DEPTH", "20"))
JOB_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("JOB_HEARTBEAT_INTERVAL_SECONDS", "10"))
JOB_HEARTBEAT_STALE_SECONDS = int(os.getenv("JOB_HEARTBEAT_STALE_SECONDS", "60"))
```

## 5. Intégration à appliquer par le coordinateur

### 5.1 `src/web/routes_api.py` — `/api/analyze`

**Aucun changement n'est nécessaire à l'appel `jobs.start_analysis(job, content)`
lui-même** : sa signature est inchangée et il délègue maintenant, en interne,
à `job_executor.submit()`. Le seul ajout nécessaire est de traduire la
saturation en 429 explicite (aujourd'hui elle remonterait comme une erreur
serveur non gérée) :

```python
from src.web import job_executor  # nouvel import

@router.post("/analyze")
async def api_analyze(...):
    ...
    job = jobs.create_job(source_label=source_label, user_id=ctx.user.id, organization_id=ctx.organization_id)
    try:
        jobs.start_analysis(job, content)
    except job_executor.JobQueueSaturatedError:
        raise HTTPException(429, {
            "error_code": "JOB_QUEUE_SATURATED",
            "message": "Le service est actuellement saturé. Réessayez dans quelques instants.",
        })
    return {"job_id": job.id}
```

Rien d'autre ne change dans cette route : la ligne durable `queued` est déjà
écrite, de façon synchrone, à l'intérieur de `jobs.start_analysis` avant son
retour — aucune ligne fantôme n'est créée pour une soumission refusée par 429
(voir `job_executor.submit`'s docstring pour l'ordre garantissant cela).

### 5.2 `main.py` — réconciliation au démarrage

**Intégré réellement via un `lifespan` (et non `@app.on_event("startup")`,
qui apparaissait dans une version précédente de ce document)** :
`@app.on_event` est déprécié dans la version de FastAPI installée
(0.141.1) — fonctionne encore (juste un `DeprecationWarning`, sans effet
sur les tests puisque `pytest.ini` a `--disable-warnings`), mais le
coordinateur a préféré le mécanisme moderne recommandé plutôt
qu'introduire du code déjà déprécié. État réel dans `main.py` :

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def _lifespan(app: FastAPI):
    from src.web import job_executor
    job_executor.reconcile_on_startup()
    yield

app = FastAPI(title="WinMarket AI", docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)
```

Comportement identique à la version `on_event` : exécuté une fois avant que
l'application ne commence à accepter des requêtes, no-op silencieux si
aucune base n'est configurée.

Aucun autre fichier n'a besoin d'être modifié : `jobs.get_job()` consulte déjà
`analysis_jobs` (voir §6) une fois cette réconciliation faite, donc
`/api/analyze/{job_id}/status` reflète `interrupted` sans changement de route.

## 6. Lecture (`jobs.get_job`) — nouvelle branche

Chaîne de lecture, dans l'ordre (inchangé pour les 2 premiers, `analysis_jobs`
est insérée AVANT le JSON legacy) :

1. `_JOBS` (mémoire du process).
2. ligne SQL `analyses` (`_load_from_database`) — résultat calculé.
3. **nouveau** : ligne SQL `analysis_jobs` (`_load_from_job_queue_table`) — un
   job qui n'a JAMAIS atteint de résultat calculé (donc rien en (2)) mais a
   une trace de file : `interrupted` → `Job.status="error"`,
   `error_code="job_interrupted"` (mis en cache, terminal) ; `error` →
   idem avec le code de file stocké (mis en cache, terminal) ; `queued`/
   `running` → `Job.status="running"` **jamais mis en cache** (pour que la
   prochaine lecture revoie l'état à jour au lieu de figer un instantané
   d'avant réconciliation) ; `done` sans ligne `analyses` correspondante →
   `None` (c'est exactement le cas `PERSISTENCE_FAILED_ERROR_CODE`
   pré-existant de B11-T1 : irrécupérable une fois `_JOBS` perdu, ne jamais
   fabriquer de substitut).
4. JSON legacy (`_load_persisted`) — dernier recours, jobs sans `user_id`.

`job.organization_id` porté par la branche (3) vient de la ligne elle-même,
jamais fabriqué — la vérification fraîche existante côté route
(`require_active_membership`, appelée après `jobs.get_job()` dans
`api_analyze_status`) continue donc de refuser un membre dont l'adhésion a été
révoquée, même pour un job servi depuis cette nouvelle table (voir
`tests/test_b12_t1_job_executor.py::test_revoked_membership_still_refused_when_job_is_served_from_the_new_table`).

## 7. `src/web/database/repositories/analysis_jobs.py` — fonctions

| Fonction | Rôle |
|---|---|
| `create_queued` | INSERT initial, appelé par `job_executor.submit` avant toute mise en file. |
| `delete_if_queued` | Rollback de `create_queued` si la file s'avère saturée juste après (voir §8). |
| `try_claim` | `UPDATE ... WHERE status='queued'` conditionnel — garantie DB réelle, pas un verrou applicatif. Fait aussi office de `mark_running` (pas de fonction séparée : un claim non-atomique en deux temps réintroduirait la fenêtre de course que `try_claim` existe pour fermer). |
| `update_heartbeat` | `UPDATE ... WHERE worker_instance_id=? AND status='running'`. |
| `mark_done` / `mark_error` / `mark_interrupted` | Transitions terminales, jamais si déjà `done`/`error`/`interrupted` (même idiome que `jobs._fail`). |
| `reconcile_stale_jobs` | Le scan de démarrage (§2), deux `UPDATE` conditionnels. |
| `get_by_id` | Lecture non filtrée — usage serveur interne uniquement (`jobs.get_job`), même contrat que `analyses_repo.get_by_job_id`. |
| `get_by_id_for_user` | Lecture filtrée par propriétaire. |

## 8. `src/web/job_executor.py` — ordre de soumission (point sensible)

`submit(job, text)` écrit la ligne durable **avant** de mettre l'item dans
`queue.Queue` — jamais l'inverse. Un pool à threads persistants peut déjà
être en train de consommer la file au moment de l'appel ; si l'item était
visible avant que la ligne existe, un worker pourrait le dépiler et appeler
`try_claim` avant l'écriture, ne rien trouver, l'ignorer silencieusement (son
contrat normal en cas de claim raté) — et le job resterait bloqué "running"
en mémoire pour toujours, plus personne ne le reprenant jamais. Ceci a été
une régression réelle rencontrée pendant le développement de ce ticket,
corrigée par cet ordre. Sur saturation (`queue.Full`), la ligne tout juste
écrite est supprimée (`delete_if_queued`) avant de lever
`JobQueueSaturatedError` — aucune ligne fantôme ne survit à un refus.
