"""
llm.py - the ONLY file that talks to a language model.

There are two implementations with the same three methods:

  OfflineLLM  - keyword rules. No network, no key. Used by tests, CI and demos.
  ClaudeLLM   - calls the Anthropic API (pip install anthropic).

The agents never know which one they got. That is on purpose: you can test
every flow offline, then flip AGENT_MODE=claude without touching agent code.

IMPORTANT RULE OF THIS PROJECT:
  The LLM only *suggests* (an intent, an answer draft, a fix list).
  Code outside this file *decides* (policy.py, assistant validation, vendor checks).
"""

import logging
import re
import time

from .config import Settings
from .errors import ConfigError, LLMError

log = logging.getLogger(__name__)

# The knowledge assistant asks the model to emit this token when the
# documents do not contain the answer. The assistant turns it into a decline.
INSUFFICIENT = "INSUFFICIENT_CONTEXT"


class BaseLLM:
    name = "base"

    def classify_ticket(self, text: str, intents: list) -> dict:
        """Return {"intent": str, "confidence": float 0..1, "reason": str}."""
        raise NotImplementedError

    def answer_question(self, question: str, chunks: list) -> str:
        """Return an answer where EVERY sentence ends with [doc:<id>]."""
        raise NotImplementedError

    def draft_fix_list(self, vendor_name: str, issues: list) -> str:
        """Return a short, polite, numbered list the vendor can act on."""
        raise NotImplementedError


# ============================================================== OFFLINE MODE
class OfflineLLM(BaseLLM):
    """Deterministic stand-in for a model. Same input -> same output, always."""

    name = "offline"

    # intent -> words that suggest it. Order matters only for ties.
    KEYWORDS = {
        "password_reset": {"password", "locked", "unlock", "login", "mfa", "reset"},
        "vpn_issue": {"vpn", "globalconnect", "tunnel", "disconnect", "disconnecting"},
        "access_request": {"access", "permission", "permissions", "grant", "repo", "jira", "role"},
        "software_install": {"install", "software", "license", "upgrade", "app", "application"},
        "hardware_issue": {"laptop", "monitor", "keyboard", "mouse", "screen", "battery", "broken"},
        "prod_change": {"production", "deploy", "deployment", "hotfix", "migration", "rollback"},
    }

    def classify_ticket(self, text: str, intents: list) -> dict:
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        best_intent, best_hits = "unknown", 0
        for intent, keywords in self.KEYWORDS.items():
            if intent not in intents:
                continue
            hits = len(words & keywords)
            if hits > best_hits:
                best_intent, best_hits = intent, hits
        if best_hits == 0:
            return {"intent": "unknown", "confidence": 0.30, "reason": "no known keywords"}
        confidence = min(0.95, 0.50 + 0.15 * best_hits)
        return {
            "intent": best_intent,
            "confidence": round(confidence, 2),
            "reason": f"{best_hits} keyword match(es) for {best_intent}",
        }

    def answer_question(self, question: str, chunks: list) -> str:
        # Lazy import to avoid a circular import (store imports nothing from here).
        from .knowledge.store import tokenize

        q_terms = set(tokenize(question))
        if not chunks or not q_terms:
            return INSUFFICIENT

        top = chunks[0]  # chunks arrive best-first from the store
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", top.text) if s.strip()]
        scored = []
        for position, sentence in enumerate(sentences):
            overlap = len(q_terms & set(tokenize(sentence)))
            if overlap:
                scored.append((overlap, position, sentence))
        if not scored:
            return INSUFFICIENT

        # Keep the 2 most relevant sentences, but print them in document order.
        picked = sorted(sorted(scored, key=lambda t: -t[0])[:2], key=lambda t: t[1])
        cited = [f"{s.rstrip('.!?')} [doc:{top.doc_id}]." for _, _, s in picked]
        return " ".join(cited)

    def draft_fix_list(self, vendor_name: str, issues: list) -> str:
        lines = [f"Hello {vendor_name} team, please fix the following before we can continue:"]
        for number, issue in enumerate(issues, start=1):
            lines.append(f"{number}. {issue.message}")
        lines.append("Reply to this message once updated and we will re-run the checks.")
        return "\n".join(lines)


