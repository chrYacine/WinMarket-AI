# Reprise du projet — lot 56

Cette version contient le code et des fixtures synthetiques. Elle ne contient aucun compte reel, ancienne analyse, document prive, SQLite, sauvegarde, secret ou environnement virtuel. La version conservee ne doit jamais etre migree depuis cette copie. Les anciens PDF/DOCX absents du lot 55 bis ne sont pas des dependances de cette livraison.

## Prerequis et perimetre qualifie

Windows, Python 3.12 (qualification locale : 3.12.10), Git pour recuperer le depot, acces aux distributions PyPI et aux poids publics pour la preparation initiale. Le lock applicatif contient 113 distributions. PostgreSQL 16.2 et pgvector 0.6.2 sont fournis par `pgserver==0.1.4` dans le venv neuf ; aucune installation de service ni droit administrateur n'est necessaire dans un dossier appartenant a l'utilisateur. Ne desactivez pas une protection Windows si elle refuse une DLL : conservez le message et faites diagnostiquer la politique locale.

Linux est couvert par la configuration CI ; seul un run GitHub effectivement observe constitue sa qualification. Docker Compose est une alternative documentee, pas une preuve locale Docker. La copie de reprise sur cette machine ne prouve pas un essai sur l'ordinateur du second developpeur.

## Installation Windows depuis dev

Depuis PowerShell, choisissez un dossier neuf hors de toute ancienne version :

```powershell
git clone --branch dev https://github.com/chrYacine/WinMarket-AI.git WinMarket-AI
Set-Location WinMarket-AI
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_lock_consistency.py
$runtime = Join-Path $env:LOCALAPPDATA 'WinMarket-AI-chrYacine-runtime'
.\.venv\Scripts\python.exe scripts/local_env.py init --runtime $runtime --pg-port 5546 --app-port 8056
.\.venv\Scripts\python.exe scripts/local_env.py migrate --runtime $runtime
.\.venv\Scripts\python.exe scripts/prepare_model.py --cache "$runtime\models"
.\.venv\Scripts\python.exe scripts/local_env.py start --runtime $runtime
.\.venv\Scripts\python.exe scripts/local_env.py status --runtime $runtime
Invoke-RestMethod http://127.0.0.1:8056/healthz
Invoke-RestMethod http://127.0.0.1:8056/readyz
```

`init` exige un repertoire inexistant, des ports libres et distincts. Il genere une configuration depuis `.env.example`, deux mots de passe PostgreSQL, un secret de session, un cluster et des stockages neufs. Le role applicatif `wm56_app` n'est ni superutilisateur, ni createur de roles/bases ; `wm56_admin` reste une identite locale de maintenance. Les ACL Windows du runtime sont restreintes a son proprietaire et SYSTEM. `admin.json`, `.env`, `operator.token`, les donnees et les sauvegardes sont confidentiels et exclus de Git.

Le port conserve 5432, le nom `winmarket_app_db`, le port applicatif conserve 8000 et les repertoires proteges sont refuses avant les operations. Les chemins resolus sont controles, y compris les liens. Aucun repli vers la configuration d'un autre depot. Ne positionnez jamais `WM_DB_TEST_MODE` pour le lancement normal.

`start` ne lance aucune migration. L'application ecoute uniquement sur loopback. L'arret demande une fermeture Uvicorn propre, attend le processus dont l'identite est verifiee, puis arrete uniquement ce cluster :

```powershell
.\.venv\Scripts\python.exe scripts/local_env.py stop --runtime $runtime
.\.venv\Scripts\python.exe scripts/local_env.py start --runtime $runtime
```

Consultez les journaux prives `application.log` et `postgres.log` en cas d'echec. Ne les publiez pas. `/healthz` indique la vie du serveur ; `/readyz` verifie sa disponibilite et la base. La readiness ne remplace pas une recherche RAG reelle. Aucun lancement sur 8000, migration implicite ou service systeme n'est configure.

## Modele et absence de telechargement cache

