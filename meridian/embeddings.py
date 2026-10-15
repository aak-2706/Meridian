from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from functools import lru_cache

from .config import settings


class EmbeddingClient:
    """Local Sentence Transformers or OpenAI-compatible embeddings with explicit index identity."""

    def __init__(self):
        self.base_url = settings.embedding_base_url
        self.api_key = settings.embedding_api_key
        self.provider = settings.embedding_provider
        self.model = settings.embedding_model

    def _provider(self) -> str:
        if self.provider == "auto":
            return "api" if self.base_url and self.model else "hash"
        if self.provider in {"sentence-transformers", "local"}:
            return "sentence-transformers"
        if self.provider in {"api", "openai-compatible"}:
            return "api"
        if self.provider == "hash":
            return "hash"
        raise RuntimeError(f"Unsupported EMBEDDING_PROVIDER: {self.provider}")

    @property
    def configured(self) -> bool:
        provider = self._provider()
        if provider == "api":
            return bool(self.base_url and self.model)
        return provider == "sentence-transformers"

    @property
    def identity(self) -> str:
        provider = self._provider()
        if provider == "api":
            return f"{self.base_url}::{self.model}"
        if provider == "sentence-transformers":
            return f"sentence-transformers::{self.model}"
        return "local-feature-hash-384"

    @staticmethod
    @lru_cache(maxsize=2)
    def _local_model(model_name: str, cache_dir: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Local semantic embeddings require sentence-transformers. "
                "Install project dependencies with `python -m pip install -r requirements.txt`."
            ) from exc
        from pathlib import Path
        cache_path = Path(cache_dir)
        snapshots = list(cache_path.glob(f"*{model_name.replace('/', '--')}*/**/snapshots/*")) if cache_path.exists() else []
        if not snapshots and cache_path.exists():
            snapshots = list(cache_path.glob(f"*{model_name.replace('/', '--')}*"))
        if snapshots:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            target = str(snapshots[0].resolve())
            try:
                return SentenceTransformer(target, device="cpu", local_files_only=True)
            except Exception:
                pass
        kwargs = {"cache_folder": cache_dir, "device": "cpu"}
        return SentenceTransformer(model_name, **kwargs)

    @classmethod
    @lru_cache(maxsize=128)
    def _cached_embed_query(cls, query: str, identity: str) -> list[float]:
        client = cls()
        results = client.embed([query])
        return results[0] if results else []

    def embed_query(self, query: str) -> list[float]:
        """Return cached embedding vector for a single search query."""
        if not query:
            return []
        return self._cached_embed_query(query, self.identity)

    def warmup(self) -> None:
        """Preload local weights or test connectivity so first query is instantaneous."""
        if self._provider() == "sentence-transformers":
            self.embed_query("meridian financial equity research warmup")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        provider = self._provider()
        if provider == "hash":
            from .store import _vector
            return [_vector(text) for text in texts]
        if provider == "sentence-transformers":
            model = self._local_model(self.model, str(settings.embedding_cache_dir))
            vectors = model.encode(texts, batch_size=64, convert_to_numpy=True, normalize_embeddings=True,
                                   show_progress_bar=False)
            return [[float(value) for value in vector] for vector in vectors]
        endpoint = self.base_url if self.base_url.endswith("/embeddings") else self.base_url + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Embedding provider request failed: {exc}") from exc
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("Embedding provider returned an unexpected number of vectors")
        data.sort(key=lambda row: int(row.get("index", 0)))
        vectors = [row.get("embedding") for row in data]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise RuntimeError("Embedding provider returned an empty or invalid vector")
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise RuntimeError("Embedding provider returned vectors with inconsistent dimensions")
        output = []
        for vector in vectors:
            values = [float(value) for value in vector]
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            output.append([value / norm for value in values])
        return output
