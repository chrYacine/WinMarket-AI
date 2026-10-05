# Architecture et invariants de reprise

FastAPI (`main.py`) expose les pages Jinja et les routes ; `src/web` porte auth, sessions, jobs, dossiers, repositories et stockage. Le moteur de scoring reste unique dans `src/agents/scoring_engine.py` avec le catalogue ferme d'evaluateurs. Les prompts sont dans `prompts/`. Streamlit n'est pas utilise.

La configuration privee se resout dans `src/web/scoring_context.py` : analyse = politique active ; simulation = brouillon. Toute configuration est attribuee a `(organization_id, owner_user_id)`. Aucun defaut metier n'est applique a un nouveau compte. Une donnee absente/contradictoire reste inconnue : INCOMPLET, jamais une note inventee. Un bloqueur confirme peut produire NO-GO malgre d'autres inconnues.

L'autorite commune d'acces reste `evaluate_subscription_access`, completee par le scope du grant et les memberships. L'identite d'operateur serveur est distincte des utilisateurs. L'admission des analyses reserve le quota dans la transaction de la file durable ; l'expiration est aussi recontrolee au traitement. Migration 0017 additive, audit preserve en cas de tentative de downgrade.

Le RAG prive utilise le pipeline documentaire/versionnement existant. Le mode hybride exige PostgreSQL/pgvector ET le flag explicite. Le lexique reste disponible ; la fusion RRF travaille sur l'identite du passage/chunk et le scoring compte les references distinctes, pas le nombre de fenetres. Une similarite vectorielle seule ne devient pas une preuve metier confirmee. La simulation de scoring reste lexicale et locale.

Dossiers multi-pieces, provenance, moderation et revisions sont conserves. Accepter une proposition sourcee reverifie la citation et les droits, puis cree une revision : le resultat parent et ses livrables ne sont pas modifies. PDF/DOCX utilisent la projection commune `result_presentation`.

Memoire locale distillee depuis les instructions du projet : ne jamais reutiliser une session SQL d'une requete dans un worker ; figer les snapshots de politique/profil par analyse ; ne pas recalculer l'historique ; utiliser `db_target` avant les migrations ; activer le mode test avant les imports ; isoler tous les fichiers temporaires ; les classes LLM simulees ne constituent pas une qualification d'embeddings. Les details contractuels sont dans `docs/api/`.
