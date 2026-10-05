"""Private storage of the pieces of a dossier — `<LOCAL_STORAGE_PATH>/ao_dossiers/<organization>/<user>/<dossier>/`.
Only internal ids appear in a path: the name the user gave a file is display-only and never becomes part of a
path. A separate root from `knowledge/` (the RAG corpus) and from the generated deliverables.

Files are written by BOUNDED streaming (a 256 KiB chunk at a time, running total, incremental SHA-256): a
100 Mo dossier is never read into memory in one block, and the byte budget is enforced while the bytes are
read — the sizes DECLARED by the client are never consulted.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from src.core import config

_CHUNK_SIZE = 1024 * 256
_HEAD_BYTES = 16


class DossierStorageError(Exception):
    """A path that would escape the dossier root."""


class ByteBudget:
    """The running total of the bytes ACTUALLY received for one dossier. `consume` raises the moment the
    total would exceed the limit; a total equal to the limit is allowed."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def consume(self, n: int) -> None:
        if self.used + n > self.limit:
            raise BudgetExceeded(self.limit)
        self.used += n


class BudgetExceeded(Exception):
    def __init__(self, limit: int):
        super().__init__(f"byte budget of {limit} exceeded")
        self.limit = limit


def _root() -> Path:
    return Path(config.LOCAL_STORAGE_PATH).resolve() / "ao_dossiers"


def dossier_dir(organization_id: uuid.UUID, user_id: uuid.UUID, dossier_id: uuid.UUID) -> Path:
    return _root() / str(organization_id) / str(user_id) / str(dossier_id)


def relative_key(path: Path) -> str:
    return path.resolve().relative_to(Path(config.LOCAL_STORAGE_PATH).resolve()).as_posix()


async def stream_to_file(upload: Any, target: Path, budget: ByteBudget) -> tuple[int, str, bytes]:
    """Copy `upload` to `target` by bounded chunks. Returns (bytes received, sha256 hex, first bytes).
    Raises BudgetExceeded mid-stream (the partial file is removed). Atomic publish: written under a
    temporary name, then renamed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    digest = hashlib.sha256()
    total, head = 0, b""
    try:
        with open(tmp, "wb") as out:
            while True:
                piece = await upload.read(_CHUNK_SIZE)
                if not piece:
                    break
                budget.consume(len(piece))
                total += len(piece)
                if len(head) < _HEAD_BYTES:
                    head = (head + piece)[:_HEAD_BYTES]
                digest.update(piece)
                out.write(piece)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return total, digest.hexdigest(), head


def resolve(storage_key: str) -> Path:
    """Storage key -> file, confined to the dossier root (FileNotFoundError for both "escapes" and "missing")."""
    root = _root()
    candidate = (Path(config.LOCAL_STORAGE_PATH).resolve() / storage_key).resolve()
    if not (candidate == root or root in candidate.parents):
        raise FileNotFoundError("Chemin hors de la racine des dossiers.")
    if not candidate.exists():
        raise FileNotFoundError("Fichier introuvable.")
    return candidate


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(storage_key: str) -> Any:
    return json.loads(resolve(storage_key).read_text(encoding="utf-8"))


def remove_dossier_dir(organization_id: uuid.UUID, user_id: uuid.UUID, dossier_id: uuid.UUID) -> bool:
    """Delete ONE dossier's directory (never anything else: the path is rebuilt from the three ids and
    checked to lie inside the dossier root). True when nothing is left."""
    target = dossier_dir(organization_id, user_id, dossier_id)
    root = _root()
    if root not in target.resolve().parents:
        raise DossierStorageError("Refus de supprimer hors de la racine des dossiers.")
    if not target.exists():
        return True
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()
