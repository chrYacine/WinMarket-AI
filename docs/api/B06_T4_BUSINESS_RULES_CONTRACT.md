# B06-T4 — `business_rules` : règles métier configurées, jamais codées en dur

Les quatre valeurs métier qui étaient codées en dur dans
`ScoringEngine.score()` viennent désormais de la `ScoringPolicy` privée et
validée du compte, via la colonne JSON `business_rules` (transportée jusqu'au
moteur par `ScoringPolicySnapshot`).

## Clés de `business_rules`

| Clé | Unité | Signification | Règle bloquante associée |
|---|---|---|---|
| `budget_minimum_eur` | EUR (≥ 0) | Budget minimal de rentabilité | `budget_estime < seuil` → bloquant |
| `max_charge_pct` | % (0-100) | Charge d'équipe maximale acceptable | `charge_actuelle_pct > seuil` → bloquant |
| `max_unmastered_technologies` | nombre (≥ 0) | Nombre de technologies non maîtrisées toléré | `len(non maîtrisées) >= seuil` → bloquant |
| `certification_penalty_score` | score 0-100 | Note du critère « Certifications requises » quand une certification obligatoire manque | aucune (note, pas bloqueur) |

**Clé absente = « non configurée »**, jamais 0 ni l'ancienne constante. Une
clé absente n'empêche pas l'activation d'une politique
(`scoring_policy_validation.validate_business_rules`) : un compte activé avant
ce ticket est donc légitimement incomplet, et c'est le moteur qui le dit.

## Contrat « incomplete » / `INCOMPLET` / `scoring_missing`

Quand une politique est injectée (`policy is not None`) et qu'une règle est
non configurée, `ScoringEngine.score()` :

1. **n'évalue pas** le bloqueur concerné — ni « pas de bloqueur » (qui
   masquerait un vrai problème), ni « bloqueur présent » (qui fabriquerait un
   NO-GO) ;
2. ajoute la clé à `ScoringResult.scoring_missing` (trié) et met
   `scoring_completeness = "incomplete"` ;
3. renvoie `decision = "INCOMPLET"`.

Précédence exacte de la décision :

```
decision = "NO-GO"      si un bloqueur a réellement été déclenché
         = "INCOMPLET"  sinon, si des règles manquent (politique injectée)
         = GO / GO SOUS RESERVE / NO-GO  sinon (logique de seuils inchangée)
```

Un bloqueur qui s'est déclenché via une règle entièrement déterminée (les
certifications détenues, par exemple) reste un **NO-GO confirmé** : le
résultat est alors `decision="NO-GO"` ET `scoring_completeness="incomplete"`.

`score_global` est **toujours** la somme pondérée habituelle des critères
calculés : jamais renormalisée, jamais mise à zéro, jamais plafonnée. C'est un
chiffre honnête de ce qui a pu être calculé ; `scoring_missing` dit ce qui
manque. Les `recommandations` d'un résultat `INCOMPLET` invitent à compléter la
configuration — jamais les actions « GO » (affecter l'équipe, lancer le
mémoire technique).

### Cas particulier : `certification_penalty_score`

`CriterionScore.score` est un float obligatoire : quand une certification
manque et que la pénalité n'est pas configurée, le moteur applique un
**placeholder générique de 20** (valeur historique, sans portée métier) et
signale `certification_penalty_score` dans `scoring_missing`. Si aucune
certification ne manque, la pénalité n'était pas nécessaire : elle n'est
**pas** signalée comme manquante.

Conséquence structurelle : cette clé ne peut jamais, à elle seule, produire un
`INCOMPLET` — une certification manquante déclenche toujours le bloqueur
correspondant, donc un NO-GO confirmé.

## Chemin hérité (`policy=None`)

Streamlit / `src/core/pipeline.py` et tout appel sans `policy=` conservent un
comportement **identique à l'octet près** : les constantes historiques
`50 000` / `95` / `4` / `20` restent utilisées, `scoring_completeness` vaut
`"complete"` et `scoring_missing` est vide. C'est le seul endroit où ces
constantes survivent légitimement : un appelant sans politique n'a aucune
configuration qui puisse manquer.

## B19-T1 — system prompt de `enrich_with_llm`

`enrich_with_llm(ao, result, llm, provider_profile=None)` construit désormais
son system prompt à l'exécution (`_build_scoring_system_prompt`) à partir du
`ProviderProfile` réel du compte : `raison_sociale` et `competences`
déclarées. L'ancienne biographie fictive (effectif, ancienneté, palmarès de
marchés remportés) est supprimée et ne doit pas réapparaître : elle était
présentée au LLM comme un fait et ressortait dans les justifications et
recommandations. Sans profil (ou sans `raison_sociale`), le prompt retombe sur
une formulation générique sans aucun fait inventé.
