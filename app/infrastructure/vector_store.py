"""Vector store ports for C2 hybrid retrieval.

Two backends behind one small interface:
- NumpyVectorStore: zero-dependency brute-force cosine over a persisted
  index (default; sub-millisecond at current 300-doc corpus scale).
- ChromaVectorStore: real Chroma persistent collection (optional extra;
  selected with KB_VECTOR_BACKEND=chroma).

Embeddings come from an OpenAI-compatible /embeddings endpoint
(SiliconFlow Qwen3-Embedding-8B by default). Nothing here ever raises
into the retrieval path — callers degrade to keyword-only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx


class EmbeddingError(RuntimeError):
    pass


# --- embedding clients -----------------------------------------------------------


class SiliconFlowEmbedding:
    """Batch embedding via an OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        batch_size: int = 32,
        timeout: float = 60.0,
        retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.base_url = (base_url or os.environ.get("SILICONFLOW_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("SILICONFLOW_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
        self.batch_size = batch_size
        self.timeout = timeout
        self.retries = retries

    @property
    def model_name(self) -> str:
        return self.model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key or not self.base_url:
            raise EmbeddingError("embedding credentials not configured")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            last: Exception | None = None
            for attempt in range(self.retries):
                try:
                    resp = httpx.post(
                        f"{self.base_url}/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={"model": self.model, "input": chunk},
                        timeout=self.timeout,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    vectors.extend(item["embedding"] for item in data["data"])
                    last = None
                    break
                except Exception as exc:  # noqa: BLE001 - retry then surface
                    last = exc
                    time.sleep(1.5 * (attempt + 1))
            if last is not None:
                raise EmbeddingError(f"embedding call failed: {last!r}") from last
        return vectors


# --- stores ------------------------------------------------------------------------


class NumpyVectorStore:
    """Brute-force cosine index persisted as JSON meta + nested float lists.

    At the current corpus scale (hundreds of docs) a flat scan is faster
    than any ANN setup cost and has zero dependencies beyond numpy.
    """

    META_FILE = "vector_meta.json"
    VEC_FILE = "vectors.json"

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._meta: list[dict] = []
        self._vectors: list[list[float]] = []

    def build(self, docs: list[dict], vectors: list[list[float]]) -> None:
        assert len(docs) == len(vectors), "doc/vector count mismatch"
        self._meta = [dict(d) for d in docs]
        self._vectors = [list(v) for v in vectors]

    def save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / self.META_FILE).write_text(
            json.dumps(self._meta, ensure_ascii=False), encoding="utf-8"
        )
        (self._dir / self.VEC_FILE).write_text(json.dumps(self._vectors), encoding="utf-8")

    def load(self) -> bool:
        meta_p = self._dir / self.META_FILE
        vec_p = self._dir / self.VEC_FILE
        if not meta_p.exists() or not vec_p.exists():
            return False
        self._meta = json.loads(meta_p.read_text(encoding="utf-8"))
        self._vectors = json.loads(vec_p.read_text(encoding="utf-8"))
        return bool(self._meta)

    @property
    def size(self) -> int:
        return len(self._meta)

    def search(self, query_vec: list[float], top_k: int = 8) -> list[tuple[str, float]]:
        if not self._vectors:
            return []
        q_norm = _norm(query_vec)
        scored: list[tuple[str, float]] = []
        for meta, vec in zip(self._meta, self._vectors):
            v_norm = _norm(vec)
            dot = sum(a * b for a, b in zip(query_vec, vec))
            score = dot / (q_norm * v_norm)
            scored.append((str(meta["doc_id"]), score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def _norm(v: list[float]) -> float:
    return sum(x * x for x in v) ** 0.5 or 1.0


class ChromaVectorStore:
    """Chroma persistent collection (optional dependency)."""

    def __init__(self, directory: str | Path, collection: str = "kb_zh") -> None:
        import chromadb  # guarded: optional extra `vector`

        self._client = chromadb.PersistentClient(path=str(directory))
        self._col = self._client.get_or_create_collection(collection)

    @property
    def size(self) -> int:
        return max(0, int(self._col.count()))

    def build(self, docs: list[dict], vectors: list[list[float]]) -> None:
        self._col.upsert(
            ids=[str(d["doc_id"]) for d in docs],
            embeddings=vectors,
            documents=[d.get("text", "") for d in docs],
            metadatas=[
                {k: str(d.get(k, "")) for k in ("title", "category", "source_type")}
                for d in docs
            ],
        )

    def save(self) -> None:  # persistence is automatic in Chroma
        return None

    def load(self) -> bool:
        return self.size > 0

    def search(self, query_vec: list[float], top_k: int = 8) -> list[tuple[str, float]]:
        if self.size == 0:
            return []
        res = self._col.query(query_embeddings=[query_vec], n_results=min(top_k, self.size))
        return list(zip(res["ids"][0], (1.0 - d for d in res["distances"][0])))


# --- reranker -----------------------------------------------------------------------


class SiliconFlowReranker:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.base_url = (base_url or os.environ.get("SILICONFLOW_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("SILICONFLOW_RERANK_MODEL", "Qwen/Qwen3-Reranker-8B")

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if not self.api_key or not self.base_url:
            raise EmbeddingError("rerank credentials not configured")
        resp = httpx.post(
            f"{self.base_url}/rerank",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "query": query, "documents": documents},
            timeout=60,
        )
        resp.raise_for_status()
        results = sorted(resp.json()["results"], key=lambda r: r["index"])
        return [float(r["relevance_score"]) for r in results]
