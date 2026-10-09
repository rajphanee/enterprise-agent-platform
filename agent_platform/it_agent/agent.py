"""
agent.py - IT support agent.

FLOW (see README "Flow 2"):
  1. validate the ticket (id, requester, text)
  2. LLM classifies -> {intent, confidence}      (LLM failure => intent "unknown")
  3. policy.decide() picks the route             (code, not prompt)
  4. route:
       AUTO           -> run automation now
                         automation fails?  -> escalate to engineer
       NEEDS_APPROVAL -> add to approval queue, wait
       ESCALATE       -> assign to the engineering queue
  5. every step is written to the audit log

Later: approve(approval_id, approver) runs the automation that was waiting.
"""

import logging

from ..audit import AuditLog
from ..config import Settings
from ..errors import AutomationError, LLMError, ValidationError
from ..llm import BaseLLM
from . import policy
from .approvals import ApprovalQueue

log = logging.getLogger(__name__)

MAX_TICKET_CHARS = 5000
ENGINEER_QUEUE = "it-engineering-queue"


# ------------------------------------------------------------------ automations
# These are SIMULATED. In real life each would call Okta, Intune, Jira, etc.
# Put the words "simulate-failure" in a ticket to see the failure path.
def _check_failure(ticket: dict, action: str) -> None:
    if "simulate-failure" in ticket["text"].lower():
        raise AutomationError(f"{action} backend returned HTTP 503 (simulated)")


def reset_password(ticket):
    _check_failure(ticket, "password reset")
    return f"Password reset link sent to {ticket['requester']}"


def reset_vpn(ticket):
    _check_failure(ticket, "VPN profile reset")
    return f"VPN profile re-issued for {ticket['requester']}"


def grant_access(ticket):
    _check_failure(ticket, "access grant")
    return f"Access request provisioned for {ticket['requester']}"


def install_software(ticket):
    _check_failure(ticket, "software install")
    return f"Install job pushed to {ticket['requester']}'s laptop"


def ship_hardware(ticket):
    _check_failure(ticket, "hardware order")
    return f"Replacement hardware ordered for {ticket['requester']}"


AUTOMATIONS = {
    "password_reset": reset_password,
    "vpn_issue": reset_vpn,
    "access_request": grant_access,
    "software_install": install_software,
    "hardware_issue": ship_hardware,
    # NOTE: no entry for prod_change or unknown - on purpose. Nothing to run.
}


