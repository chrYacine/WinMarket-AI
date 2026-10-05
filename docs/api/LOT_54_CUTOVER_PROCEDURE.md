# Lot 54 — Procédure de bascule SQLite → PostgreSQL (corrigée au lot 55)

**Statut : préparée et répétée sur données jetables** (lot 54 : `docs/qa/lot_54_20260926/RAPPORT.md` §3 ;
lot 55 : `docs/qa/lot_55_20260926/RAPPORT.md`). **Aucune étape ci-dessous n'a été exécutée sur la vraie base
de l'utilisateur.** La base réelle reste SQLite tant que l'utilisateur n'a pas donné l'accord explicite
décrit en fin de document (§10).

**Corrections du lot 55 par rapport à la version d'origine (lot 54), toutes qualifiées sur données
jetables** :
- §1 (sauvegarde) : `scripts/backup_restore.py` sauvegarde désormais TOUS les répertoires réellement
  référencés par le code (`DATA_DIR`/`LOCAL_STORAGE_PATH`/`OUTPUT_DIR`, jamais une liste de deux noms
  figée) et vérifie chaque fichier restauré par hash + permissions (`files_manifest.json`) — l'ancienne
  version manquait réellement `data/knowledge/` (voir le défaut reproduit, RAPPORT.md §1). Le backup SQLite
  utilise l'API Online Backup de SQLite (`sqlite3.Connection.backup`) au lieu d'une copie brute de fichier.
- §3/§4 (schéma et transfert) : `alembic` est désormais ciblé explicitement via le résolveur du lot 50 ter
  (`-x db_url=...`), jamais une variable d'environnement nue. Le script de transfert refuse maintenant une
  divergence de VALEUR sur une ligne déjà présente en mode `resume` (une clé primaire existante ne suffisait
  pas à prouver une ligne identique), vérifie que source et cible sont réellement à la même révision Alembic
  de tête avant tout transfert, réattribue automatiquement 'interrupted' à tout job resté `queued`/`running`
  copié sur la cible, et accepte les URLs par variable d'environnement pour éviter un mot de passe dans
  l'historique du shell.
- §2 (arrêt des écritures) et §5 (fenêtre de validation) : description honnête, corrigée — voir ces sections.

## 0. Prérequis (à obtenir/préparer AVANT toute étape réelle)

- **Un serveur PostgreSQL persistant et durable, avec l'extension `pgvector` installée.** Jamais le
  `pgserver` de test (embarqué, jetable, orchestration de test uniquement) utilisé pour qualifier ce lot —
  ses BINAIRES (PostgreSQL 16.2 + pgvector précompilé pour Windows) peuvent servir de source vérifiable pour
  une installation durable fraîchement initialisée, mais son orchestration de test elle-même n'est jamais la
  cible de production. Aucun PostgreSQL utilisateur n'existe aujourd'hui sur ce poste (vérifié : aucun
  service, aucun binaire sur PATH, port 5432 libre) — voir `docs/qa/lot_55_20260926/RAPPORT.md` pour le
  choix concret d'installation locale préparé ce lot.
- Comptes dédiés sur ce serveur : un compte applicatif (lecture/écriture sur la base cible uniquement) et,
  si nécessaire, un compte de déploiement distinct pour les extensions/migrations — jamais les identifiants
  d'administration partagés.
- **Secrets : jamais dans une commande, jamais dans ce document, jamais dans un rapport.** Toute URL
  contenant un mot de passe est fournie via une variable d'environnement positionnée juste avant l'appel
  (jamais un argument `--target-db-url=...` littéral, qui reste dans l'historique du shell et dans la liste
  des process) : `$env:WM_MIGRATE_TARGET_DB_URL = '...'` (PowerShell, portée du processus courant
  uniquement) puis `python scripts/migrate_sqlite_to_postgresql.py` sans argument d'URL. Ni ce document ni
  un rapport de recette n'affiche jamais un mot de passe — seul `render_as_string(hide_password=True)` (déjà
  utilisé partout dans le script et dans `alembic`) apparaît dans les sorties conservées.