Modele multilingue MiniLM L12, 384 dimensions ; depot ONNX public `qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`, revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0`. Les empreintes du modele ET du tokenizer sont versionnees dans `src/rag/model_artifact.json`. `prepare_model.py` telecharge cette revision explicitement ; l'application verifie les octets et charge les seuls fichiers locaux. Cache absent/modifie : indexation signalee en echec/degradation lexicale, jamais un faux succes hybride.

Pour un reseau restreint, `--from-snapshot CHEMIN` copie uniquement les cinq fichiers publics attendus, puis verifie les memes hashes. Aucun corpus utilisateur n'est necessaire. Limite reelle : 128 tokens dont 2 speciaux, fenetres utiles de 126 tokens, chevauchement 32. Aucun appel LLM payant n'est necessaire pour l'installation, les tests ou la recette livree.

## Inscription et activation manuelle sans paiement

L'utilisateur s'inscrit par `/register`, peut se connecter et voit « En attente d'activation ». Son espace est vierge : aucune politique, capacite, reference ou valeur de demonstration. Seul un operateur disposant de l'acces au serveur et de son credential peut accorder des droits. Un administrateur d'organisation n'est pas operateur de plateforme.

Bootstrap unique, sans mot de passe universel :

```powershell
.\.venv\Scripts\python.exe scripts/operator_access.py --env-file "$runtime\.env" --credential-file "$runtime\operator.token" bootstrap --actor "$env:USERNAME"
.\.venv\Scripts\python.exe scripts/operator_access.py --env-file "$runtime\.env" --credential-file "$runtime\operator.token" list
$userId = Read-Host 'UUID exact du compte affiche par list'
$orgId = Read-Host 'UUID exact de son espace affiche par list'
$expiry = Read-Host 'Echeance UTC choisie, format ISO avec fuseau (ex. YYYY-MM-DDTHH:MM:SS+00:00)'
$quota = Read-Host 'Nombre maximal explicite d analyses, revisions comprises'
$reason = Read-Host 'Motif de l activation'
.\.venv\Scripts\python.exe scripts/operator_access.py --env-file "$runtime\.env" --credential-file "$runtime\operator.token" activate --user-id $userId --organization-id $orgId --expires-at $expiry --max-analyses $quota --reason $reason
```

L'origine est `manual_without_payment`, visible dans le compte. L'activation et son audit sont transactionnels. Une repetition avec les memes parametres est sans effet ; une modification explicite constitue une nouvelle attribution et redemarre sa fenetre de quota. Toute analyse reservee durablement, y compris une revision ou un echec apres admission, consomme une unite. Une saturation annulant la reservation ne la consomme pas. Le quota ne bloque pas la consultation des donnees existantes pendant la validite du droit.

```powershell
$reason = Read-Host 'Motif de la revocation'
.\.venv\Scripts\python.exe scripts/operator_access.py --env-file "$runtime\.env" --credential-file "$runtime\operator.token" revoke --user-id $userId --organization-id $orgId --reason $reason
```

Revocation/expiration : les requetes suivantes avec le cookie existant sont refusees, les donnees restent conservees. L'activation ne restaure pas une organisation suspendue ou une appartenance revoquee. Elle ne donne aucun acces aux documents d'un collegue. Les anciennes commandes d'activation illimitee/import/demonstration ne sont pas des procedures operationnelles du lot 56.

## Sauvegarde PostgreSQL et fichiers, puis restauration neuve

Aucun operateur ne doit modifier le runtime pendant la maintenance. `runtime_backup.py` pose un verrou de maintenance, refuse une application encore lancee, utilise `pg_dump`, copie tous les fichiers de `data`, compare les hashes avant/apres et enregistre le hash du dump et de toutes les lignes de chaque table. Les commandes d'activation et de lancement refusent ce verrou. Les clients SQL externes doivent egalement etre arretes ; le controle avant/apres detecte une modification concurrente mais ne remplace pas leur mise au repos.

```powershell
.\.venv\Scripts\python.exe scripts/local_env.py stop --runtime $runtime
$backup = Join-Path $env:LOCALAPPDATA 'WM56-backup-neuf'
.\.venv\Scripts\python.exe scripts/runtime_backup.py backup --runtime $runtime --archive $backup
$restore = Join-Path $env:LOCALAPPDATA 'WM56-restore-neuf'
.\.venv\Scripts\python.exe scripts/local_env.py init --runtime $restore --pg-port 5548 --app-port 8058 --database wm56_restore_db
.\.venv\Scripts\python.exe scripts/runtime_backup.py restore --runtime $restore --archive $backup
.\.venv\Scripts\python.exe scripts/prepare_model.py --cache "$restore\models"
.\.venv\Scripts\python.exe scripts/local_env.py start --runtime $restore
```

Ne pas executer `migrate` entre init et restore : la cible doit etre vide. La restauration refuse sa source, toute base peuplee, tout stockage non vide ou hash invalide. Les roles et mots de passe sont recrees par init ; aucun mot de passe, cookie ou credential operateur n'est embarque dans le backup. Les comptes/audits/empreintes de credentials operateur en base sont restaures : conservez le credential serveur separement et en securite, ou bootstrappez explicitement une nouvelle identite operateur. Le nouveau secret de session impose une nouvelle connexion. Le cache public du modele se prepare separement.

## Tests, recette et checklist du second developpeur

```powershell
$tests = Join-Path $env:LOCALAPPDATA 'WM56-tests'
$evidence = Join-Path $env:LOCALAPPDATA 'WM56-private-evidence'
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
.\.venv\Scripts\python.exe scripts/run_tests.py --scratch-parent $tests --embedding-cache "$runtime\models" tests -q -rs --junitxml="$evidence\junit.xml"
.\.venv\Scripts\python.exe -m flake8 src scripts tests migrations main.py
```

Conserver les preuves dans `$evidence`, hors depot et contexte de build. Le lanceur declare le mode test AVANT les imports, cree une racine neuve, supprime les credentials fournisseurs et reutilise `db_target`. Les tests PostgreSQL lancent leur propre pgserver jetable si aucune URL dediee n'est fournie. En CI, `WM_REQUIRE_POSTGRES=1` interdit de presenter une absence de PostgreSQL comme un succes. Garder des chemins courts sous Windows pour les quatre UUID du stockage prive.

La recette `qa/browser_recipe.py` utilise Playwright installe dans un venv d'outillage distinct (lock `qa/requirements.lock.txt`). Elle exige un runtime exclusivement synthetique lance avec `local_env.py start --synthetic-recipe`. Cette option injecte une reponse capturee UNIQUEMENT pour la recherche de faits ; scoring, autorisations, migrations, PostgreSQL, embeddings, verification de citation et livrables restent reels. Ne jamais utiliser ce mode avec des documents clients.

Checklist de reprise : confirmer SHA de dev ; installer le lock dans un venv neuf ; verifier pip check/lock ; initialiser cluster/storage neufs ; migrer explicitement ; preparer modele ; verifier sante ; inscrire un compte ; constater refus metier ; bootstrap/activer avec duree/quota choisis ; configurer profil/politique/capacite ; ajouter un document synthetique ; rechercher ; analyser ; telecharger ; revoquer sans supprimer les donnees ; sauvegarder/restaurer dans une cible neuve ; noter OS, versions, SHA, commandes, echecs/skips. Aucun essai sur le PC du second developpeur n'est affirme avant retour de cette checklist.

## Reserves de securite et futur deploiement

Le lot 56 bis met Black a 26.3.1 et pytest a 9.0.3, avec leurs seules nouvelles dependances Pygments 2.21.0 et pytokens 0.4.1. Aucun outil ni alerte n'est retire du controle. Les audits local et GitHub du lock corrige ne trouvent plus de vulnerabilite connue ; verifier les checks GitHub Actions du SHA utilise. Les commandes d'installation et de reprise restent identiques. Une promotion vers main exige toujours une PR explicite et les controles de qualification.

Pour un futur deploiement, injecter les secrets hors Git, separer les roles de migration et d'execution selon la politique d'hebergement, ajouter TLS/reverse proxy, qualifier le stockage et la restauration sur cette infrastructure, adapter les garde-fous loopback avec revue explicite. Aucun hebergement, paiement, email reel, optimisation des tokens, B15 ou concurrence multi-processus n'est qualifie par ce lot.
