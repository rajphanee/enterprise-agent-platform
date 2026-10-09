"""
store.py - a tiny in-memory vector store with access-control tags.

Each document is split into chunks. Every chunk carries the set of roles
allowed to see it (copied from its document). search() drops chunks the role
cannot see BEFORE scoring, so a restricted chunk can never influence ranking,
scores, or the answer.

WHY NOT CHROMADB HERE?
  The first version used ChromaDB with a `where={"role": ...}` filter, which
  works the same way. We switched the default to plain TF-IDF so the project
  runs with zero downloads. See README "Swapping in a real vector DB".
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import StorageError, ValidationError

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be",
    "do", "does", "i", "my", "we", "our", "you", "your", "it", "what", "how", "when",
    "who", "can", "with", "at", "by", "from", "this", "that", "much", "get", "need",
    "there", "any", "about", "as", "if", "after", "before", "every", "per",
}


def tokenize(text: str) -> list:
    """lowercase -> words -> drop stopwords -> very light plural stripping."""
    tokens = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]  # "days" -> "day", "bands" -> "band"
        tokens.append(word)
    return tokens


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    text: str
    allowed_roles: frozenset
    terms: Counter = field(default_factory=Counter)


class KnowledgeStore:
    REQUIRED_FIELDS = ("id", "title", "text", "allowed_roles")

    def __init__(self):
        self.chunks = []
        self.doc_ids = set()

    # ------------------------------------------------------------ loading
    def load_file(self, path: Path) -> int:
        """Load documents from a JSON list. Returns how many docs were added."""
        path = Path(path)
        if not path.exists():
            raise StorageError(f"documents file not found: {path}")
        try:
            docs = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"{path} is not valid JSON: {exc}")
        if not isinstance(docs, list):
            raise StorageError(f"{path} must contain a JSON list of documents")
        for doc in docs:
            self.add_document(doc)
        return len(docs)

    def add_document(self, doc: dict) -> None:
        if not isinstance(doc, dict):
            raise ValidationError("each document must be a JSON object")
        missing = [f for f in self.REQUIRED_FIELDS if not doc.get(f)]
        if missing:
            raise ValidationError(f"document {doc.get('id', '?')} is missing {missing}")
        if doc["id"] in self.doc_ids:
            raise ValidationError(f"duplicate document id {doc['id']!r}")
        roles = doc["allowed_roles"]
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise ValidationError(f"document {doc['id']}: allowed_roles must be a list of strings")

        # Chunking rule: one chunk per paragraph (blank line separated).
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", doc["text"]) if p.strip()]
        for number, paragraph in enumerate(paragraphs, start=1):
            chunk = Chunk(
                chunk_id=f"{doc['id']}#{number}",
                doc_id=doc["id"],
                title=doc["title"],
                text=paragraph,
                allowed_roles=frozenset(roles),
                terms=Counter(tokenize(doc["title"] + " " + paragraph)),
            )
            self.chunks.append(chunk)
        self.doc_ids.add(doc["id"])

    # ------------------------------------------------------------ search
    @staticmethod
    def _vector(terms: Counter, doc_freq: Counter, total: int) -> dict:
        """TF-IDF weights. idf = log((N+1)/(df+1)) + 1  (the usual smoothed form)."""
        return {
            t: count * (math.log((total + 1) / (doc_freq.get(t, 0) + 1)) + 1.0)
            for t, count in terms.items()
        }

    @staticmethod
    def _cosine(a: dict, b: dict) -> float:
        dot = sum(weight * b.get(term, 0.0) for term, weight in a.items())
        norm_a = math.sqrt(sum(w * w for w in a.values()))
        norm_b = math.sqrt(sum(w * w for w in b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def search(self, query: str, role: str, k: int = 3) -> list:
        """Return up to k (chunk, score) pairs the ROLE is allowed to see, best first."""
        # STEP 1 - permission filter. This happens before any scoring on purpose.
        visible = [c for c in self.chunks if role in c.allowed_roles]

        query_terms = Counter(tokenize(query))
        if not visible or not query_terms:
            return []

        # STEP 2 - score only what the role can see. Even the IDF statistics are
        # computed from visible chunks, so hidden documents cannot change the
        # scores a user sees (that would be a small side-channel leak).
        doc_freq = Counter()
        for chunk in visible:
            doc_freq.update(set(chunk.terms))
        total = len(visible)

        query_vec = self._vector(query_terms, doc_freq, total)
        scored = []
        for chunk in visible:
            score = self._cosine(query_vec, self._vector(chunk.terms, doc_freq, total))
            if score > 0:
                scored.append((chunk, round(score, 4)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]
