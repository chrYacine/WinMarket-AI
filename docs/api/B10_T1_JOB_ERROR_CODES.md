# B10-T1 — Contrat : terminaison contrôlée des jobs d'analyse

Destiné au développeur consommant `GET /api/analyze/{job_id}/status`. **Aucun écran n'est requis.**

## Défauts confirmés

1. `get_pipeline()` et le lancement du thread worker (`start_analysis`) s'exécutaient HORS de toute protection — une exception à l'un ou l'autre laissait le job bloqué à `status="running"` indéfiniment, invisible pour un client qui interroge le statut.
2. L'exception générique finale insérait le texte brut de l'exception directement dans `job.error` (`f"...: {exc}"`), renvoyé tel quel par l'API — tout détail interne porté par l'exception (fragment de texte de document via un message de validation, chemin de fichier...) pouvait fuiter dans la réponse publique.
3. Un échec de génération de document effaçait le résultat de scoring déjà calculé (le job se terminait sans jamais fixer `job.ao`/`job.result`).

## Codes d'erreur (`error_code` dans la réponse de statut)

Les codes existants sont conservés à l'identique : `invalid_rag_evidence` (B18-T1), `historical_score_unavailable` (B18-T2). Nouveaux codes ajoutés par ce ticket :

| `error_code` | Signification | `status` associé |
|---|---|---|
| `worker_launch_failed` | Le thread d'analyse n'a pas pu démarrer (ressources épuisées) — le job n'a jamais commencé à s'exécuter | `error` |
| `extraction_failed` | Bug réel dans `AOExtractor.extract` (distinct des replis LLM déjà gérés par B05-T2, qui ne lèvent jamais) | `error` |
| `rag_failed` | Échec de la recherche/rerank RAG autre qu'une preuve invalide (déjà couverte par `invalid_rag_evidence`) | `error` |
| `scoring_failed` | Échec du calcul de score autre qu'une preuve invalide | `error` |
| `document_generation_failed` | Le score a été calculé avec succès, mais la génération PDF/DOCX a échoué — `job.ao`/`job.result` restent renseignés (voir plus bas), `job.files` est vide, **aucun fichier n'est jamais annoncé comme disponible** | `error` |
| `persistence_failed` | Le score et les documents ont été produits avec succès, mais l'enregistrement JSON durable a échoué — le résultat reste consultable pour la durée de vie du process, mais ne survivrait pas à un redémarrage (B11/B12 restent responsables de la reprise après crash) | `done` |
| `unexpected_error` | Toute autre exception non prévue — le détail réel est uniquement journalisé côté serveur (`logger.exception`), jamais exposé | `error` |

**Aucun message d'erreur public n'inclut plus jamais le texte brut d'une exception.** Chaque message est une chaîne fixe, sûre, en français, invariante d'une occurrence à l'autre du même code.

## Garanties de transition d'état

- Toute exception interceptable pendant l'initialisation du job (y compris la création des dépendances et le lancement effectif du thread) mène désormais à un état terminal observable — plus aucun job ne peut rester bloqué à `"running"` indéfiniment suite à une exception.
- Une transition vers `"error"` passe systématiquement par un point unique (`jobs._fail`) qui refuse de modifier un job déjà terminal (`"done"` ou `"error"`) — une progression tardive ne peut jamais remettre un job terminé à `"running"`, ni un second échec écraser le premier code d'erreur déjà enregistré.
- Un repli LLM normal déjà géré par B05/B06/B18 (LLM désactivé, réponse invalide, exception fournisseur) n'est **jamais** converti en échec fatal par ce ticket — ces chemins ne lèvent pas d'exception et continuent de produire un job `"done"` normal.

## `job.ao` / `job.result` après un échec de rendu (`document_generation_failed`)

Contrairement à avant ce ticket, `job.ao` et `job.result` sont désormais renseignés dès qu'ils sont calculés (immédiatement après l'extraction et après le scoring respectivement), et non plus seulement à la toute fin du job. Un échec de génération de document survenant APRÈS ce point ne les efface jamais. `job.files` reste `{}` dans ce cas — jamais un chemin vers un fichier qui n'a pas été écrit.

L'enregistrement durable (JSON + PostgreSQL) est TOUJOURS tenté même après un échec de rendu, afin que le score déjà calculé ne soit pas perdu si le rendu échoue — voir `persistence_failed` ci-dessus pour le cas où CET enregistrement échoue à son tour.

## Contrôle d'accès (inchangé, revérifié par ce ticket)

`GET /api/analyze/{job_id}/status` et `GET /api/download/{job_id}/{kind}` continuent de traiter un job appartenant à un autre compte exactement comme un job inexistant (`404`) — aucun repli global, aucune fuite d'existence vers un tiers. Ce comportement préexistant est rejoué par les tests de ce ticket, non modifié.

## Persistance et compatibilité

Aucune migration SQL. `Job.error_code` existe déjà depuis B18-T1 ; les nouvelles valeurs s'y ajoutent simplement. Un job historique n'est pas affecté rétroactivement.
