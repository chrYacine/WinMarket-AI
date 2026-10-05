"""Backend-agnostic file storage abstraction.

Today's only implementation (LocalStorageService) mirrors existing
behavior exactly: WinMarket AI's document pipeline (DocumentGenerator,
frozen business logic) already writes PDF/DOCX straight to disk
(data/outputs/) — this module never moves, copies or renames those files.
It exists so `analysis_documents.storage_path` always holds a reference
this service can resolve back into a real file regardless of backend, and
so a future SupabaseStorageService/S3 implementation can be dropped in via
STORAGE_BACKEND=object without any caller changing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from src.core import config


class StorageService(ABC):
    @abstractmethod
    def save(self, local_path: Path, key: str | None = None) -> str:
        """Register a local file with the backend; return its storage_path reference."""

    @abstractmethod
    def get(self, storage_path: str) -> Path:
        """Resolve a storage_path reference back to a local filesystem Path."""

    @abstractmethod
    def delete(self, storage_path: str) -> None:
        ...

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        ...

    @abstractmethod
    def get_download_reference(self, storage_path: str) -> str:
        """A reference suitable for serving a download — a local path today,
        a signed URL once an object-store backend is implemented."""

    @abstractmethod
    def resolve_for_download(self, storage_path: str) -> Path:
        """Resolve a storage_path to a servable file, confined to the
        backend's storage roots. Raises FileNotFoundError if the reference
        escapes those roots or doesn't point at an existing file."""


class LocalStorageService(StorageService):
    """V3 default. storage_path is a `local://<relative-path>` reference
    rooted at LOCAL_STORAGE_PATH (falls back to an absolute path for files
    that live outside that root, e.g. a custom OUTPUT_DIR)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or config.LOCAL_STORAGE_PATH).resolve()

    def _to_relative(self, path: Path) -> str:
        # POSIX-style separators regardless of OS: this reference may be
        # written on Windows in dev and read back on a Linux deployment.
        path = Path(path).resolve()
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _resolve(self, storage_path: str) -> Path:
        if storage_path.startswith("local://"):
            rel = storage_path[len("local://"):]
            candidate = Path(rel)
            return candidate if candidate.is_absolute() else (self.root / rel)
        return Path(storage_path)

    def save(self, local_path: Path, key: str | None = None) -> str:
        # No copy: the file is already on disk (written by DocumentGenerator
        # or the upload handler) — we only mint the backend-agnostic reference.
        return f"local://{self._to_relative(Path(local_path))}"

    def get(self, storage_path: str) -> Path:
        return self._resolve(storage_path)

    def delete(self, storage_path: str) -> None:
        path = self._resolve(storage_path)
        if path.exists():
            path.unlink()

    def exists(self, storage_path: str) -> bool:
        return self._resolve(storage_path).exists()

    def get_download_reference(self, storage_path: str) -> str:
        return str(self._resolve(storage_path))

    def resolve_for_download(self, storage_path: str) -> Path:
        """Resolve a storage_path reference to a file, confined to the
        configured storage roots (LOCAL_STORAGE_PATH, and OUTPUT_DIR since
        DocumentGenerator always writes there — see src/web/jobs.py).

        This is the only path used to serve a download: it never trusts a
        raw filesystem path as-is, and raises FileNotFoundError — never a
        bare path — for both "doesn't exist" and "escapes every known root",
        so callers can map both to the same masked 404.
        """
        candidate = self._resolve(storage_path).resolve()
        allowed_roots = [self.root, Path(config.OUTPUT_DIR).resolve()]
        if not any(candidate == r or r in candidate.parents for r in allowed_roots):
            raise FileNotFoundError(f"Chemin hors de la racine de stockage : {candidate}")
        if not candidate.exists():
            raise FileNotFoundError(f"Fichier introuvable : {candidate}")
        return candidate


def get_storage_service() -> StorageService:
    backend = config.STORAGE_BACKEND
    if backend == "local":
        return LocalStorageService()
    raise NotImplementedError(
        f"STORAGE_BACKEND={backend!r} n'est pas encore implémenté. Seul 'local' "
        f"est disponible dans cette version — l'interface StorageService est "
        f"prête pour un futur SupabaseStorageService/S3 (save/get/delete/exists/"
        f"get_download_reference), sans changement côté appelants."
    )
