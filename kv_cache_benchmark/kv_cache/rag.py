"""
RAG (Retrieval-Augmented Generation) workload modeling for KV Cache Benchmark.

Simulates document ingestion, chunking, and retrieval patterns that
stress the cache with large context sizes and unique I/O patterns.
"""

import hashlib
import random
import logging
import struct
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime

import numpy as np

# 09-03 (S5): dimensionality of the deterministic per-chunk embeddings
# used by REAL retrieval; matches the eval-family vector layout the
# device arm (filtered_topk_exact, PIND 6) will scan in P3.
RAG_EMBED_DIM = 64

from kv_cache.config import cfg
from kv_cache.models import ModelConfig, InferenceRequest

logger = logging.getLogger(__name__)


@dataclass
class RAGChunk:
    """Represents a single chunk of a document in a RAG system."""
    chunk_id: str
    doc_id: str
    chunk_index: int
    token_count: int
    kv_cache_key: str

    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    storage_tier: str = ""
    size_bytes: int = 0


@dataclass
class RAGDocument:
    """Represents a document that has been chunked for RAG."""
    doc_id: str
    total_tokens: int
    chunk_size: int
    chunks: List[RAGChunk] = field(default_factory=list)
    # 09-03 (S5): (num_chunks x RAG_EMBED_DIM) float32, unit-normalized,
    # deterministic per (doc_id, chunk_idx). None in legacy ('off') mode.
    embeddings: Optional[np.ndarray] = None

    @property
    def num_chunks(self) -> int:
        return len(self.chunks)


@dataclass
class RAGQuery:
    """Represents a RAG query that retrieves document chunks."""
    query_id: str
    query_tokens: int
    retrieved_chunks: List[RAGChunk]
    generation_tokens: int

    @property
    def total_context_tokens(self) -> int:
        """The total context is the user's query plus all retrieved document chunks."""
        return self.query_tokens + sum(c.token_count for c in self.retrieved_chunks)


