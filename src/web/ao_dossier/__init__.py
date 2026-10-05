"""Lot 47 bis — the AO dossier: several tender pieces (RC, CCTP, CCAP, acte d'engagement, annexes) that
make ONE analysis.

- `limits`  : the categories, the labels the interface shows and the effective limits (one place, served to
              the API and to the page);
- `storage` : private storage of the pieces, bounded streaming (never 100 Mo in one block);
- `intake`  : structure + size + format + content validation of a whole dossier BEFORE any job exists;
- `service` : database rows, loading a validated dossier for the job (from the database, not from memory),
              and the public (path-free) summary.

A dossier is INPUT to one analysis. It is never indexed, searched or counted as a professional reference
(that is the account's own RAG corpus, `src/web/knowledge`), and nothing here reads the account's scoring
policy or produces a score.
"""
