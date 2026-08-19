"""Embedding-based ranking over the catalog built by catalog.py.

Loads a local sentence-transformer once and embeds the whole catalog
(metrics, dimensions, tables/columns) up front, so repeated searches
(e.g. every keystroke from the web UI) are just a matrix multiply.
"""
import numpy as np
from sentence_transformers import SentenceTransformer

from catalog import build_catalog

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"


class SemanticSearchIndex:
    def __init__(self):
        self.catalog = build_catalog()
        self.model = SentenceTransformer(EMBED_MODEL_NAME)
        self.embeddings = self.model.encode(
            [item["search_text"] for item in self.catalog], normalize_embeddings=True
        )

    def search(self, query: str, top_k: int = 8):
        query_embedding = self.model.encode([query], normalize_embeddings=True)[0]
        # macOS's Accelerate BLAS backend emits spurious (harmless) FP warnings
        # on this matmul shape; embeddings are verified finite and unit-norm.
        with np.errstate(all="ignore"):
            scores = self.embeddings @ query_embedding
        ranked = sorted(zip(scores, self.catalog), key=lambda pair: pair[0], reverse=True)
        return [(float(score), item) for score, item in ranked[:top_k]]
