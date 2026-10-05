# B19-T2 — Rendu des documents découplé du résultat d'analyse

Défauts corrigés : (1) un échec de rendu laissait l'utilisateur sans aucun
moyen d'obtenir ses documents, alors que la ligne SQL et son snapshot
étaient intacts (B11-T1) ; (2) aucun statut **par document** n'existait
(`job.files` vaut `{}` ou porte les deux clés) ; (3) du texte AO/LLM libre
atteignait `Paragraph` de ReportLab **non échappé**.

Module : `src/web/document_rendering_service.py` (aucun second moteur de
rendu — il appelle le `DocumentGenerator` existant, avec `llm=None`).

## 1. Signatures publiques

```python
get_document_status(db, *, analysis_id, user_id, organization_id) -> dict[str, str]
# -> {"pdf": "available"|"unavailable", "docx": "available"|"unavailable"}
#    Les deux clés sont TOUJOURS présentes. Pas de troisième valeur.

regenerate_document(db, *, analysis_id, user_id, organization_id, kind) -> AnalysisDocument
# kind ∈ {"pdf", "docx"} (constante DOCUMENT_KINDS). Flush, jamais commit :
# la transaction appartient à l'appelant (session_scope de la route).
```

`"available"` signifie **uniquement** : une ligne `AnalysisDocument` de ce
mime existe pour ce `(analysis, user, organization)` **et**
`StorageService.resolve_for_download()` — l'appel exact que fait la route de
téléchargement — a résolu son `storage_path` vers un fichier réel dans les
racines de stockage. Tout le reste (pas de ligne, `storage_path` vide,
fichier supprimé, chemin hors racine, backend en erreur) est
`"unavailable"`. Un lien construit sur `"available"` ne peut donc pas 404.
« Jamais généré » et « généré puis perdu » sont volontairement
indiscernables : l'action utile est la même — régénérer.

## 2. Erreurs (toutes typées, sous-classes de `DocumentRenderingError`)

Chacune porte `.error_code` (stable) et `.user_message` (sûre à afficher).

| Exception | `error_code` | HTTP suggéré |
|---|---|---|
| `AnalysisNotFoundError` | `analysis_not_found` | **404** |
| `AnalysisOwnershipMismatchError` | `analysis_organization_mismatch` | **404** |
| `SnapshotUnusableError` | `analysis_snapshot_unusable` | **409** (ou 422) |
| `UnsupportedDocumentKindError` | `unsupported_document_kind` | **400** |
| `DocumentRenderingError` (rendu échoué) | `document_rendering_failed` | **500** |

Les deux premières se mappent au **même 404** délibérément : « inexistante »
et « appartient à autrui » ne doivent pas être distinguables. Les deux
points d'entrée lèvent les mêmes erreurs d'autorisation, donc une route peut
les traiter identiquement. La recherche passe par l'unique
`analyses_repo.get_by_id_for_user` existant — jamais un `db.get(Analysis,…)`.

## 3. Garanties

- **Aucun LLM, aucun recalcul.** `llm=None` rend l'appel modèle
  structurellement impossible. Score, décision (`INCOMPLET` compris,
  verbatim), `scoring_completeness`/`scoring_missing`,
  `enrichment_status`, `data_integrity`, `rag_selection_status`, evidences
  retenues et `scoring_policy_version` sont **relus** du snapshot, jamais
  re-dérivés de la politique/du profil **actuels** du compte.
- **Snapshot inutilisable ⇒ refus explicite**, jamais de contenu inventé :
  `ao`/`result` absents, ou dégradation B18-T2 concluant `"unavailable"`
  (score non fini) ⇒ `SnapshotUnusableError`. Le snapshot stocké n'est
  jamais réécrit (copie profonde privée).
- **Publication atomique** : réutilise `_prepare_target`/`_finalize` de
  `document_generator.py` (écriture en `<cible>.tmp`, `os.replace` seulement
  après succès complet). Un rendu échoué ne laisse ni `.tmp` ni fichier
  partiel qu'une route pourrait servir.
- **Pas de doublon.** L'attachement passe par `analyses_repo.upsert_document`
  (idempotent par `(analysis_id, mime_type)`) — jamais `add_document`, jamais
  un `AnalysisDocument` construit à la main. Chaque publication utilise un
  basename aléatoire neuf (donc aucune collision/écrasement partiel), puis
  **l'ancien fichier physique est supprimé** une fois la ligne pointant sur
  le nouveau. *Conséquence d'ordre* : l'appelant qui `rollback` après coup
  perd l'ancien fichier pendant que la ligne y revient — l'état honnête est
  alors `"unavailable"`, donc « régénérer », jamais un document faux.
- **Chemins** : même racine privée que le flux normal —
  `jobs.ANALYSIS_FILES_DIR/<user_id>/<job_id|analysis-id>/<hex>.<ext>`, lue à
  l'appel (pas capturée à l'import). Aucun segment ne dérive d'une entrée
  utilisateur ; jamais de répertoire global/partagé.

## 4. Échappement PDF (`src/livrables/document_generator.py`)

`Paragraph` de ReportLab **parse** un mini-XML (`<b>`, `<i>`, `<br/>`,
`<font>`…). Sans échappement, `"AT&T"` ou `"budget <50k€"` étaient
**silencieusement perdus** du PDF (vérifié : les 4 chaînes de test
disparaissent quand on neutralise le correctif).

Correctif : helper `_escape_for_pdf(value)` utilisant
**`xml.sax.saxutils.escape` (stdlib)** avec `{'"': "&quot;", "'": "&apos;"}`.
Choix motivé : aucune nouvelle dépendance ; `reportlab.lib.utils` n'expose
pas d'échappeur généraliste dans cette version ; le paraparser étant un
parseur XML, l'échappeur XML de la stdlib est exactement l'outil adapté. Les
quotes sont incluses car la markup ReportLab les traite spécialement dans
les **attributs** de balise. Appliqué aux 14 sites dynamiques de
`generate_pdf` uniquement ; les libellés littéraux et le `<b>…</b>` que ce
fichier émet volontairement autour d'un nom de critère ne sont pas touchés.
Strictement **préservant** : seuls les 5 caractères de markup deviennent des
entités, que ReportLab re-rend en caractère littéral. Rien n'est supprimé ni
tronqué.

**DOCX : aucun correctif équivalent nécessaire.** `python-docx` affecte la
chaîne à un **nœud texte lxml** (`run._r.text`) ; lxml échappe un nœud texte
à la sérialisation, donc la markup est stockée et relue telle quelle, jamais
interprétée. Vérifié, pas supposé : un test écrit
`<b>AT&T</b> & <script>alert(1)</script>` via `add_paragraph` et le relit
identique, et assert l'absence de `&amp;` dans le DOCX produit.
