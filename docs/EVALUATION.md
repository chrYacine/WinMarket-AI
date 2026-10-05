# Evaluation RAG, LLM et scoring

La qualification reutilise les contrats et tests des lots 44, 49, 51, 51 bis et 52. Le corpus `qa/evaluation_corpus.json` est explicitement synthetique ; il n'est jamais une configuration implicite d'un utilisateur.

## Niveaux de preuve

1. Deterministe : autorisations, provenance exacte, inconnus/contradictions, etats INCOMPLET, comptage par reference, revisions immuables, refus d'auto-activation et quotas.
2. Reel local : PostgreSQL/pgvector, embeddings ONNX et tokenizer charges depuis les octets epingles, indexation, recherche paraphrasee, remplacement/suppression et preservation de l'index valide si la reconstruction echoue.
3. LLM simule : la recette navigateur injecte une reponse sourcee capturee a l'adaptateur de recherche de faits. Le serveur reverifie la citation, l'appartenance et la version avant acceptation. Aucun resultat de scoring n'est prefabrique et aucun appel externe n'est effectue. Ce niveau ne mesure pas la qualite d'un LLM reel.

## Attendus et metriques

`qa/evaluate_rag.py --cache CHEMIN --output FICHIER` cree son propre PostgreSQL jetable, migre le schema et charge le corpus synthetique. Deux requetes ont des references attendues explicites : paraphrase renovation energetique/ecole -> chantier.md ; identifiant QUALIBAT -> certification.md. Rappel des references a 3 = references attendues retrouvees / references attendues ; integrite = extraits exactement egaux a la tranche de leur texte canonique / extraits retournes. Ce petit corpus n'etablit pas un gain general de l'hybride sur le lexical.

Les autres attendus restent les regressions existantes, sans nouveau seuil metier :

| Cas | Attendu | Preuve |
|---|---|---|
| Hors sujet, pas de reranker | Un voisin vectoriel seul n'influence pas le scoring | test_lot51_bis_rag_frontiers |
| Plusieurs passages, meme reference | Une seule reference comptee | test_lot51_bis_rag_frontiers |
| Deux comptes/deux espaces | Aucun document de l'autre scope | test_lot51_hybrid_rag, recette navigateur |
| Document remplace/supprime | Ancienne version exclue | test_lot51_hybrid_rag, test_lot52_completion_facts_http |
| Fait absent ou contradictoire | Pas de valeur inventee ; INCOMPLET si aucun bloqueur confirme | test_lot44_criteria_contract, test_lot52_fact_search |
| Citation acceptee puis revision | Provenance conservee, parent inchange | test_lot52_completion_facts_http, recette navigateur |
| Reconstruction en echec | Index valide precedent conserve | test_lot51_bis_rag_frontiers |

## Manifeste et changement d'index

`scripts/build_evaluation_manifest.py` calcule les hashes du lock, des prompts, schemas/modeles et parametres ; il inclut le commit quand Git est disponible et permet de fournir explicitement le SHA verifie. `src/rag/model_artifact.json` epingle la revision HF et les cinq fichiers. Generer le manifeste et les resultats dans un dossier prive hors depot/build, en indiquant le SHA teste. Ne pas commiter les sorties generees ; conserver ici uniquement les parametres et corpus synthetiques reproductibles.

Dimensions 384, limite reelle 128 tokens (126 utiles), chevauchement 32, RRF K=60. La revision enregistree sur chaque version documentaire combine l'identite technique de decoupage et la revision chargee. Toute modification modele/tokenizer/dimension/decoupage exige une nouvelle identite de revision et une reconstruction explicite. Garder l'ancien cache/model pendant la validation, reconstruire les nouvelles versions, comparer les evaluations et basculer seulement apres succes. Le pipeline existant n'efface l'ancien index qu'apres construction reussie ; un echec le preserve. Une dimension autre que celle du stockage vector(384) est refusee : une nouvelle migration/index distinct est alors necessaire.

La simulation de politique reste lexicale, sans preparation implicite du modele. Aucune reindexation de l'ancien corpus, aucun recalibrage de baremes, optimisation de tokens ou registre MLOps additionnel. Une regression ou une reserve non revue interdit une promotion main.