class ITSupportAgent:
    def __init__(self, llm: BaseLLM, audit: AuditLog, approvals: ApprovalQueue, settings: Settings):
        self.llm = llm
        self.audit = audit
        self.approvals = approvals
        self.settings = settings
        # Approved tickets need their original text again, so we keep it here.
        # NOTE: in-memory only. A real system would store tickets in a DB.
        self._tickets = {}

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _validate(ticket) -> dict:
        if not isinstance(ticket, dict):
            raise ValidationError("ticket must be a JSON object")
        for field in ("id", "requester", "text"):
            value = ticket.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"ticket field {field!r} is required and must be text")
        if len(ticket["text"]) > MAX_TICKET_CHARS:
            raise ValidationError(f"ticket text is longer than {MAX_TICKET_CHARS} characters")
        return {k: ticket[k].strip() for k in ("id", "requester", "text")}

    def _classify(self, ticket: dict) -> dict:
        try:
            result = self.llm.classify_ticket(ticket["text"], policy.INTENTS)
        except LLMError as exc:
            # Fail safe: if we can't classify, a human looks at it.
            log.warning("classification failed for %s: %s", ticket["id"], exc)
            return {"intent": "unknown", "confidence": 0.0, "reason": f"llm_error: {exc}"}
        return result

    def _run(self, ticket: dict, intent: str) -> str:
        action = AUTOMATIONS.get(intent)
        if action is None:
            raise AutomationError(f"no automation exists for intent {intent!r}")
        return action(ticket)

    def _escalate(self, ticket, classification, decision, reason) -> dict:
        self.audit.record("it_agent", "ticket_escalated", ticket["requester"],
                          {"ticket_id": ticket["id"], "assigned_to": ENGINEER_QUEUE, "reason": reason})
        return {
            "ticket_id": ticket["id"],
            "status": "escalated",
            "intent": classification["intent"],
            "confidence": classification["confidence"],
            "route": policy.ESCALATE,
            "risk": decision.risk,
            "rule": decision.rule,
            "assigned_to": ENGINEER_QUEUE,
            "reason": reason,
        }

    # ------------------------------------------------------------ main flow
    def handle_ticket(self, raw_ticket) -> dict:
        ticket = self._validate(raw_ticket)
        self._tickets[ticket["id"]] = ticket

        classification = self._classify(ticket)
        decision = policy.decide(
            classification["intent"], classification["confidence"],
            ticket["text"], self.settings.min_classifier_confidence,
        )
        self.audit.record("it_agent", "ticket_classified", ticket["requester"], {
            "ticket_id": ticket["id"],
            "intent": classification["intent"],
            "confidence": classification["confidence"],
            "llm": self.llm.name,
            "route": decision.route,
            "risk": decision.risk,
            "rule": decision.rule,
        })

        if decision.route == policy.ESCALATE:
            return self._escalate(ticket, classification, decision, decision.reason)

        if decision.route == policy.NEEDS_APPROVAL:
            item = self.approvals.add(ticket["id"], classification["intent"],
                                      ticket["requester"], ticket["text"][:200])
            self.audit.record("it_agent", "approval_requested", ticket["requester"],
                              {"ticket_id": ticket["id"], "approval_id": item["id"]})
            return {
                "ticket_id": ticket["id"],
                "status": "pending_approval",
                "intent": classification["intent"],
                "confidence": classification["confidence"],
                "route": decision.route,
                "risk": decision.risk,
                "rule": decision.rule,
                "approval_id": item["id"],
                "reason": decision.reason,
            }

        # AUTO
        try:
            outcome = self._run(ticket, classification["intent"])
        except AutomationError as exc:
            # The automation broke -> a person takes over. Never retry silently.
            return self._escalate(ticket, classification, decision, f"automation failed: {exc}")

        self.audit.record("it_agent", "ticket_auto_resolved", "system",
                          {"ticket_id": ticket["id"], "outcome": outcome})
        return {
            "ticket_id": ticket["id"],
            "status": "resolved",
            "intent": classification["intent"],
            "confidence": classification["confidence"],
            "route": decision.route,
            "risk": decision.risk,
            "rule": decision.rule,
            "outcome": outcome,
        }

    # ------------------------------------------------------- approval flow
    def approve(self, approval_id: str, approver: str) -> dict:
        item = self.approvals.approve(approval_id, approver)  # raises on policy errors
        self.audit.record("it_agent", "approval_granted", approver,
                          {"approval_id": approval_id, "ticket_id": item["ticket_id"]})

        # If the process restarted we no longer have the full text; fall back
        # to the stored summary (first 200 chars) - enough for the simulators.
        ticket = self._tickets.get(item["ticket_id"]) or {
            "id": item["ticket_id"], "requester": item["requester"], "text": item["summary"],
        }
        try:
            outcome = self._run(ticket, item["intent"])
        except AutomationError as exc:
            item = self.approvals.mark_result(approval_id, False, str(exc))
            self.audit.record("it_agent", "approved_action_failed", "system",
                              {"approval_id": approval_id, "error": str(exc)})
            return {"approval_id": approval_id, "status": "failed", "error": str(exc),
                    "next_step": f"escalated to {ENGINEER_QUEUE}"}

        item = self.approvals.mark_result(approval_id, True, outcome)
        self.audit.record("it_agent", "approved_action_executed", "system",
                          {"approval_id": approval_id, "outcome": outcome})
        return {"approval_id": approval_id, "status": "executed", "outcome": outcome}

    def reject(self, approval_id: str, approver: str, reason: str) -> dict:
        item = self.approvals.reject(approval_id, approver, reason)
        self.audit.record("it_agent", "approval_rejected", approver,
                          {"approval_id": approval_id, "reason": reason})
        return {"approval_id": approval_id, "status": item["status"], "reason": reason}
