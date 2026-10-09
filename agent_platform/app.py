"""
app.py - builds the whole platform in one call.

Everything (CLI, tests, evals) goes through build_platform() so all three
agents always share ONE audit log and ONE LLM client.

    from agent_platform.app import build_platform
    p = build_platform()
    p.knowledge.ask("How many PTO days do I get?", role="employee", user="alice")
"""

from dataclasses import dataclass
from datetime import date

from .audit import AuditLog
from .config import Settings, load_settings
from .it_agent.agent import ITSupportAgent
from .it_agent.approvals import ApprovalQueue
from .knowledge.assistant import KnowledgeAssistant
from .knowledge.store import KnowledgeStore
from .llm import BaseLLM, get_llm
from .vendor.agent import VendorOnboardingAgent


@dataclass
class Platform:
    settings: Settings
    llm: BaseLLM
    audit: AuditLog
    store: KnowledgeStore
    approvals: ApprovalQueue
    knowledge: KnowledgeAssistant
    it: ITSupportAgent
    vendor: VendorOnboardingAgent


def build_platform(settings: Settings = None, llm: BaseLLM = None, today: date = None) -> Platform:
    """
    settings: None -> read from env/.env
    llm:      None -> pick from settings.mode (tests pass a fake one)
    today:    None -> real date (tests pass a fixed date so insurance checks are stable)
    """
    settings = settings or load_settings()
    llm = llm or get_llm(settings)
    audit = AuditLog(settings.audit_file)

    store = KnowledgeStore()
    store.load_file(settings.documents_file)

    approvals = ApprovalQueue(settings.approvals_file)

    return Platform(
        settings=settings,
        llm=llm,
        audit=audit,
        store=store,
        approvals=approvals,
        knowledge=KnowledgeAssistant(store, llm, audit, settings),
        it=ITSupportAgent(llm, audit, approvals, settings),
        vendor=VendorOnboardingAgent(llm, audit, settings, today=today),
    )
