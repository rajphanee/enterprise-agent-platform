"""
assistant.py - permission-aware Q&A that would rather decline than guess.

FLOW (see README "Flow 1"):
  1. validate question + role
  2. per-role cache lookup          -> hit? return cached answer
  3. store.search(question, role)   -> restricted docs already filtered out
  4. nothing relevant?              -> DECLINE (no_relevant_documents)
  5. LLM drafts an answer
  6. validate citations:
        - every sentence must cite [doc:ID]
        - every cited ID must be one of the retrieved docs
     fails?                         -> DECLINE (reason says why)
  7. cache (keyed by role!) + audit + return

WHY THE CACHE KEY INCLUDES THE ROLE:
  If HR asks "what are the salary bands?" and we cached by question only,
  the next employee asking the same question would get HR's answer. Leak.
"""

import re

from ..audit import AuditLog
from ..config import Settings
from ..errors import LLMError, ValidationError
from ..llm import INSUFFICIENT, BaseLLM
from .store import KnowledgeStore

KNOWN_ROLES = {"employee", "engineer", "hr", "finance", "admin"}
MAX_QUESTION_CHARS = 1000

CITATION_RE = re.compile(r"\[doc:([A-Za-z0-9_\-]+)\]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def validate_answer(answer: str, retrieved_doc_ids: set) -> tuple:
    """
    Return (ok, reason, cited_ids).
    Kept as a plain function so tests can hit it directly with bad answers.
    """
    text = (answer or "").strip()
    if not text:
        return False, "empty_answer", set()
    if INSUFFICIENT in text:
        return False, "model_reported_insufficient_context", set()

    sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]
    for sentence in sentences:
        if not CITATION_RE.search(sentence):
            return False, "uncited_sentence", set()

    cited = set(CITATION_RE.findall(text))
    unknown = cited - set(retrieved_doc_ids)
    if unknown:
        # The model cited something it was never shown -> hallucinated source.
        return False, f"cited_unretrieved_document:{','.join(sorted(unknown))}", cited
    return True, "ok", cited


class KnowledgeAssistant:
    def __init__(self, store: KnowledgeStore, llm: BaseLLM, audit: AuditLog, settings: Settings):
        self.store = store
        self.llm = llm
        self.audit = audit
        self.settings = settings
        self._cache = {}  # (role, normalized question) -> result dict

    @staticmethod
    def _normalize(question: str) -> str:
        return " ".join(question.lower().split()).rstrip("?!. ")

    def _decline(self, question: str, role: str, user: str, reason: str) -> dict:
        result = {
            "status": "declined",
            "answer": "I can't answer that from the documents you have access to.",
            "citations": [],
            "reason": reason,
            "cached": False,
        }
        self.audit.record(
            "knowledge", "question_declined", user,
            {"role": role, "question": question, "reason": reason},
        )
        return result

    def ask(self, question: str, role: str, user: str = "anonymous") -> dict:
        # ---- 1. input validation (raise = caller's mistake, not a decline)
        if not isinstance(question, str) or not question.strip():
            raise ValidationError("question must be a non-empty string")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationError(f"question is longer than {MAX_QUESTION_CHARS} characters")
        if role not in KNOWN_ROLES:
            raise ValidationError(f"unknown role {role!r}. Valid roles: {sorted(KNOWN_ROLES)}")
        question = question.strip()

        # ---- 2. cache (per role)
        cache_key = (role, self._normalize(question))
        if cache_key in self._cache:
            cached = dict(self._cache[cache_key], cached=True)
            self.audit.record("knowledge", "answer_served_from_cache", user,
                              {"role": role, "question": question})
            return cached

        # ---- 3 + 4. retrieval with permission filter
        results = self.store.search(question, role, k=3)
        if not results or results[0][1] < self.settings.min_retrieval_score:
            return self._decline(question, role, user, "no_relevant_documents")

        chunks = [chunk for chunk, _score in results]
        retrieved_ids = {chunk.doc_id for chunk in chunks}

        # ---- 5. draft answer
        try:
            raw_answer = self.llm.answer_question(question, chunks)
        except LLMError as exc:
            # Fail closed: a broken model means "no answer", never "a guess".
            return self._decline(question, role, user, f"llm_error: {exc}")

        # ---- 6. citation gate
        ok, reason, cited = validate_answer(raw_answer, retrieved_ids)
        if not ok:
            return self._decline(question, role, user, reason)

        # ---- 7. success
        result = {
            "status": "answered",
            "answer": raw_answer,
            "citations": sorted(cited),
            "reason": "ok",
            "cached": False,
        }
        self._cache[cache_key] = result  # only good answers are cached
        self.audit.record(
            "knowledge", "question_answered", user,
            {"role": role, "question": question, "citations": sorted(cited),
             "top_score": results[0][1]},
        )
        return result