- Une décision, prise AVANT la bascule, sur l'arrêt réel des écritures (voir §2 — aucun mode maintenance
  n'existe dans ce dépôt).

## 1. Sauvegarde avant toute opération

1. `python scripts/backup_restore.py backup <dossier_horodaté>` — sauvegarde la base SQLite réelle (API
   Online Backup SQLite, cohérente quel que soit le mode journal) **et** l'intégralité des répertoires de
   fichiers réellement référencés par le code (`DATA_DIR`/`LOCAL_STORAGE_PATH`/`OUTPUT_DIR` — voir
   `_collect_file_roots` : plus une liste de deux noms figée, couvre `outputs/`, `historique/`,
   `knowledge/`, et tout futur sous-répertoire sans modification du script). Un `files_manifest.json`
   (sha256 + permissions par fichier) accompagne la sauvegarde.
2. **Vérifier la sauvegarde en la restaurant** dans un emplacement jetable et distinct :
   `python scripts/backup_restore.py restore <dossier_horodaté> --target-db-url sqlite:///<chemin_jetable> --target-data-dir <dossier_jetable>`
   (le script refuse structurellement de cibler la vraie base/le vrai dossier). La restauration recalcule et
   compare le hash + les permissions de CHAQUE fichier contre `files_manifest.json` et refuse (code de
   sortie non nul) toute divergence — une sauvegarde jamais restaurée avec succès, hash compris, n'est pas
   une sauvegarde vérifiée.
3. Conserver cette sauvegarde (et sa restauration de vérification) dans un emplacement **distinct** du
   serveur applicatif, avec une politique de rétention explicite (`scripts/backup_restore.py purge`, jamais
   automatique par défaut).

## 2. Arrêt maîtrisé des écritures — ce qui existe réellement, sans le sur-promettre

