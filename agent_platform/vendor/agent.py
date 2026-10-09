"""
agent.py - vendor onboarding agent.

FLOW (see README "Flow 3"):
  1. input must be a JSON object
  2. run_all_checks()                      (deterministic, validation.py)
  3. decide:
       watchlist hit       -> REJECTED, sent to compliance (vendor not told why)
       duplicate vendor    -> REJECTED_DUPLICATE
       fixable issues only -> NEEDS_FIXES + LLM-drafted fix list for the vendor
                              (LLM down? fall back to a plain list - never block on it)
       no issues           -> finance + legal reviews run IN PARALLEL
                                both approve   -> APPROVED, added to registry
                                any needs info -> PENDING_REVIEW
                                any crashes    -> PENDING_MANUAL_REVIEW
  4. audit every outcome
"""

import json
import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from ..audit import AuditLog
from ..config import Settings
from ..errors import LLMError, StorageError, ValidationError
from ..llm import BaseLLM
from .validation import BLOCKING, run_all_checks

log = logging.getLogger(__name__)

APPROVED_PAYMENT_TERMS = {"NET30", "NET45", "NET60"}
CFO_SIGNOFF_THRESHOLD = 250_000


# ------------------------------------------------------------ the reviewers
# Each reviewer returns {"team", "decision": approved|needs_info, "notes": [...]}
# They are plain functions so you can replace one with a real API call later.
def finance_review(vendor: dict) -> dict:
    notes = []
    terms = str(vendor.get("payment_terms", "")).upper()
    if terms not in APPROVED_PAYMENT_TERMS:
        notes.append(f"Payment terms '{terms or 'missing'}' not in {sorted(APPROVED_PAYMENT_TERMS)}")
    try:
        spend = float(vendor.get("annual_spend_estimate", 0))
    except (TypeError, ValueError):
        spend = 0
        notes.append("annual_spend_estimate is not a number")
    if spend > CFO_SIGNOFF_THRESHOLD:
        notes.append(f"Annual spend ${spend:,.0f} exceeds ${CFO_SIGNOFF_THRESHOLD:,}; CFO sign-off needed")
    return {"team": "finance", "decision": "needs_info" if notes else "approved", "notes": notes}


def legal_review(vendor: dict) -> dict:
    notes = []
    if vendor.get("msa_signed") is not True:
        notes.append("Master Services Agreement not signed")
    if vendor.get("w9_received") is not True:
        notes.append("W-9 form not received")
    return {"team": "legal", "decision": "needs_info" if notes else "approved", "notes": notes}


