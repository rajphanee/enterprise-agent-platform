"""
Enterprise Agent Platform
=========================

Three small enterprise AI agents that share one governed platform:

  1. KnowledgeAssistant    - permission-aware Q&A that must cite every sentence.
  2. ITSupportAgent        - LLM classifies tickets, but POLICY CODE decides what runs.
  3. VendorOnboardingAgent - rule-based vendor checks + parallel finance/legal review.

Every decision from every agent is written to a hash-chained audit log.

NOTE (for new developers):
  - Start reading at agent_platform/app.py  -> it wires everything together.
  - Then read cli.py                        -> it shows how each flow is called.
  - Nothing here needs internet unless you set AGENT_MODE=claude.
"""

__version__ = "1.0.0"
