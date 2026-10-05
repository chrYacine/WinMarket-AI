# Déploiement Render — cible de démonstration

`render.yaml` décrit une cible de **démonstration** : une instance FastAPI (Uvicorn, 1 worker), une base
PostgreSQL Render + `pgvector` dans la même région, un disque persistant privé. Branche `dev`, déploiement
manuel uniquement. Ce guide ne constate aucun déploiement : il décrit la procédure et les vérifications.

| Ressource | Blueprint | Remarque |
|---|---|---|
| Service web `winmarket-ai-yacine` | runtime `python` natif, plan `standard`, 1 instance, région `frankfurt` | payant — coût à valider sur le récapitulatif Render |
| Disque `winmarket-ai-yacine-data` | 2 Go monté sur `/var/data` | payant, proportionnel à la taille |
| Base `winmarket-ai-yacine-db` | PostgreSQL 16, plan `free`, région `frankfurt` | **temporaire : une base gratuite expire** — relever la date affichée par Render dès la création |
| Python | `PYTHON_VERSION=3.12.10` | version sur laquelle le verrou de dépendances est qualifié |

Région et version majeure de la base sont **immuables** après création. Une base gratuite ne convient pas à des
données à conserver : pas de sauvegarde, expiration. Aucun compte ni document ancien n'est importé.

## 0. Gardes d'environnement (rappel)

`APP_ENV=production` est le commutateur unique qui fait accepter une cible réelle à
`src/core/environment_guard.py::validate_environment` (config au démarrage),
`src/core/db_target.py::resolve_database_url` (Alembic) et `src/web/database/session.py::get_engine`
(connexion applicative) : hôte PostgreSQL non-loopback, rôle et base explicites, `BASE_URL` HTTPS public,
`SESSION_SECRET` d'au moins 20 caractères. En production, les variables Render ne sont jamais écrasées par
un fichier `.env`. Voir `tests/test_lot58_deployment_environment.py`.

**Ces contrôles s'exécutent dès l'import de `src.core.config`, donc dès le pre-deploy** : `DATABASE_URL`,
`BASE_URL` et `SESSION_SECRET` doivent tous être renseignés avant le premier déploiement.

## 1. Base de données

- `DATABASE_URL` est fournie au service par `fromDatabase` (propriété `connectionString`) — jamais recopiée
  à la main, jamais affichée. L'application choisit elle-même le pilote `pg8000`.
- `pgvector` : la migration `0015` exécute `CREATE EXTENSION vector` sur PostgreSQL, pendant le pre-deploy,
  avec le rôle fourni par Render. À confirmer au premier déploiement (§6).

## 2. Disque persistant et pre-deploy

- Le disque n'est monté **que pendant l'exécution du service**, jamais pendant `buildCommand` ni
  `preDeployCommand` (documentation Render).
- Or l'import de `src.core.config` crée `DATA_DIR`, `OUTPUT_DIR` et `LOGS_DIR`. Le `preDeployCommand`
  redirige donc **ces trois variables, pour la seule commande de migration**, vers `/tmp/wm-predeploy/...`,
  jetable. Les migrations n'ont besoin que de la base. Les autres chemins (`LOCAL_STORAGE_PATH`,
  `EMBEDDING_CACHE_DIR`) sont seulement validés à l'import, jamais créés.
- La commande de démarrage n'a aucune surcharge : le service en cours d'exécution utilise les chemins
  `/var/data/...` de `render.yaml`, sur le disque persistant.
