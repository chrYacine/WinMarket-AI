"""Lot 51 — local embedding adapter: fastembed/ONNX, deliberately no torch.

Chosen for this project's modest, CPU-only Windows dev machine (already
sensitive to new large native binaries — see the lot 50 ter Smart App
Control incident): sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
served through fastembed's ONNX conversion (qdrant/paraphrase-multilingual-
MiniLM-L12-v2-onnx-Q), Apache-2.0, ~0.22 Go, 384 dimensions, no query/
passage prefix asymmetry to get wrong (unlike the e5 family), real
multilingual support (~50 languages including French) confirmed by a live
probe during this lot (see docs/qa/lot_51_.../RAPPORT.md §1). onnxruntime
is a NEW native binary on this machine — it worked at qualification time,
never claimed durably immune to the same Smart App Control class of issue
already documented for scipy/scikit-learn.

Never imported by src/web/database/models.py or any SQLite-only code path
— only src/rag/hybrid_index.py and src/rag/hybrid_search.py ever call this,
and only when the PostgreSQL+pgvector structural gate
(src/rag/hybrid_search.dialect_supports_vectors) and
config.RAG_HYBRID_MODE_ENABLED both hold.

Lot 51 bis — real effective token limit, verified rather than assumed. The
Sentence-Transformers model card advertises `max_seq_length=128`; that page
describes the SentenceTransformer wrapper, not proof of what THIS ONNX
conversion's tokenizer actually enforces. Verified empirically (live probe,
this lot): `tokenizer_config.json` for the cached
`qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q` snapshot carries BOTH
`model_max_length=512` and `max_length=128`; fastembed's own loader
(`fastembed.common.preprocessor_utils._resolve_max_context`) takes
`min(...)` of the two — 128 — and calls `tokenizer.enable_truncation(128)`
on the SAME tokenizer object used for every real `embed()` call. A 1052-
character French text was empirically confirmed to truncate to exactly 128
tokens, with a marker placed after character ~1000 completely absent from
the decoded truncated tokens, and `cosine(full_text, first_1000_chars) ==
1.0` — the tail contributes NOTHING to the resulting vector. This is why
`src/rag/chunking.py` now windows by REAL tokens (via
`EmbeddingAdapter.content_token_offsets`), never by a character count that
could silently exceed this limit.
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from src.core import config


class EmbeddingUnavailableError(Exception):
    """A real, safe-to-surface failure — library missing, provider
    exception, dimension mismatch, or a non-finite value. Callers (the
    indexing/search paths) must treat this as a controlled degradation to
    lexical-only, never crash the whole request and never guess a
    replacement vector."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str
    model_revision: str
    dimension: int


def current_embedding_config() -> EmbeddingConfig:
    """The config every freshly-computed passage vector is stamped with.
    Comparing a stored KnowledgeDocumentVersion's
    embedding_model_id/_revision/_dimension against THIS is how
    src/rag/hybrid_index.py detects a version has gone stale (lot 51
    ticket: "pas de mélange d'espaces vectoriels").

    `model_revision` reflects what is ACTUALLY loaded in THIS process, not
    merely the configured label, whenever the model has already been
    loaded here (see EmbeddingAdapter._resolved_revision_suffix) — lot 51
    bis closed a real gap: EMBEDDING_MODEL_REVISION previously never
    changed even if fastembed/the HF snapshot it resolves to changed
    underneath it, so two genuinely different artifacts could share the
    same declared "revision" and never trigger a reindex. Before any real
    load in this process (a pure staleness check with no model loaded
    yet), this honestly falls back to the plain configured label — no
    network call or model load is forced just to answer a state query."""
    adapter = EmbeddingAdapter._instance
    suffix = adapter._resolved_revision_suffix if adapter is not None else None
    revision = config.EMBEDDING_MODEL_REVISION if suffix is None else f"{config.EMBEDDING_MODEL_REVISION}+{suffix}"
    return EmbeddingConfig(
        model_id=config.EMBEDDING_MODEL_ID,
        model_revision=revision,
        dimension=config.EMBEDDING_DIMENSION,
    )