**Aucune route de ce dépôt n'implémente un mode maintenance ni une coupure d'admission des nouveaux jobs
sans arrêter le processus applicatif entier** (vérifié : `src/web/job_executor.py` n'expose ni pause ni
drain — les workers sont des threads démons du même processus qu'`uvicorn`, jamais un processus séparé
qu'on pourrait arrêter isolément). L'honnêteté de cette section prime sur toute promesse de fenêtre "lecture
seule" qui n'existe pas dans le code :

1. **Vérifier (jamais supposer) qu'aucun job n'est `queued`/`running`** (`analysis_jobs`) avant de couper —
   utile comme signal, mais **pas une garantie** : un utilisateur peut soumettre une nouvelle analyse entre
   cette vérification et l'arrêt réel du processus, tant que celui-ci répond encore aux requêtes.
2. **Arrêter le processus applicatif réel** (le service `uvicorn`, ou l'équivalent en production) — c'est,
   en l'absence de tout mécanisme de drainage partiel, la SEULE garantie honnête contre une écriture
   concurrente pendant la bascule.
3. Un job qui était `queued`/`running` au moment de l'arrêt reste tel quel dans la base SQLite arrêtée —
   traité explicitement, jamais laissé "en vol" silencieusement :
   - Si la base SQLite reste ensuite active un moment (ex. pour la sauvegarde finale) avant tout redémarrage
     de l'application dessus, son propre `reconcile_on_startup` s'en chargera au prochain démarrage sur
     CETTE base — mais après une bascule, l'application ne redémarre jamais sur l'ancienne base SQLite.
   - **`scripts/migrate_sqlite_to_postgresql.py` réattribue donc lui-même ce job `queued`/`running` copié à
     `interrupted` sur la CIBLE, automatiquement, en fin de transfert** (lot 55, réutilise la même
     réconciliation que `scripts/backup_restore.py`'s propre chemin de restauration — jamais un job laissé
     éternellement réclamable sur une cible dont le processus d'origine ne le reprendra jamais).

## 3. Sauvegarde finale « à froid » et migration du schéma cible

1. Avec les écritures arrêtées (processus applicatif coupé, §2), reprendre une sauvegarde finale (étape 1)
   — c'est CELLE-CI qui sert de filet de sécurité pour le retour arrière (§9), pas la première.
2. Sur le PostgreSQL cible (vide, schéma non créé) : cibler `alembic` **explicitement**, jamais via une
   variable d'environnement nue susceptible d'être écrasée par le chargement de `.env`
   (`src.core.config` charge `.env` avec `override=True` — voir `src/core/db_target.py`, résolveur du lot
   50 ter, déjà celui qu'utilisent tous les tests/migrations de ce dépôt) :
   ```
   alembic -x db_url=postgresql+pg8000://<utilisateur>:<mot_de_passe>@<hôte>/<base> upgrade head
   ```
   Ici encore, ne jamais taper le mot de passe en clair dans une commande qui reste dans l'historique — le
   fournir via un mécanisme de secrets local (gestionnaire de mots de passe, fichier non versionné lu par un
   petit script wrapper), jamais collé directement.
   **Jamais** `WM_DB_TEST_MODE=1` pour cette exécution réelle (la garde `src/core/db_target.py` refuserait
   alors la cible réelle, précisément l'inverse de ce que cette étape doit faire).

## 4. Transfert des données

```
$env:WM_MIGRATE_SOURCE_DB_URL = 'sqlite:///<chemin_sqlite_réel_arrêté>'
$env:WM_MIGRATE_TARGET_DB_URL = 'postgresql+pg8000://<utilisateur>:<mot_de_passe>@<hôte>/<base>'
python scripts/migrate_sqlite_to_postgresql.py --mode empty-target
```

- Les URLs passent par variable d'environnement (lot 55), jamais par `--source-db-url`/`--target-db-url`
  littéraux pour une exécution réelle — évite qu'un mot de passe atterrisse dans l'historique du shell ou la
  liste des process. Le résumé imprimé n'affiche jamais le mot de passe
  (`render_as_string(hide_password=True)`, quelle que soit la source de l'URL).
- **Ne jamais** passer `WM_DB_TEST_MODE=1` pour cette exécution réelle (même raison qu'à l'étape 3).
- Avant tout transfert, le script vérifie que source ET cible sont réellement à la révision Alembic de tête
  attendue (si l'une des deux porte une trace Alembic réelle — ce qui est le cas normal d'une vraie base
  applicative) — une table présente ne suffit plus à prouver un schéma compatible ; une révision différente
  est un refus net, avant toute ligne copiée.
- Le script est **idempotent** en cas d'échec partiel : relancer avec `--mode resume` reprend exactement là
  où l'échec s'est arrêté. Une clé primaire déjà présente sur la cible n'est plus, à elle seule, une preuve
  que la ligne est identique : chaque colonne (hors transformations dérivées explicitement documentées —
  `embedding_status`/`embedding_indexed_at`, la colonne de clé étrangère différée) est comparée à la valeur
  source ; une divergence réelle est **refusée** (comptée en erreur), jamais ignorée ni écrasée
  silencieusement.
- Vérifier le rapport imprimé (comptes copiés/déjà présentes/divergences/erreurs par table) — **zéro erreur
  attendu** avant de continuer. Une erreur non nulle (code de sortie non nul) **bloque** la suite de la
  procédure ; la cible reste interdite à l'application tant qu'elle n'est pas résolue et le transfert
  rejoué proprement.

## 5. Contrôles réels avant toute activation

1. **Effectifs par table** : comparer `SELECT COUNT(*)` source vs cible pour CHAQUE table (le script
   `scripts/migrate_sqlite_to_postgresql.py --dry-run` sur la source donne les comptes attendus).
2. **Identifiants et relations** : vérifier qu'un échantillon d'`Analysis.job_id`/`parent_job_id`/
   `origin_job_id`, d'`AoDossier`/`AoDossierPiece` et de `AnalysisComplement.source_json` se retrouvent
   inchangés sur la cible (voir `tests/test_lot54_migration_rehearsal.py` pour la méthode exacte, rejouée
   sur les vraies tables).
3. **Fichiers privés** : les références en base (`storage_key`, `text_storage_key`, `storage_path`) doivent
   rester résolvables — si le nouveau serveur applicatif partage le même `LOCAL_STORAGE_PATH`/`OUTPUT_DIR`
   (même disque), rien à faire ; sinon, copier ces répertoires **en plus** de la base (jamais déplacés/
   supprimés côté source — `scripts/backup_restore.py`'s nouveau contrôle par hash/permissions, §1, sert
   aussi de preuve ici) et vérifier son propre contrôle référentiel (`_check_referential_integrity`).
4. **Mots de passe/hash** : jamais lus ni affichés par ce script ni par cette procédure — vérifiés
   uniquement par un test de connexion réel après bascule (étape 7).
5. **Toute opération de validation qui écrit réellement des données** (une connexion applicative crée une
   session ; lancer une analyse de bout en bout crée un job, une analyse, des livrables) **n'est PAS gratuite
   sur la cible** — ce ne sont pas de simples lectures. Deux options honnêtes, à choisir explicitement, jamais
   implicitement :
   - Utiliser un compte et des données strictement identifiés comme "validation de bascule" (jamais un
     compte réel), en acceptant de les conserver ou de les purger après coup par une action délibérée
     (jamais une suppression automatique devinée).
   - Ou accepter que ces écritures de validation restent sur la cible en production — un choix produit
     explicite, pas une conséquence non prévue de la procédure.
   Cette fenêtre de validation doit rester **isolée du trafic utilisateur réel** (l'application n'est pas
   encore ouverte au public — voir §7-§8) : ce n'est qu'une fois cette isolation confirmée que les contrôles
   ci-dessus ont une valeur probante non polluée par un usage concurrent.

## 6. Indexation du RAG hybride (optionnelle, décision séparée)

Une fois la cible validée et **avant** de basculer le trafic dessus :
1. Activer `RAG_HYBRID_MODE_ENABLED=true` dans la configuration de la cible SEULEMENT.
2. Réindexer chaque version active de document privé (`POST .../reindex` par document, ou un script dédié
   parcourant `knowledge_repo.list_active_documents` + `hybrid_index.index_version`) — jamais un index
   copié depuis la source (SQLite n'en a jamais eu). `embedding_status` de chaque version migrée est déjà
   remis à `pending`/`not_applicable` par le script de transfert (jamais laissé à `ready` sans vecteur
   réel derrière).
3. **Le modèle d'embeddings (`fastembed`, révision exacte épinglée) doit être pré-mis en cache durablement
   AVANT le jour de la bascule** — un téléchargement réseau imprévu au premier appel réel n'est jamais
   acceptable un jour de présentation (voir `docs/qa/lot_55_20260926/RAPPORT.md` pour l'emplacement de cache
   préparé ce lot).
4. Vérifier une recherche réelle (mode effectivement `hybrid`/`hybrid_partial`, pas seulement annoncé) sur un
   cas connu avant d'exposer la cible aux utilisateurs.

**Ceci reste une décision produit séparée** : la bascule de base de données n'oblige pas à activer le RAG
hybride le même jour — SQLite reste lexical, PostgreSQL peut rester lexical aussi tant que ce choix n'est
pas fait explicitement.

## 7. Vérifications de santé, authentification, parcours

1. Démarrer l'application pointée sur la nouvelle base (jamais en mode test), dans un environnement encore
   **isolé du trafic utilisateur réel** (voir §5 point 5).
2. Un compte réel existant peut se connecter (mot de passe inchangé — hash jamais recalculé).
3. Un historique existant s'affiche, une ancienne analyse/révision reste consultable et son PDF/DOCX
   téléchargeable, avec le même contenu qu'avant bascule (SHA-256 comparé).
4. Une nouvelle analyse peut être lancée de bout en bout sur la nouvelle base — voir §5 point 5 pour le
   traitement de cette écriture de validation.

## 8. Activation de configuration

Seulement après validation complète de l'étape 7 : basculer `DATABASE_URL` (et `LOCAL_STORAGE_PATH`/
`OUTPUT_DIR` si déplacés) de manière définitive, redémarrer le service applicatif réel. **Ne jamais
supprimer l'ancienne base SQLite à cette étape** — elle reste le filet de sécurité jusqu'à ce que la
nouvelle cible ait tourné en production sans incident pendant une durée à définir avec l'utilisateur.

## 9. Retour arrière — ce qu'il implique réellement

**Remettre simplement l'ancienne base SQLite en place PERD toute écriture faite sur PostgreSQL après la
bascule.** Deux options honnêtes, à choisir AVANT la bascule, jamais après :

- **Fenêtre de validation sans écriture utilisateur** : après la bascule (étape 8), une courte période
  (à définir) où l'application reste accessible en lecture seule, ou avec un trafic limité/annoncé, le temps
  de confirmer qu'aucun incident n'apparaît. Le retour arrière pendant cette fenêtre est un simple retour à
  la sauvegarde SQLite de l'étape 3 (aucune perte, puisque rien de nouveau n'a été écrit sur PostgreSQL).
- **Reprise des écritures avant tout retour arrière possible** : si des écritures réelles ont déjà eu lieu
  sur PostgreSQL avant qu'un incident soit détecté, un retour arrière vers SQLite exige de **rejouer** ces
  écritures (même mécanisme de transfert, sens inverse, jamais construit dans ce lot — hors périmètre
  explicite) ou de les accepter comme perdues, ce qui doit être un choix explicite de l'utilisateur, jamais
  une promesse implicite de « sans perte ». **Inventorier les nouvelles écritures PostgreSQL avant tout
  retour arrière** — jamais un abandon de données nouvelles sans décision explicite.

Aucune promesse « bascule réversible sans perte » n'est faite au-delà de la fenêtre de validation sans
écriture — le formuler autrement serait une garantie non démontrée.

## 10. Ce qui reste à obtenir de l'utilisateur avant toute exécution réelle

1. La cible PostgreSQL durable locale préparée (voir `docs/qa/lot_55_20260926/RAPPORT.md`) : confirmation
   qu'elle démarre/s'arrête proprement, persiste après redémarrage du service, écoute en loopback
   uniquement, avec `pgvector` installé si le RAG hybride est prévu.
2. **L'accord explicite pour** : (a) lire/sauvegarder la vraie base et les vrais fichiers privés, (b)
   interrompre les écritures pendant la fenêtre de bascule (arrêt réel du processus applicatif, §2 — aucun
   mode maintenance partiel n'existe), (c) transférer vers la cible identifiée, (d) modifier la
   configuration réelle de l'application, (e) activer ou non le RAG hybride. **Ce prompt seul ne donne pas
   cet accord** ; aucun rapport remis, aucun choix d'hébergement, ni la préparation de cette procédure
   elle-même ne le donne automatiquement — il doit être donné explicitement, séparément.
3. La durée acceptable de la fenêtre de maintenance et de la fenêtre de validation post-bascule, et le choix
   explicite (§5 point 5) sur le sort des écritures de validation.

**Rien dans ce lot n'attend cet accord pour être préparé** : les scripts, leur qualification sur données
jetables et cette procédure sont déjà complets et corrigés. Seule l'exécution sur l'environnement réel reste
bloquée, dans l'attente de l'accord ci-dessus.
