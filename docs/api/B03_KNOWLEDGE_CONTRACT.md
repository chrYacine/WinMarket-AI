# B03 — Contrat API : documentation, RAG, capacité privés

Destiné au développeur frontend. Toutes les routes ci-dessous exigent une session authentifiée (cookie `wm_session`, voir B01/B02) et un abonnement Starter actif — un appel sans session renvoie `401`, sans abonnement actif `403`.

**Sélection d'organisation** : si le compte n'appartient qu'à une seule organisation active (cas normal aujourd'hui), rien à faire. S'il appartient à plusieurs, toute route ci-dessous accepte un paramètre de requête `organization_id` ; sans lui, une réponse `409` signale l'ambiguïté (`{"detail": "Plusieurs organisations sont disponibles pour ce compte : précisez organization_id."}`). `organization_id` n'est jamais un droit — le serveur vérifie qu'il correspond à une appartenance active réelle du compte.

**CSRF** *(corrigé au lot 45 — l'ancienne rédaction de ce paragraphe, « pas de jeton CSRF par requête, protection reposant sur `SameSite=Lax` », était périmée depuis B14-T1)* : **toute mutation** ci-dessous (`POST /api/knowledge/reload`, `POST /api/knowledge/documents`, `POST /api/knowledge/documents/{id}/versions`, `DELETE /api/knowledge/documents/{id}`) exige un jeton CSRF en double soumission : l'en-tête `X-CSRF-Token` doit égaler le cookie CSRF, sinon `403`. Le jeton est exposé à la page par `<meta name="csrf-token">` (lu par `window.wmCsrfToken()`, `static/js/main.js`). `SameSite=Lax` reste une défense complémentaire, jamais la seule. Les lectures (`GET`, y compris le téléchargement) n'en exigent pas. Voir `docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md`.

---

## Documentation de référence

### `GET /api/knowledge`
Résumé du corpus privé de l'appelant (forme stabilisée, inchangée depuis avant B03).
```json
{
  "total_documents": 3,
  "total_kb": 128,
  "groups": [{"folder": "Mes documents", "documents": [{"source": "cctp.pdf", "chars": 45210}]}],
  "corpus_empty": false
}
```

### `POST /api/knowledge/reload`
Force la reconstruction de l'index (utile après un import externe). Permission `knowledge:write` (analyst/admin). Réponse identique à `GET /api/knowledge`.

### `GET /api/knowledge/search?q=...`
```json
{
  "query": "hébergement souverain",
  "corpus_empty": false,
  "results": [
    {"source": "cctp.pdf", "score": 0.42, "relevance_pct": 100, "excerpt": "…texte…"}
  ]
}
```
`q` vide → `results: []` immédiatement. Corpus vide → `corpus_empty: true`, jamais d'exception.

### `GET /api/knowledge/documents`
Liste détaillée (nouvelle route).
```json
{"documents": [
  {"id": "uuid", "original_filename": "cctp.pdf", "status": "ready", "active_version_id": "uuid",
   "active_version_number": 1, "latest_version": {"id": "uuid", "version_number": 2, "status": "failed", "error_code": "OCR_REQUIRED"},
   "created_at": "...", "updated_at": "..."}
]}
```
`status` : `"ready"` (une version active existe) ou `"processing"` (aucune version n'a encore réussi). **`status` n'est pas l'état de la dernière version envoyée** : un document dont le seul envoi a échoué est `"processing"` alors que rien n'est en cours. Depuis le lot 45, `latest_version` (`null` sans version) donne l'état réel du dernier envoi (`received` / `processing` / `ready` / `failed` + `error_code`) et `active_version_number` le numéro de la version consultée par la recherche (`null` sans version active) — champs **additifs**, les précédents sont inchangés. La liste ne contient que les documents non supprimés du compte de l'appelant, dans l'organisation sélectionnée (jamais ceux d'un collègue, même administrateur).

### `POST /api/knowledge/documents` (multipart, champ `file`)
Permission `knowledge:write`. Formats acceptés : `.md`, `.txt`, `.pdf`, `.docx`. Limite `KNOWLEDGE_MAX_FILE_SIZE_MB` (10 Mio par défaut) et `KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS` (100 par défaut).

- **`201`** — extraction réussie :
  ```json
  {"document": {...}, "version": {"id": "uuid", "version_number": 1, "status": "ready", "error_code": null}}
  ```
- **`422`** — deux formes distinctes, à ne pas confondre :
  1. *extraction échouée* (le document **existe** avec une version `failed`, il occupe une place de la limite de documents actifs et n'est pas recherchable) :
     ```json
     {"detail": {"document": {...}, "version": {"id": "uuid", "version_number": 1, "status": "failed", "error_code": "OCR_REQUIRED"}}}
     ```
     `error_code` : `UNSUPPORTED_CONTENT` (contenu incohérent avec l'extension pendant l'extraction), `CORRUPTED_FILE`, `OCR_REQUIRED` (PDF scanné sans texte), `EMPTY_CONTENT`, `CONTENT_TOO_LARGE`.
  2. *refus avant tout enregistrement* — extension non acceptée ou signature PDF/DOCX invalide : `{"detail": {"error_code": "UNSUPPORTED_CONTENT", "message": "..."}}` ; **aucun document ni version n'est créé**.
  Un client ne renvoie jamais automatiquement le même fichier après un `422` de forme 1 : le document en échec apparaît dans la liste, à remplacer par une version corrigée ou à supprimer. **Règle de quota (inchangée, confirmée au lot 47 — décision produit requise pour la modifier)** : le comptage serveur (`count_active_documents`) prend tout document non supprimé, y compris un document dont aucune version n'a réussi ; seule la suppression libère la place, et remplacer un document existant n'en consomme jamais une. La limite est celle du serveur (`KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS`) : le client l'affiche, ne la recalcule pas.
- **`413`** — fichier au-delà de la limite de taille (refusé pendant la réception, avant tout traitement).
- **`409`** — `{"detail": {"error_code": "CORPUS_FULL", "message": "..."}}` — limite de documents actifs atteinte.
- **`403`** — rôle insuffisant (viewer) ou appartenance révoquée.

### `GET /api/knowledge/documents/{document_id}`
Détail + historique des versions.
```json
{"id": "uuid", "original_filename": "cctp.pdf", "status": "ready", "active_version_id": "uuid",
 "versions": [{"id": "uuid", "version_number": 1, "status": "ready", "error_code": null, "created_at": "..."}]}
```
`404` générique si le document n'existe pas OU appartient à un autre compte — aucune distinction observable.

### `GET /api/knowledge/documents/{document_id}/download`
Télécharge le fichier original tel qu'uploadé (pas le texte extrait) de la **version active** (lecture : autorisée au rôle lecteur pour ses propres documents). `404` si absent/interdit/supprimé ou sans version active, `404` aussi si le fichier physique a disparu du disque — le texte déjà indexé et les analyses passées ne sont alors pas affectés. **Lot 47 — nom, extension et `Content-Type` décrivent la version réellement servie** : le nom est le radical *assaini* du nom de création du document (dernier composant de chemin, sans caractère de contrôle, guillemet, chevron, `:`, `*`, `?`, `|`, ni point/blanc en bordure, 120 caractères au plus ; à défaut `document`), l'extension et le type viennent des métadonnées serveur de la version (format détecté à l'envoi — extension + octets magiques — recoupé avec le fichier stocké) : `.pdf` → `application/pdf`, `.docx` → `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `.md` → `text/markdown`, `.txt` → `text/plain`. Un format inconnu ou incohérent (ancien enregistrement) donne un nom neutre **sans extension inventée** et `application/octet-stream`. Réponse en `attachment` avec `X-Content-Type-Options: nosniff`. Aucun fichier n'est renommé sur disque (noms opaques) et aucun chemin n'est déduit du nom fourni. Après un remplacement échoué, la version active précédente est servie avec **ses** métadonnées. Le nom de chaque version envoyée n'est pas conservé (pas de colonne dédiée : aucune migration).

### `POST /api/knowledge/documents/{document_id}/versions` (multipart, champ `file`)
Nouvelle version. Permission `knowledge:write`. Mêmes codes que l'upload initial, sauf succès qui renvoie `200` (pas `201` — le document existait déjà). La version précédente reste active/recherchable tant que celle-ci n'a pas réussi.

### `DELETE /api/knowledge/documents/{document_id}`
Permission `knowledge:write`. Suppression logique immédiate (exclu de toute recherche future) ; suppression physique best-effort.
```json
{"status": "deleted", "physically_cleaned": true}
```
`physically_cleaned: false` signale un échec de nettoyage disque à surveiller côté exploitation — le document reste bien supprimé côté application dans les deux cas. `physically_cleaned: true` signifie seulement que les fichiers ont été retirés du stockage applicatif : ni les sauvegardes du serveur ni un effacement sécurisé du disque ne sont garantis. Les analyses déjà réalisées conservent leur copie des preuves citées (`Analysis.result_data` n'est jamais réécrit).

---

## Capacité privée

### `GET /api/capacity`
```json
{"status": "configured", "charge_globale_pct": 40, "disponibilite_pct": 60,
 "nombre_projets_en_cours": 2, "projets_en_cours": ["Projet X"], "capacites_par_pole": {"Software Engineering": 30}}
```
`status: "unconfigured"` (valeurs à 0) tant que rien n'a été sauvegardé — c'est l'état d'un compte neuf.

### `POST /api/capacity`
Permission `capacity:configure` (**analyst ET organization_admin** depuis B06-T1 — viewer seul reçoit `403`; voir `docs/api/B06_SCORING_CONFIG_CONTRACT.md` pour le détail du changement). Écrit uniquement la capacité de l'appelant lui-même, jamais celle d'un collègue.
```json
{"charge_globale_pct": 40, "nombre_projets_en_cours": 2, "projets_en_cours": ["Projet X"], "capacites_par_pole": {"Software Engineering": 30}}
```
Réponse : même forme que `GET /api/capacity`.

---

## Lancement d'analyse — préconditions capacité ET scoring

### `POST /api/analyze`
Inchangé pour le frontend (`mode`, `text`/`file`/`example_id`), **sauf** deux refus explicites, vérifiés dans cet ordre :
```json
409 {"detail": {"error_code": "CAPACITY_NOT_CONFIGURED", "message": "Configurez votre capacité (charge, projets en cours) avant de lancer une analyse."}}
409 {"detail": {"error_code": "SCORING_NOT_CONFIGURED", "message": "Configurez et activez votre politique de scoring avant de lancer une analyse."}}
```
**B06-T1** ajoute le second refus (`SCORING_NOT_CONFIGURED`) — voir `docs/api/B06_SCORING_CONFIG_CONTRACT.md` pour le parcours complet de configuration. À afficher comme une invite à configurer `/api/capacity` puis `/api/scoring-config` avant de relancer l'analyse — jamais une erreur générique. Permission `analysis:create` (viewer refusé, `403`).

---

## Page HTML

### `GET /app/base-connaissances`
Même contrôle d'accès que `/api/knowledge` désormais (avant B03, cette page ne vérifiait que l'authentification). Un refus (appartenance révoquée, organisation suspendue, sélection d'organisation ambiguë) rend une page d'erreur au lieu du contenu, avec le même code HTTP que l'API JSON (`403`/`409`) — jamais un rendu vide silencieux qui masquerait le refus.

**Lot 45 — la page gère les documents.** Elle liste les documents du compte (nom, dates, version active, état réel de la dernière version), ajoute (`POST /api/knowledge/documents`, champ `file`), remplace (`.../versions`), télécharge l'original, supprime après confirmation explicite et recherche dans le corpus privé (passages + source). Chaque appel porte explicitement `organization_id` ; le serveur revérifie appartenance, rôle (`knowledge:write` — un lecteur ne voit que la lecture) et CSRF. Une réponse arrivée après un changement d'organisation est ignorée. Noms de fichiers, messages et extraits sont affichés comme **texte** (`textContent`), jamais comme HTML. Un corpus vide est affiché comme tel — aucun document d'exemple n'est jamais injecté. Ajouter, remplacer ou supprimer un document ne modifie ni les faits métier déclarés, ni les certifications, ni les critères, ni la politique de scoring active, et ne recalcule aucune analyse existante.
