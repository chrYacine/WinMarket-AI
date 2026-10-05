# B07-T1 / B08-T1 — Profil acheteur & capacité : contrat

Règle commune : un fait est soit réel, soit explicitement absent. Aucune
valeur plausible n'est inventée pour combler un trou, et aucune donnée
fictive n'est présentée comme vérifiée.

## B07-T1 — `CompanyProfile.source` (profil de l'ACHETEUR de l'AO)

`CompanyProfile` (`src/core/models.py`) décrit uniquement l'acheteur nommé
dans l'AO — jamais l'ESN elle-même (ça, c'est `ProviderProfile`,
`src/web/database/models.py`). Aucun champ n'a été ajouté au modèle.

| `source` | Signification | Champs métier |
|---|---|---|
| `pappers` | Un seul candidat, dont le nom correspond au nom de l'acheteur | renseignés depuis la fiche réelle |
| `ambiguous` | Plusieurs candidats, ou un candidat unique au nom non concordant | défauts du modèle — aucun candidat n'est choisi |
| `unavailable` | Enrichissement non autorisé, pas de token, pas de nom d'acheteur, zéro résultat, ou panne/exception du fournisseur | défauts du modèle |

`mock` **n'existe plus** et n'est plus jamais produit. Le faux profil qu'il
étiquetait (effectif « 250-500 », CA « 50M€ estimés », ville « Paris »,
ancienneté « 10+ ans », solidité « Bonne ») est supprimé.

Dans les cas `ambiguous`/`unavailable`, seul `raison_sociale` est rempli :
le nom de l'acheteur **tel qu'écrit dans l'AO**, verbatim (jamais
« Client démo »). Tous les autres champs gardent le défaut du modèle
partagé, qui s'affiche « Non renseigné » — y compris `solidite_financiere`,
dont le défaut a été changé de `"Moyenne"` (un ancien défaut qui avait
l'air d'une évaluation réelle) vers `"Non renseigné"` par ce même lot,
pour rester cohérent avec les autres champs. Ce changement de défaut est
purement déclaratif : `src/agents/scoring_engine.py` ne compare ce champ
qu'aux valeurs `"Bonne"`/`"A verifier"` pour la branche favorable, donc
`"Moyenne"` et `"Non renseigné"` tombaient déjà — et tombent toujours —
dans la même branche neutre (score 60).

### `enrich(company_name, *, external_enrichment_enabled=True)`

Nouveau paramètre mot-clé. Le défaut `True` préserve à l'identique le
comportement de l'appelant figé `src/core/pipeline.py`
(`enrich(ao.client)`, sans kwarg) : token configuré ⇒ recherche tentée.
Le chemin SaaS (`src/web/jobs.py`, `/simulate`) passe toujours la valeur
explicitement, issue de `ProviderProfile.external_enrichment_enabled`
(défaut **False** en base : aucun compte n'est opté par défaut).
Avec `False` : **zéro appel réseau**, retour d'un profil `unavailable`.

Détection d'ambiguïté : la requête demande désormais jusqu'à 5 candidats
(`par_page=5`, contre 1 avant) — sans quoi les homonymes seraient
structurellement invisibles et `resultats[0]` resterait un pari. Aucune
exception ne s'échappe de `enrich()` ; aucun token n'est journalisé.

## B08-T1 — Calcul de capacité

Supprimés : les surcharges arbitraires liées aux technologies de l'AO
(+10 pour `sap`/`mainframe`/`cobol`/`blockchain`, +8 au-delà de 6
technologies, et le forçage de `equipe_disponible=False`). La capacité ne
dépend plus **en rien** de ce que l'AO demande.

```
charge    = plan.charge_globale_pct                 # tel quel, sans ajout
remaining = 100 - charge
ok        = remaining >= plan.disponibilite_minimum_pct
            and (not plan.capacites_par_pole
                 or min(plan.capacites_par_pole.values()) >= plan.disponibilite_minimum_pct)
```

`charge_globale_pct` et `capacites_par_pole` sont deux signaux réels et
**indépendants** sur la même équipe : jamais additionnés ni recoupés,
seulement comparés au même seuil. Aucune répartition par rôle n'est
inventée, aucun pôle absent du dictionnaire n'est nommé. Le `commentaire`
cite les vrais nombres et, si un pôle est le facteur décidant, son vrai
nom et son vrai pourcentage — plus aucune mention de « compétence
critique » liée aux technologies.

## Pour le front

- `GET/POST /api/capacity` expose `disponibilite_minimum_pct` (entier,
  0-100, défaut 10) : « capacité restante minimale exigée pour considérer
  l'équipe disponible ». Le défaut 10 reproduit exactement l'ancien seuil
  codé en dur — un compte existant ne change pas de comportement.
  **Lot 48 (défaut confirmé, corrigé)** : l'API l'exposait depuis B08-T1,
  mais aucun champ du formulaire (`/app/analyser`, modale de capacité)
  ne le montrait ni ne le renvoyait à l'enregistrement — chaque compte
  recevait donc silencieusement le seuil 10 % de la colonne, sans jamais
  pouvoir le voir ni le choisir, alors qu'il décide directement l'état
  du critère « Disponibilité de l'équipe » (GO/INCOMPLET). Le champ
  « Seuil minimum de disponibilité pour démarrer un projet (%) » est
  désormais dans la modale (`templates/app_analyze.html`,
  `static/js/analyze.js`) : chargé depuis `GET /api/capacity`, envoyé à
  `POST /api/capacity`. Le défaut 10 reste la valeur de départ tant que
  le compte ne l'a pas changé (compatibilité) ; il est maintenant visible
  et modifiable, comme `charge_globale_pct`.
- Le `PUT`/`GET` du profil prestataire expose `external_enrichment_enabled`
  (booléen, défaut **false**) : autorisation explicite du compte à envoyer
  le nom de l'acheteur d'un AO à un annuaire externe (Pappers). Tant que
  c'est `false`, aucun appel externe n'est émis et le profil acheteur
  s'affiche comme non renseigné — ce n'est pas une erreur.
- Un `source` valant `ambiguous` mérite un libellé distinct de
  `unavailable` côté UI : « plusieurs entreprises portent ce nom »
  plutôt que « information indisponible ».