- Un disque impose une instance unique et empêche les déploiements sans coupure : Render arrête l'instance
  existante avant de démarrer la nouvelle (quelques secondes d'indisponibilité).

## 3. Commandes (syntaxe exacte : `render.yaml`)

| Étape | Commande | Disque monté ? |
|---|---|---|
| Build | `pip install -r requirements.lock.txt` | non |
| Pre-Deploy | `DATA_DIR=/tmp/... OUTPUT_DIR=/tmp/... LOGS_DIR=/tmp/... python -m alembic upgrade head` | non |
| Start | `python scripts/prepare_model.py --cache "$EMBEDDING_CACHE_DIR" && python -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --workers 1` | oui |

Health check Render : `/healthz` (vivacité seule). `/readyz` vérifie en plus la base (`SELECT 1`).

`--workers 1` est **obligatoire** : `main.py` réconcilie les jobs interrompus et démarre le balayage
périodique (`expiry_sweeper`) à chaque démarrage de processus.

`scripts/prepare_model.py` vérifie le modèle par hash (`src/rag/model_artifact.json`). Le premier démarrage le
télécharge sur le disque persistant (démarrage plus long) ; les suivants réutilisent ce cache.

## 4. Variables d'environnement

Valeurs fixes : voir `render.yaml`. Deux valeurs sont demandées par le Dashboard à la création
(`sync: false`) :

- **`SESSION_SECRET`** : générée localement par l'opérateur, par exemple
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`, collée une seule fois dans le champ
  Render. Jamais dans Git, un chat, un ticket ou une capture.
- **`BASE_URL`** : origine publique HTTPS, sans chemin ni `/` final. Elle sert aux liens de
  réinitialisation de mot de passe et doit être **exactement** l'URL publique du service.
  1. **À la création**, saisir l'adresse **prévue** `https://winmarket-ai-yacine.onrender.com`. C'est une
     hypothèse : Render peut attribuer un autre sous-domaine si ce nom est déjà pris ailleurs.
  2. **Après création**, relever l'URL réellement affichée par Render pour le service et la comparer à la
     valeur saisie.
  3. Si elles diffèrent, corriger `BASE_URL` dans l'onglet Environment, puis redéployer manuellement.
  4. **Aucune utilisation fonctionnelle ni activation de compte avant cette comparaison.**

Fournisseurs facultatifs : `LLM_ENABLED=false` et `PAPPERS_ENABLED=false` au premier démarrage. Leurs clés
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MISTRAL_API_KEY`, `PAPPERS_API_TOKEN`) ne sont volontairement pas
déclarées dans `render.yaml`, pour que le Blueprint ne les demande pas. Pour les activer plus tard, les ajouter
dans l'onglet Environment du service, passer le drapeau correspondant à `true`, puis redéployer. Cette
activation est une décision distincte, et le premier appel réel doit être autorisé explicitement.

## 5. Création depuis le Dashboard

1. Dashboard Render → **New** → **Blueprint**.
2. Connecter le dépôt `chrYacine/WinMarket-AI`, branche **`dev`**, fichier `render.yaml`.
3. Renseigner `SESSION_SECRET` et `BASE_URL` (§4).
4. Vérifier le récapitulatif : service `standard` à Frankfurt, disque de 2 Go, base `free` PostgreSQL 16 à
   Frankfurt, et **le coût affiché**. Confirmer seulement si ce récapitulatif est validé.
5. Relever la **date d'expiration** de la base gratuite.

## 6. Vérifications au premier déploiement

1. Build : installation du verrou sous Python 3.12.10.
2. Pre-deploy : `alembic upgrade head` jusqu'à la dernière révision, sans erreur d'écriture.
3. Extension `vector` présente dans la base.
4. Démarrage : modèle vérifié, Uvicorn à l'écoute, `/healthz` et `/readyz` (`database: ok`) répondent.
5. `BASE_URL` égale à l'URL réellement attribuée (§4).
6. Une inscription passe en attente, puis une activation manuelle (§7) la débloque.
7. Un document synthétique s'indexe, se télécharge, et reste présent après un redémarrage du service puis
   après un redéploiement manuel.
8. Recherche RAG en mode `hybrid`/`hybrid_partial` réel, avec `RAG_HYBRID_MODE_ENABLED=true`.

## 7. Opérateur (activation manuelle, sans paiement)

Depuis le Shell Render du service déployé, où les variables du service sont déjà celles du processus
(aucun `--env-file`) :
```
python scripts/operator_access.py --credential-file /var/data/operator.token bootstrap --actor "<nom>"
python scripts/operator_access.py --credential-file /var/data/operator.token list
python scripts/operator_access.py --credential-file /var/data/operator.token activate --user-id <uuid> --organization-id <uuid> --expires-at <ISO_UTC> --max-analyses <n> --reason "Accès manuel autorisé"
python scripts/operator_access.py --credential-file /var/data/operator.token revoke --user-id <uuid> --organization-id <uuid> --reason "Fin accès manuel"
```
Le fichier d'identité opérateur reste sur le disque privé, jamais affiché ni copié hors du service.

## 8. Retour arrière

L'environnement Render se déploie depuis `dev`, jamais depuis `main`. `main` ne reçoit une promotion que
par PR explicite après recette. Supprimer une ressource Render (service, disque, base) efface ses données :
action manuelle, jamais automatique.

## 9. Décisions restant hors de ce guide

- Plan durable de la base avant toute donnée à conserver (la base gratuite expire).
- Activation ou non des fournisseurs LLM et Pappers.
- Domaine personnalisé ou sous-domaine `onrender.com`.
