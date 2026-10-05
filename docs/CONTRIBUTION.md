# Contribution et promotion

Depot : `chrYacine/WinMarket-AI`. `dev` accueille les changements ; `main` conserve la version promue. Le code source est distribue sous la licence MIT conservee a la racine, avec sa mention d'auteur originale.

Travailler sur une branche courte `feature/sujet` ou `fix/sujet` depuis dev, avec des commits cibles, les tests pertinents et une PR vers dev. Utiliser les fixtures synthetiques, jamais les comptes ou documents clients.

Promotion courante : PR dev vers main apres controles CI et revue. L'import initial sur dev puis main est une operation distincte, demandee par le proprietaire. Ne jamais forcer un push pour remplacer l'historique.

Protections recommandees : PR obligatoire, controles qualification et dependency-audit obligatoires, conversations resolues, interdiction des force-push et suppressions. Ce guide ne configure pas les protections GitHub : leur activation doit etre verifiee dans les parametres du depot.

CI : push dev, PR dev/main et lancement manuel ; permissions contents:read, aucun secret fournisseur, actions epinglees. PostgreSQL/pgvector utilise une base de test jetable. Les controles cibles sont remplaces par la suite complete lorsqu'un commit modifie le lock ou requirements-test.txt. Consulter le resultat du run pour connaitre les validations effectivement executees.

Ne publier aucun rapport interne, capture, dump de base, archive de recette, fichier .env ou donnees utilisateur, dans Git comme dans les artefacts Actions. Conserver les preuves privees hors depot ; les checks et erreurs techniques restent visibles.