# =============================================================== CLAUDE MODE
class ClaudeLLM(BaseLLM):
    name = "claude"

    def __init__(self, settings: Settings):
        try:
            import anthropic  # imported here so offline users never need it
        except ImportError:
            raise ConfigError("AGENT_MODE=claude needs the SDK: pip install anthropic")
        self._anthropic = anthropic
        self.settings = settings
        self.client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # we do our own retry loop below so we can log it
        )

    def _call(self, **kwargs):
        """Call the API with simple retry + backoff for temporary failures."""
        a = self._anthropic
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self.client.messages.create(model=self.settings.claude_model, **kwargs)
            except a.AuthenticationError as exc:
                raise LLMError(f"Anthropic rejected the API key: {exc}")  # retrying won't help
            except (a.APIConnectionError, a.RateLimitError) as exc:
                problem = exc
            except a.APIStatusError as exc:
                if exc.status_code < 500:
                    raise LLMError(f"Anthropic API error {exc.status_code}: {exc}")
                problem = exc
            if attempt < attempts:
                wait = 2 ** attempt
                log.warning("LLM call failed (%s). Retry %s/%s in %ss", problem, attempt, attempts - 1, wait)
                time.sleep(wait)
        raise LLMError(f"LLM call failed after {attempts} attempts: {problem}")

    def classify_ticket(self, text: str, intents: list) -> dict:
        # FORCED TOOL CALL: tool_choice makes Claude answer ONLY through this tool,
        # and "enum" means the intent must be one of our fixed list.
        tool = {
            "name": "classify_ticket",
            "description": "Classify an IT support ticket into exactly one intent.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": list(intents)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["intent", "confidence", "reason"],
            },
        }
        response = self._call(
            max_tokens=300,
            system="You classify IT tickets. If unsure, use intent 'unknown' with low confidence.",
            tools=[tool],
            tool_choice={"type": "tool", "name": "classify_ticket"},
            messages=[{"role": "user", "content": f"Ticket:\n{text}"}],
        )
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                data = dict(block.input)
                # Never trust the model blindly - re-check the shape here too.
                if data.get("intent") not in intents:
                    raise LLMError(f"model returned unknown intent {data.get('intent')!r}")
                try:
                    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0))))
                except (TypeError, ValueError):
                    raise LLMError("model returned a non-numeric confidence")
                data.setdefault("reason", "")
                return data
        raise LLMError("model did not return a tool_use block")

    def answer_question(self, question: str, chunks: list) -> str:
        context = "\n\n".join(f'<doc id="{c.doc_id}">\n{c.text}\n</doc>' for c in chunks)
        system = (
            "Answer ONLY from the documents provided. "
            "End EVERY sentence with its source like this: 'Text here [doc:ID].' "
            f"If the documents do not answer the question, reply with exactly {INSUFFICIENT}."
        )
        response = self._call(
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
        )
        return "".join(getattr(b, "text", "") for b in response.content).strip()

    def draft_fix_list(self, vendor_name: str, issues: list) -> str:
        issue_text = "\n".join(f"- {i.field}: {i.message}" for i in issues)
        response = self._call(
            max_tokens=400,
            system="Write a short, polite numbered list telling a vendor what to fix. No legal advice.",
            messages=[{"role": "user", "content": f"Vendor: {vendor_name}\nProblems:\n{issue_text}"}],
        )
        text = "".join(getattr(b, "text", "") for b in response.content).strip()
        if not text:
            raise LLMError("model returned an empty fix list")
        return text


def get_llm(settings: Settings) -> BaseLLM:
    """Factory: pick the implementation from AGENT_MODE."""
    if settings.mode == "claude":
        return ClaudeLLM(settings)
    return OfflineLLM()