class RAGDocumentManager:
    """Manages the ingestion and retrieval of RAG document chunks."""

    # Supported retrieval distributions
    DISTRIBUTIONS = ('zipfian', 'uniform', 'random')

    def __init__(self, cache, chunk_size: int = None, top_k_chunks: int = None,
                 mode: str = 'off'):
        # offload-stages s5: 'off' = legacy RNG chooser (continuity cells
        # only), 'host' = real embedding top-k on the host, 'device'
        # lands with P3 (PIND 6 scan; until then it scores host-side).
        self.mode = str(mode or 'off')
        self.cache = cache
        self.chunk_size = chunk_size if chunk_size is not None else cfg('rag', 'chunk_size_tokens', default=512)
        self.top_k_chunks = top_k_chunks if top_k_chunks is not None else cfg('rag', 'top_k_chunks', default=5)
        self.max_documents = cfg('rag', 'max_documents', default=0)  # 0 = unlimited
        self.retrieval_distribution = cfg('rag', 'retrieval_distribution', default='zipfian')
        if self.retrieval_distribution not in self.DISTRIBUTIONS:
            logger.warning(f"Unknown retrieval distribution '{self.retrieval_distribution}', defaulting to 'zipfian'")
            self.retrieval_distribution = 'zipfian'
        self.documents: Dict[str, RAGDocument] = {}
        self.chunk_index: Dict[str, RAGChunk] = {}
        self.lock = threading.Lock()
        self.ingestion_order: List[str] = []  # Track order for LRU eviction

        # Statistics
        self.stats = {
            'documents_ingested': 0,
            'documents_evicted': 0,
            'chunks_created': 0,
            'retrieval_requests': 0,
            'chunks_retrieved': 0,
            # S5 device arm (09-05)
            'rag_device_lookups': 0,
            'rag_device_mismatches': 0,
            'rag_device_ties': 0,
            'rag_device_errors': 0,
        }

    def ingest_document(self, doc_id: str, total_tokens: int, model_config: ModelConfig):
        """
        Simulates the ingestion of a document.
        Splits it into chunks and stores the KV cache for each chunk.
        """
        max_chunk_bytes = cfg('rag', 'max_chunk_bytes', default=256 * 1024**2)
        bytes_per_token = max(model_config.kv_cache_size_per_token, 1)
        max_tokens_per_chunk = max(1, min(self.chunk_size, max_chunk_bytes // bytes_per_token))

        if max_tokens_per_chunk < self.chunk_size:
            logger.debug(f"Adjusting chunk size for {doc_id} to {max_tokens_per_chunk} tokens "
                  f"to stay under {max_chunk_bytes / 1024**2:.0f}MiB per chunk.")

        num_chunks = (total_tokens + max_tokens_per_chunk - 1) // max_tokens_per_chunk

        doc = RAGDocument(
            doc_id=doc_id,
            total_tokens=total_tokens,
            chunk_size=max_tokens_per_chunk,
            chunks=[]
        )

        for chunk_idx in range(num_chunks):
            remaining_tokens = total_tokens - chunk_idx * max_tokens_per_chunk
            chunk_tokens = min(max_tokens_per_chunk, remaining_tokens)

            chunk = RAGChunk(
                chunk_id=f"{doc_id}_chunk_{chunk_idx}",
                doc_id=doc_id,
                chunk_index=chunk_idx,
                token_count=chunk_tokens,
                kv_cache_key=f"rag_{doc_id}_chunk_{chunk_idx}"
            )

            try:
                success, location, write_latency = self.cache.allocate_cache(
                    key=chunk.kv_cache_key,
                    num_tokens=chunk_tokens
                )
            except MemoryError:
                logger.error(f"MemoryError while ingesting chunk {chunk.chunk_id}; skipping remaining chunks.")
                break
            except Exception as exc:
                logger.error(f"Error ingesting chunk {chunk.chunk_id}: {exc}")
                continue

            if not success:
                logger.warning(f"Failed to allocate cache for chunk {chunk.chunk_id}.")
                continue

            chunk.storage_tier = location
            chunk.size_bytes = chunk_tokens * model_config.kv_cache_size_per_token

            doc.chunks.append(chunk)
            self.chunk_index[chunk.chunk_id] = chunk

        if self.mode in ('host', 'device') and doc.chunks:
            doc.embeddings = np.stack(
                [self._chunk_embedding(doc_id, c.chunk_index)
                 for c in doc.chunks])

        with self.lock:
            # Evict oldest documents if we've hit the limit
            if self.max_documents > 0:
                while len(self.documents) >= self.max_documents:
                    self._evict_oldest_document_unlocked()

            self.documents[doc_id] = doc
            self.ingestion_order.append(doc_id)
            self.stats['documents_ingested'] += 1
            self.stats['chunks_created'] += len(doc.chunks)
        return doc

    def _evict_oldest_document_unlocked(self):
        """Evict the oldest document to free cache space. Must be called with lock held."""
        if not self.ingestion_order:
            return

        oldest_doc_id = self.ingestion_order.pop(0)
        if oldest_doc_id not in self.documents:
            return

        doc = self.documents[oldest_doc_id]
        for chunk in doc.chunks:
            try:
                self.cache.delete(chunk.kv_cache_key)
            except Exception as e:
                logger.debug(f"Could not delete cache for chunk {chunk.chunk_id}: {e}")
            if chunk.chunk_id in self.chunk_index:
                del self.chunk_index[chunk.chunk_id]

        del self.documents[oldest_doc_id]
        self.stats['documents_evicted'] += 1
        logger.debug(f"Evicted RAG document {oldest_doc_id} ({doc.num_chunks} chunks)")

    def evict_oldest_document(self):
        """Evict the oldest document to free cache space (thread-safe)."""
        with self.lock:
            self._evict_oldest_document_unlocked()

    def _compute_chunk_probabilities(self, num_chunks: int) -> Optional[List[float]]:
        """
        Compute selection probabilities based on configured distribution.

        Returns:
            List of probabilities, or None for uniform random selection.
        """
        if self.retrieval_distribution in ('uniform', 'random'):
            # Uniform: all chunks equally likely (None tells np.random.choice to use uniform)
            return None
        elif self.retrieval_distribution == 'zipfian':
            # Zipfian: earlier chunks are more likely (1/1, 1/2, 1/3, ...)
            # This models real RAG where document intros/summaries are often most relevant
            probs = [1.0 / (i + 1) for i in range(num_chunks)]
            total = sum(probs)
            return [p / total for p in probs]
        else:
            # Fallback to uniform
            return None

    @staticmethod
    def _chunk_embedding(doc_id: str, chunk_idx: int) -> np.ndarray:
        """Deterministic unit-norm embedding for one chunk (seeded from
        content identity, never from call order)."""
        seed = int.from_bytes(hashlib.sha256(
            f"ragemb_{doc_id}_{chunk_idx}".encode()).digest()[:8], "little")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(RAG_EMBED_DIM).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    @staticmethod
    def query_embedding(query_seed: int) -> np.ndarray:
        """Deterministic unit-norm query vector for a given seed."""
        rng = np.random.default_rng(int(query_seed) & 0xFFFFFFFFFFFFFFFF)
        v = rng.standard_normal(RAG_EMBED_DIM).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    @staticmethod
    def topk_host(embeddings: np.ndarray, query: np.ndarray,
                  k: int) -> List[int]:
        """The S5 HOST ARM: dot-product scores + top-k, deterministic
        tie-break by lower chunk index. Also the ground truth the device
        arm's result-set equality gate compares against (P3)."""
        scores = embeddings @ query
        k = min(k, len(scores))
        # sort by (-score, index): stable, tie-broken by index
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        return order[:k]

    @staticmethod
    def _device_images(doc: 'RAGDocument'):
        """32-byte metadata records (doc_id = vector_index = chunk idx)
        + the contiguous f32 embedding block -- the eval-family layout
        filtered_topk_exact scans. Cached per doc."""
        if getattr(doc, "_dev_images", None) is None:
            recs = [struct.pack("<QIIHHfII", i, 0, 0, 0, 0, 0.0, 0, i)
                    for i in range(doc.embeddings.shape[0])]
            doc._dev_images = (
                b"".join(recs),
                np.ascontiguousarray(doc.embeddings,
                                     dtype=np.float32).tobytes())
        return doc._dev_images

    def _retrieve_device(self, doc: 'RAGDocument', query: np.ndarray,
                         host_idx: List[int]) -> Optional[List[int]]:
        """S5 device arm: ONE filtered_topk_exact execute over the doc's
        staged index. Parity vs the host scorer: a REAL mismatch needs a
        differing id whose score is separated from the k-th score beyond
        float32 noise; anything closer is a tie (counted separately --
        device and numpy accumulate f32 in different orders). Any error
        falls back to the host result, counted."""
        backend = getattr(self.cache, 'backends', {}).get('nvme')
        fn = getattr(backend, 'rag_topk_device', None)
        if fn is None:
            return None
        try:
            meta_img, vec_img = self._device_images(doc)
            k = min(self.top_k_chunks, doc.embeddings.shape[0])
            dev_ids = fn(meta_img, vec_img,
                         np.ascontiguousarray(query,
                                              dtype=np.float32).tobytes(),
                         k, int(doc.embeddings.shape[1]))
            with self.lock:
                self.stats['rag_device_lookups'] += 1
            if set(dev_ids) != set(host_idx):
                scores = doc.embeddings @ query
                kth = float(np.sort(scores)[-k])
                diff = set(dev_ids) ^ set(host_idx)
                real = any(abs(float(scores[i]) - kth) > 1e-5
                           for i in diff if 0 <= i < len(scores))
                with self.lock:
                    if real:
                        self.stats['rag_device_mismatches'] += 1
                    else:
                        self.stats['rag_device_ties'] += 1
            return [int(i) for i in dev_ids
                    if 0 <= int(i) < len(doc.chunks)]
        except Exception as exc:
            logger.warning("rag device retrieval fell back to host: %s",
                           exc)
            with self.lock:
                self.stats['rag_device_errors'] += 1
            return None

    def retrieve_chunks(self, doc_id: str,
                        query_seed: Optional[int] = None) -> List[RAGChunk]:
        """
        Retrieval of the top-k most relevant chunks for a query.

        mode 'off' (legacy, continuity cells only): RNG chooser with the
        configured distribution -- NOT content scoring.
        mode 'host'/'device': REAL retrieval -- deterministic embeddings,
        dot-product scoring, exact top-k ('device' scores host-side until
        the P3 PIND-6 wiring lands).
        """
        with self.lock:
            if doc_id not in self.documents:
                return []
            doc = self.documents[doc_id]
            self.stats['retrieval_requests'] += 1

        if self.mode in ('host', 'device') and doc.embeddings is not None:
            qseed = (query_seed if query_seed is not None
                     else random.getrandbits(32))
            query = self.query_embedding(qseed)
            retrieved_indices = self.topk_host(
                doc.embeddings, query, self.top_k_chunks)
            if self.mode == 'device':
                dev = self._retrieve_device(doc, query, retrieved_indices)
                if dev is not None:
                    retrieved_indices = dev
        else:
            chunk_probabilities = self._compute_chunk_probabilities(len(doc.chunks))
            retrieved_indices = np.random.choice(
                len(doc.chunks),
                size=min(self.top_k_chunks, len(doc.chunks)),
                replace=False,
                p=chunk_probabilities
            )

        retrieved_chunks = [doc.chunks[i] for i in retrieved_indices]

        for chunk in retrieved_chunks:
            chunk.access_count += 1
            chunk.last_accessed = datetime.now()

        with self.lock:
            self.stats['chunks_retrieved'] += len(retrieved_chunks)

        return retrieved_chunks

    def get_stats(self) -> Dict:
        """Returns a copy of the current statistics."""
        with self.lock:
            return dict(self.stats)