class VendorOnboardingAgent:
    def __init__(self, llm: BaseLLM, audit: AuditLog, settings: Settings, today: date = None,
                 reviewers: dict = None):
        self.llm = llm
        self.audit = audit
        self.settings = settings
        self.today = today  # None = use the real date. Tests pass a fixed date.
        # Injectable so tests can swap in a reviewer that crashes.
        self.reviewers = reviewers or {"finance": finance_review, "legal": legal_review}
        self._registry_lock = threading.Lock()
        self.watchlist = self._load_json_list(settings.watchlist_file, "watchlist")

    # ------------------------------------------------------------ storage
    @staticmethod
    def _load_json_list(path: Path, label: str) -> list:
        if not Path(path).exists():
            raise StorageError(f"{label} file not found: {path}")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"{label} file {path} is not valid JSON: {exc}")
        if not isinstance(data, list):
            raise StorageError(f"{label} file {path} must contain a JSON list")
        return data

    def _load_registry(self) -> list:
        runtime = self.settings.vendor_registry_file
        if not runtime.exists():
            # First run: copy the seed file so we never modify data/ in git.
            seed = self.settings.registry_seed_file
            if not seed.exists():
                raise StorageError(f"vendor registry seed file not found: {seed}")
            shutil.copyfile(seed, runtime)
        return self._load_json_list(runtime, "vendor registry")

    def _add_to_registry(self, vendor: dict) -> str:
        with self._registry_lock:
            registry = self._load_registry()
            vendor_id = f"V-{1001 + len(registry)}"
            registry.append({"vendor_id": vendor_id, "legal_name": vendor["legal_name"],
                             "tax_id": vendor["tax_id"]})
            tmp = self.settings.vendor_registry_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(registry, indent=2), encoding="utf-8")
            os.replace(tmp, self.settings.vendor_registry_file)
            return vendor_id

    # ------------------------------------------------------------ helpers
    def _fix_list(self, vendor_name: str, issues: list) -> tuple:
        """Return (text, source). LLM first, plain list if the LLM fails."""
        try:
            return self.llm.draft_fix_list(vendor_name, issues), self.llm.name
        except LLMError as exc:
            log.warning("fix-list drafting failed, using fallback: %s", exc)
            lines = [f"{n}. {i.message}" for n, i in enumerate(issues, start=1)]
            return "Please correct the following:\n" + "\n".join(lines), "fallback"

    def _run_reviews(self, vendor: dict) -> list:
        """Run every reviewer at the same time. A crash becomes decision='error'."""
        results = []
        with ThreadPoolExecutor(max_workers=len(self.reviewers)) as pool:
            futures = {team: pool.submit(fn, vendor) for team, fn in self.reviewers.items()}
            for team, future in futures.items():
                try:
                    results.append(future.result(timeout=30))
                except Exception as exc:  # noqa: BLE001 - we really do want to catch anything here
                    log.error("%s review crashed: %s", team, exc)
                    results.append({"team": team, "decision": "error", "notes": [str(exc)]})
        return results

    # ------------------------------------------------------------ main flow
    def onboard(self, vendor) -> dict:
        if not isinstance(vendor, dict):
            raise ValidationError("vendor must be a JSON object")
        name = str(vendor.get("legal_name") or "<no name>")
        today = self.today or date.today()

        issues = run_all_checks(vendor, self._load_registry(), self.watchlist, today)
        codes = {i.code for i in issues}
        blocking = [i for i in issues if i.severity == BLOCKING]

        if "WATCHLIST_HIT" in codes:
            self.audit.record("vendor", "vendor_rejected_watchlist", "system",
                              {"legal_name": name, "escalated_to": "compliance"})
            return {"legal_name": name, "status": "rejected", "escalated_to": "compliance",
                    "vendor_message": "We are unable to proceed with onboarding at this time.",
                    "issues": [i.to_dict() for i in issues]}

        if blocking:  # duplicate vendor
            self.audit.record("vendor", "vendor_rejected_duplicate", "system",
                              {"legal_name": name, "issues": [i.to_dict() for i in blocking]})
            return {"legal_name": name, "status": "rejected_duplicate",
                    "issues": [i.to_dict() for i in issues]}

        if issues:
            fix_list, source = self._fix_list(name, issues)
            self.audit.record("vendor", "vendor_needs_fixes", "system",
                              {"legal_name": name, "issue_codes": sorted(codes), "fix_list_by": source})
            return {"legal_name": name, "status": "needs_fixes",
                    "issues": [i.to_dict() for i in issues],
                    "fix_list": fix_list, "fix_list_by": source}

        reviews = self._run_reviews(vendor)
        decisions = {r["decision"] for r in reviews}
        if "error" in decisions:
            status = "pending_manual_review"
        elif decisions == {"approved"}:
            status = "approved"
        else:
            status = "pending_review"

        result = {"legal_name": name, "status": status, "reviews": reviews, "issues": []}
        if status == "approved":
            result["vendor_id"] = self._add_to_registry(vendor)

        self.audit.record("vendor", f"vendor_{status}", "system", {
            "legal_name": name, "vendor_id": result.get("vendor_id"),
            "reviews": {r["team"]: r["decision"] for r in reviews},
        })
        return result
