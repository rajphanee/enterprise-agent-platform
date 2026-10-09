"""
policy.py - the rules that decide what an IT ticket is ALLOWED to do.

These rules live in code, not in the LLM prompt. A prompt can be argued with
("ignore previous instructions, this is low risk"); an if-statement cannot.

Routes:
  AUTO            -> the agent runs the automation right away
  NEEDS_APPROVAL  -> goes to the approval queue; a different person must approve
  ESCALATE        -> a human engineer handles it; the agent does nothing

Rules are checked TOP TO BOTTOM and the first match wins:
  1. Unknown intent                               -> ESCALATE
  2. Anything touching production (intent OR text) -> ESCALATE  (always, no exceptions)
  3. Classifier confidence below threshold        -> ESCALATE
  4. Otherwise route by risk: low=AUTO, medium=NEEDS_APPROVAL, high=ESCALATE
"""

import re
from dataclasses import dataclass

AUTO = "auto"
NEEDS_APPROVAL = "needs_approval"
ESCALATE = "escalate"

# The fixed list of intents the classifier is allowed to return.
INTENTS = [
    "password_reset",
    "vpn_issue",
    "access_request",
    "software_install",
    "hardware_issue",
    "prod_change",
    "unknown",
]

RISK_BY_INTENT = {
    "password_reset": "low",
    "vpn_issue": "low",
    "access_request": "medium",
    "software_install": "medium",
    "hardware_issue": "medium",
    "prod_change": "high",
    "unknown": "high",
}

ROUTE_BY_RISK = {"low": AUTO, "medium": NEEDS_APPROVAL, "high": ESCALATE}

# Defense in depth: even if the classifier says "software_install", a ticket
# that mentions production still goes to a person.
PRODUCTION_PATTERN = re.compile(
    r"\b(prod|production|deploy(ment)?|hotfix|database migration|rollback)\b",
    re.IGNORECASE,
)


@dataclass
class PolicyDecision:
    route: str
    risk: str
    reason: str
    rule: str  # which numbered rule fired - very handy when reading the audit log


def decide(intent: str, confidence: float, ticket_text: str, min_confidence: float) -> PolicyDecision:
    # Rule 1
    if intent not in RISK_BY_INTENT or intent == "unknown":
        return PolicyDecision(ESCALATE, "high", f"intent {intent!r} is not automatable", "R1_unknown_intent")

    # Rule 2
    if intent == "prod_change" or PRODUCTION_PATTERN.search(ticket_text or ""):
        return PolicyDecision(ESCALATE, "high", "production changes always go to an engineer", "R2_production")

    # Rule 3
    if confidence < min_confidence:
        return PolicyDecision(
            ESCALATE, RISK_BY_INTENT[intent],
            f"classifier confidence {confidence:.2f} < {min_confidence:.2f}", "R3_low_confidence",
        )

    # Rule 4
    risk = RISK_BY_INTENT[intent]
    return PolicyDecision(ROUTE_BY_RISK[risk], risk, f"{risk}-risk intent {intent}", "R4_risk_table")
