"""Deterministic synthetic corpus and query generation for RecallOrigin.

The corpus is synthetic by design: it can be generated offline, contains no
private data, and makes the relevant document for every query unambiguous.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass

_ADJECTIVES = (
    "amber",
    "brisk",
    "cobalt",
    "distant",
    "even",
    "frozen",
    "gentle",
    "hidden",
    "indigo",
    "lunar",
    "mellow",
    "narrow",
    "quiet",
    "rapid",
    "silver",
    "tidal",
)
_NOUNS = (
    "atlas",
    "badger",
    "cedar",
    "delta",
    "ember",
    "falcon",
    "garden",
    "harbor",
    "island",
    "junction",
    "kernel",
    "ledger",
    "meadow",
    "notebook",
    "orchard",
    "quartz",
)
_VERBS = (
    "archives",
    "builds",
    "checks",
    "describes",
    "explains",
    "indexes",
    "measures",
    "records",
)


@dataclass(frozen=True, slots=True)
class BenchmarkDocument:
    """One deterministic memory submitted through the public API."""

    doc_id: str
    content: str
    memory_key: str
    partition: str = "workspace:benchmark"


@dataclass(frozen=True, slots=True)
class BenchmarkQuery:
    """One query with explicit relevance labels."""

    query_id: str
    text: str
    relevant_doc_ids: tuple[str, ...]
    query_type: str
    partition: str = "workspace:benchmark"


def document_at(index: int, *, seed: int) -> BenchmarkDocument:
    """Generate a document directly, independent of iteration order."""
    if index < 0:
        msg = "index must be non-negative"
        raise ValueError(msg)
    digest = hashlib.sha256(f"recall-origin:{seed}:{index}".encode()).digest()
    adjective = _ADJECTIVES[digest[0] % len(_ADJECTIVES)]
    noun = _NOUNS[digest[1] % len(_NOUNS)]
    verb = _VERBS[digest[2] % len(_VERBS)]
    marker = f"originmarker{index:09d}"
    doc_id = f"doc-{index:09d}"
    content = (
        f"[doc:{doc_id}] The {adjective} {noun} {verb} synthetic benchmark "
        f"record {marker} for deterministic retrieval."
    )
    return BenchmarkDocument(
        doc_id=doc_id,
        content=content,
        memory_key=f"benchmark.document.{index:09d}",
    )


def iter_documents(*, scale: int, seed: int) -> Iterator[BenchmarkDocument]:
    """Yield ``scale`` documents without retaining the corpus in memory."""
    if scale < 1:
        msg = "scale must be positive"
        raise ValueError(msg)
    for index in range(scale):
        yield document_at(index, seed=seed)


def query_indices(*, scale: int, count: int, seed: int) -> tuple[int, ...]:
    """Select deterministic, distinct query targets without runtime RNG behavior.

    A seed-derived start and a step coprime to ``scale`` define a full-cycle
    permutation. This keeps selection stable across Python versions while
    avoiding an allocation proportional to the corpus size.
    """
    if scale < 1:
        msg = "scale must be positive"
        raise ValueError(msg)
    if count < 1:
        msg = "query count must be positive"
        raise ValueError(msg)
    bounded = min(scale, count)
    if scale == 1:
        return (0,)

    digest = hashlib.sha256(f"recall-origin-query-selection-v1:{seed}:{scale}".encode()).digest()
    start = int.from_bytes(digest[:8], byteorder="big") % scale
    step = int.from_bytes(digest[8:16], byteorder="big") % scale or 1
    while math.gcd(step, scale) != 1:
        step = (step + 1) % scale or 1
    return tuple(sorted((start + ordinal * step) % scale for ordinal in range(bounded)))


def generate_queries(*, scale: int, count: int, seed: int) -> tuple[BenchmarkQuery, ...]:
    """Create a stable mix of exact-content and lexical-marker queries."""
    queries: list[BenchmarkQuery] = []
    for ordinal, index in enumerate(query_indices(scale=scale, count=count, seed=seed)):
        document = document_at(index, seed=seed)
        query_type = "exact" if ordinal % 3 == 0 else "lexical"
        query_text = document.content if query_type == "exact" else f"originmarker{index:09d}"
        queries.append(
            BenchmarkQuery(
                query_id=f"query-{ordinal:05d}",
                text=query_text,
                relevant_doc_ids=(document.doc_id,),
                query_type=query_type,
            )
        )
    return tuple(queries)


def _canonical_document(document: BenchmarkDocument) -> bytes:
    return (
        json.dumps(
            asdict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def corpus_sha256(*, scale: int, seed: int) -> str:
    """Hash the canonical streamed corpus for reproducibility checks."""
    digest = hashlib.sha256()
    for document in iter_documents(scale=scale, seed=seed):
        digest.update(_canonical_document(document))
    return digest.hexdigest()


def hash_documents(documents: Sequence[BenchmarkDocument]) -> str:
    """Hash an already materialized corpus with the same canonical format."""
    digest = hashlib.sha256()
    for document in documents:
        digest.update(_canonical_document(document))
    return digest.hexdigest()
