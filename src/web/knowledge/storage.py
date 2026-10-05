"""Private, per-(organization, owner) storage root for uploaded reference
documents — separate from src/web/storage/service.py (B01 livrables:
generated PDF/DOCX), and from data/reg_docs (the global demo corpus).

Layout: <LOCAL_STORAGE_PATH>/knowledge/<organization_id>/<owner_user_id>/
<document_id>/<version_id>/<opaque_name>.<ext> — only internal ids in the
path, never the original filename (that is display-only, stored in
KnowledgeDocument.original_filename).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from src.core import config


class KnowledgeStorageError(Exception):
    """Raised for a path that would escape the private knowledge root."""


def _root() -> Path:
    return Path(config.LOCAL_STORAGE_PATH).resolve() / "knowledge"


def document_version_dir(*, organization_id: uuid.UUID, owner_user_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID) -> Path:
    return _root() / str(organization_id) / str(owner_user_id) / str(document_id) / str(version_id)


def write_upload(
    *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, document_id: uuid.UUID, version_id: uuid.UUID,
    suffix: str, content: bytes,
) -> str:
    """Write the uploaded bytes to a fresh, private path and return the
    storage key (a path relative to LOCAL_STORAGE_PATH, POSIX-style,
    resolvable back via resolve_private_path — never an absolute path
    handed back to the caller)."""
    target_dir = document_version_dir(
        organization_id=organization_id, owner_user_id=owner_user_id, document_id=document_id, version_id=version_id,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    opaque_name = f"{uuid.uuid4().hex}{suffix.lower()}"
    target = target_dir / opaque_name
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(content)
    import os
    os.replace(tmp, target)  # atomic publish, mirrors src/livrables/document_generator.py
    return target.resolve().relative_to(Path(config.LOCAL_STORAGE_PATH).resolve()).as_posix()


def resolve_private_path(storage_key: str) -> Path:
    """Resolve a storage key back to a file, confined to the private
    knowledge root — raises FileNotFoundError (never a bare path) for both
    "escapes the root" and "doesn't exist", mirroring
    src/web/storage/service.py::LocalStorageService.resolve_for_download."""
    root = _root()
    candidate = (Path(config.LOCAL_STORAGE_PATH).resolve() / storage_key).resolve()
    if not (candidate == root or root in candidate.parents):
        raise FileNotFoundError(f"Chemin hors de la racine de connaissances privée : {candidate}")
    if not candidate.exists():
        raise FileNotFoundError(f"Fichier introuvable : {candidate}")
    return candidate


def delete_private_path(storage_key: str) -> bool:
    """Best-effort physical delete — returns True on success. A failure
    here must never be silently swallowed by the caller: the document's
    metadata is already marked deleted regardless (see
    documents_service.delete_document), and a failed unlink is logged so
    it can be retried/audited rather than declared clean."""
    try:
        path = resolve_private_path(storage_key)
    except FileNotFoundError:
        return True  # already gone — not a failure to report
    try:
        path.unlink()
        return True
    except OSError:
        return False