class EmbeddingAdapter:
    """One real fastembed model per process, loaded lazily on the FIRST
    call to embed() — never at import time or application startup (ticket:
    "aucun téléchargement automatique au démarrage de l'application"). A
    model load/first-download takes several seconds and holds the model in
    memory; `shared()` returns the same instance to every caller in this
    process so it is never reloaded per request or per search."""

    _class_lock = threading.Lock()
    _instance: "EmbeddingAdapter | None" = None

    def __init__(self) -> None:
        self._model = None
        self._model_lock = threading.Lock()
        # Lot 51 bis — populated only once the model is actually loaded (see current_embedding_config).
        self._resolved_revision_suffix: str | None = None
        # A SEPARATE tokenizer instance, truncation explicitly disabled, used ONLY to compute
        # real, full token offsets for windowing (src/rag/chunking.py) — never the shared,
        # truncation-enabled tokenizer fastembed itself uses for embed(), so windowing never
        # mutates state a concurrent embed() call also depends on.
        self._content_tokenizer = None
        self._num_special_tokens: int | None = None
        self._effective_max_tokens: int | None = None

    @classmethod
    def shared(cls) -> "EmbeddingAdapter":
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            try:
                from fastembed import TextEmbedding
            except Exception as exc:  # pragma: no cover - environment-dependent
                raise EmbeddingUnavailableError(f"fastembed_not_installed: {exc}") from exc
            try:
                from src.rag.model_artifact import verified_artifact
                artifact = verified_artifact(Path(config.EMBEDDING_CACHE_DIR))
                model = TextEmbedding(
                    model_name=config.EMBEDDING_MODEL_ID,
                    cache_dir=config.EMBEDDING_CACHE_DIR,
                    specific_model_path=str(artifact),
                    local_files_only=True,
                )
            except Exception as exc:
                raise EmbeddingUnavailableError(f"model_load_failed: {exc}") from exc

            try:
                self._prepare_tokenizers(model)
            except Exception as exc:
                raise EmbeddingUnavailableError(f"tokenizer_introspection_failed: {exc}") from exc
            # Assigned LAST: any exception above must never leave a half-initialized adapter
            # looking "loaded" to a concurrent caller.
            self._model = model

    def _prepare_tokenizers(self, model) -> None:
        """Reads the REAL effective truncation limit off the shared embedding
        tokenizer (whatever fastembed itself configured — never
        reimplemented/guessed, so this can never drift from what embed()
        actually does), builds a separate untruncated tokenizer for offset
        computation, and derives a revision suffix from the concretely
        resolved model snapshot directory (lot 51 bis: EMBEDDING_MODEL_
        REVISION must reflect the loaded artifact, not just a hand-typed
        label)."""
        from tokenizers import Tokenizer

        shared_tokenizer = model.model.tokenizer
        truncation = shared_tokenizer.truncation
        if not truncation or "max_length" not in truncation:
            raise ValueError("the loaded model's tokenizer has no truncation configured — cannot determine a safe window size")
        self._effective_max_tokens = int(truncation["max_length"])

        model_dir = Path(model.model._model_dir)
        content_tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        # tokenizer.json itself SERIALIZES truncation parameters (confirmed empirically — a
        # fresh from_file() already truncates at the same limit) — must disable explicitly.
        content_tokenizer.no_truncation()
        self._content_tokenizer = content_tokenizer
        self._num_special_tokens = len(content_tokenizer.encode("", add_special_tokens=True).ids)

        # The last path component of a HuggingFace cache snapshot dir is the resolved commit
        # hash — the actual artifact identity, independent of any label we typed ourselves.
        self._resolved_revision_suffix = model_dir.name or None

    def max_content_tokens_per_window(self) -> int:
        """The number of CONTENT tokens (excluding the special tokens the
        tokenizer always adds) that fit in one window without fastembed's
        own truncation silently dropping anything — verified, not assumed,
        against the actually-loaded tokenizer (see module docstring)."""
        self._ensure_loaded()
        budget = self._effective_max_tokens - self._num_special_tokens
        if budget <= 0:
            raise EmbeddingUnavailableError(f"non_positive_token_budget: max={self._effective_max_tokens}, special={self._num_special_tokens}")
        return budget

    def content_token_offsets(self, text: str) -> list[tuple[int, int]]:
        """Every content token's exact (start_char, end_char) span in `text`,
        in order, WITHOUT truncation — the real basis for windowing
        (src/rag/chunking.py::window_passages_by_tokens), never a character
        count that could silently exceed what the model actually sees."""
        self._ensure_loaded()
        if not text:
            return []
        encoding = self._content_tokenizer.encode(text, add_special_tokens=False)
        return list(encoding.offsets)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Real embeddings, finite, of EXACTLY config.EMBEDDING_DIMENSION —
        never silently padded/truncated/renormalized. Raises
        EmbeddingUnavailableError on ANY failure; the caller decides the
        degraded behavior (this function never guesses one)."""
        if not texts:
            return []
        self._ensure_loaded()
        try:
            raw = list(self._model.embed(texts))
        except Exception as exc:
            raise EmbeddingUnavailableError(f"embed_call_failed: {exc}") from exc

        expected_dim = config.EMBEDDING_DIMENSION
        vectors: list[list[float]] = []
        for vec in raw:
            values = [float(x) for x in vec]
            if len(values) != expected_dim:
                raise EmbeddingUnavailableError(f"dimension_mismatch: got {len(values)}, expected {expected_dim}")
            if not all(math.isfinite(v) for v in values):
                raise EmbeddingUnavailableError("non_finite_vector")
            vectors.append(values)
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
