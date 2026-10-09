"""Vendor agent: validation checks, routing outcomes, reviewer crashes."""

import copy
import json

from agent_platform.errors import LLMError
from agent_platform.llm import OfflineLLM
from agent_platform.vendor.validation import normalize_name, routing_checksum_ok


def _samples(platform):
    return json.loads((platform.settings.data_dir / "vendors_sample.json").read_text())


def _clean_vendor(platform):
    return copy.deepcopy(_samples(platform)[0])


def test_helpers():
    assert normalize_name("Acme Cloud Services, L.L.C.") == "acme cloud services"
    assert routing_checksum_ok("021000021")
    assert not routing_checksum_ok("021000022")
    assert not routing_checksum_ok("12345")


def test_clean_vendor_is_approved_and_registered(platform):
    r = platform.vendor.onboard(_clean_vendor(platform))
    assert r["status"] == "approved" and r["vendor_id"].startswith("V-")
    # Registering again must now be caught as a duplicate.
    again = platform.vendor.onboard(_clean_vendor(platform))
    assert again["status"] == "rejected_duplicate"


def test_broken_vendor_gets_fix_list(platform):
    r = platform.vendor.onboard(_samples(platform)[1])
    codes = {i["code"] for i in r["issues"]}
    assert r["status"] == "needs_fixes"
    assert {"BAD_TAX_ID", "BANK_NAME_MISMATCH", "INSURANCE_EXPIRED", "LOW_COVERAGE", "BAD_EMAIL"} <= codes
    assert "1." in r["fix_list"]


def test_watchlist_hit_is_rejected_without_telling_vendor_why(platform):
    r = platform.vendor.onboard(_samples(platform)[2])
    assert r["status"] == "rejected" and r["escalated_to"] == "compliance"
    assert "watchlist" not in r["vendor_message"].lower()


def test_review_needing_info_is_pending(platform):
    r = platform.vendor.onboard(_samples(platform)[4])
    assert r["status"] == "pending_review"
    teams = {rv["team"]: rv["decision"] for rv in r["reviews"]}
    assert teams == {"finance": "needs_info", "legal": "needs_info"}


def test_reviewer_crash_goes_to_manual_review(platform):
    def exploding_review(vendor):
        raise RuntimeError("legal system down")
    platform.vendor.reviewers = {"finance": lambda v: {"team": "finance", "decision": "approved", "notes": []},
                                 "legal": exploding_review}
    r = platform.vendor.onboard(_clean_vendor(platform))
    assert r["status"] == "pending_manual_review"


def test_llm_failure_falls_back_to_plain_fix_list(platform):
    class BrokenLLM(OfflineLLM):
        def draft_fix_list(self, vendor_name, issues):
            raise LLMError("timeout")
    platform.vendor.llm = BrokenLLM()
    r = platform.vendor.onboard(_samples(platform)[1])
    assert r["status"] == "needs_fixes" and r["fix_list_by"] == "fallback"


def test_every_decision_is_audited_and_chain_is_valid(platform):
    for v in _samples(platform):
        platform.vendor.onboard(v)
    report = platform.audit.verify()
    assert report["ok"] and report["entries"] == 5
