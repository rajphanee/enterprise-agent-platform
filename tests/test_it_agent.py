"""IT agent: policy routing, approval queue rules, failure paths."""

import pytest

from agent_platform.errors import LLMError, NotFoundError, PolicyError, ValidationError
from agent_platform.it_agent import policy
from agent_platform.llm import OfflineLLM


def _ticket(text, tid="T-1", requester="alice"):
    return {"id": tid, "requester": requester, "text": text}


# ------------------------------------------------------------------ policy
def test_production_always_escalates_even_if_classifier_says_low_risk():
    d = policy.decide("password_reset", 0.99, "reset the prod db password", min_confidence=0.6)
    assert d.route == policy.ESCALATE and d.rule == "R2_production"


def test_low_confidence_escalates():
    d = policy.decide("password_reset", 0.4, "password thing", min_confidence=0.6)
    assert d.route == policy.ESCALATE and d.rule == "R3_low_confidence"


def test_word_product_does_not_trigger_production_rule():
    d = policy.decide("software_install", 0.9, "install product analytics app", min_confidence=0.6)
    assert d.route == policy.NEEDS_APPROVAL


# ------------------------------------------------------------- agent flows
def test_low_risk_ticket_is_auto_resolved(platform):
    r = platform.it.handle_ticket(_ticket("I forgot my password and my account is locked"))
    assert r["status"] == "resolved" and r["intent"] == "password_reset"


def test_medium_risk_goes_to_queue_and_requester_cannot_self_approve(platform):
    r = platform.it.handle_ticket(_ticket("Need access to the finance repo", requester="dave"))
    assert r["status"] == "pending_approval"
    with pytest.raises(PolicyError, match="separation of duties"):
        platform.it.approve(r["approval_id"], "Dave")  # case-insensitive match
    done = platform.it.approve(r["approval_id"], "manager")
    assert done["status"] == "executed"
    with pytest.raises(PolicyError):  # cannot approve twice
        platform.it.approve(r["approval_id"], "manager")


def test_reject_needs_reason_and_unknown_id_fails(platform):
    r = platform.it.handle_ticket(_ticket("Please install the Slack app", requester="carol"))
    with pytest.raises(ValidationError):
        platform.it.reject(r["approval_id"], "manager", "  ")
    assert platform.it.reject(r["approval_id"], "manager", "not licensed")["status"] == "rejected"
    with pytest.raises(NotFoundError):
        platform.it.approve("APR-9999", "manager")


def test_automation_failure_escalates(platform):
    r = platform.it.handle_ticket(_ticket("password locked simulate-failure"))
    assert r["status"] == "escalated" and "automation failed" in r["reason"]


def test_llm_outage_escalates_to_human(platform):
    class BrokenLLM(OfflineLLM):
        def classify_ticket(self, text, intents):
            raise LLMError("503")
    platform.it.llm = BrokenLLM()
    r = platform.it.handle_ticket(_ticket("I forgot my password"))
    assert r["status"] == "escalated" and r["intent"] == "unknown"


@pytest.mark.parametrize("bad", [None, "text", {}, {"id": "T", "requester": "a"}, {"id": "T", "requester": "a", "text": 5}])
def test_bad_tickets_raise_validation_error(platform, bad):
    with pytest.raises(ValidationError):
        platform.it.handle_ticket(bad)
