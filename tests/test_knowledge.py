"""Knowledge assistant: permissions, citations, declines, per-role cache."""

import pytest

from agent_platform.errors import LLMError, ValidationError
from agent_platform.knowledge.assistant import validate_answer
from agent_platform.llm import OfflineLLM


def test_employee_gets_cited_answer(platform):
    r = platform.knowledge.ask("How many PTO days do I get per year?", role="employee")
    assert r["status"] == "answered"
    assert r["citations"] == ["pto-policy"]
    assert "20 PTO days" in r["answer"]


def test_restricted_doc_is_never_retrieved_for_wrong_role(platform):
    # ACL filter runs inside search, so salary-bands is invisible to employees.
    results = platform.store.search("salary bands senior engineer", role="employee")
    assert all(chunk.doc_id != "salary-bands" for chunk, _ in results)

    r = platform.knowledge.ask("What are the salary bands for Senior Engineer?", role="employee")
    assert r["status"] == "declined"
    assert "170,000" not in r["answer"]


def test_hr_can_see_salary_bands(platform):
    r = platform.knowledge.ask("What are the salary bands for Senior Engineer?", role="hr")
    assert r["status"] == "answered"
    assert "salary-bands" in r["citations"]


def test_cache_is_separate_per_role(platform):
    hr = platform.knowledge.ask("What are the salary bands for Senior Engineer?", role="hr")
    assert hr["status"] == "answered"
    # Same question, different role: must NOT come from HR's cache entry.
    emp = platform.knowledge.ask("What are the salary bands for Senior Engineer?", role="employee")
    assert emp["status"] == "declined" and emp["cached"] is False
    again = platform.knowledge.ask("what are the salary bands for senior engineer", role="hr")
    assert again["cached"] is True


def test_unknown_role_and_empty_question_raise(platform):
    with pytest.raises(ValidationError):
        platform.knowledge.ask("anything", role="ceo")
    with pytest.raises(ValidationError):
        platform.knowledge.ask("   ", role="employee")


@pytest.mark.parametrize("answer,reason", [
    ("", "empty_answer"),
    ("Employees get 20 days.", "uncited_sentence"),
    ("Employees get 20 days [doc:pto-policy]. Also free lunch.", "uncited_sentence"),
    ("Bands are high [doc:salary-bands].", "cited_unretrieved_document:salary-bands"),
    ("INSUFFICIENT_CONTEXT", "model_reported_insufficient_context"),
])
def test_citation_gate_rejects_bad_answers(answer, reason):
    ok, why, _ = validate_answer(answer, {"pto-policy"})
    assert not ok and why == reason


def test_hallucinated_citation_from_model_becomes_decline(platform):
    class LyingLLM(OfflineLLM):
        def answer_question(self, question, chunks):
            return "Everyone gets 50 days [doc:made-up-doc]."
    platform.knowledge.llm = LyingLLM()
    r = platform.knowledge.ask("How many PTO days do I get per year?", role="employee")
    assert r["status"] == "declined"
    assert r["reason"].startswith("cited_unretrieved_document")


def test_llm_outage_declines_instead_of_crashing(platform):
    class BrokenLLM(OfflineLLM):
        def answer_question(self, question, chunks):
            raise LLMError("timeout")
    platform.knowledge.llm = BrokenLLM()
    r = platform.knowledge.ask("How many PTO days do I get per year?", role="employee")
    assert r["status"] == "declined" and r["reason"].startswith("llm_error")
