"""
run_evals.py - golden-case evaluation for all three agents.

Unit tests check that code paths work. Evals check that the AGENTS behave the
way the business expects on realistic inputs. Run this after any change to
prompts, documents, keywords, policy rules or thresholds.

    python evals/run_evals.py            # offline, uses a throwaway state folder
    AGENT_MODE=claude python evals/run_evals.py   # same cases against Claude

Exit code 0 = every case passed, 1 = at least one failed (CI-friendly).
"""

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_platform.app import build_platform  # noqa: E402
from agent_platform.config import load_settings  # noqa: E402


def main() -> int:
    cases = json.loads((Path(__file__).parent / "golden_cases.json").read_text())
    failures, total = [], 0

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, AGENT_STATE_DIR=tmp)
        platform = build_platform(load_settings(env=env), today=date(2026, 10, 6))
        print(f"Running evals with LLM = {platform.llm.name}\n")

        print("Knowledge assistant")
        for c in cases["knowledge"]:
            total += 1
            r = platform.knowledge.ask(c["question"], role=c["role"], user="eval")
            ok = r["status"] == c["expect_status"]
            if ok and c.get("expect_citation"):
                ok = c["expect_citation"] in r["citations"]
            print(f"  {'PASS' if ok else 'FAIL'}  [{c['role']:8}] {c['question']} -> {r['status']}")
            if not ok:
                failures.append(("knowledge", c, r))

        print("\nIT agent")
        for n, c in enumerate(cases["it"], start=1):
            total += 1
            r = platform.it.handle_ticket({"id": f"EVAL-{n}", "requester": "eval-user", "text": c["text"]})
            ok = r["status"] == c["expect_status"]
            print(f"  {'PASS' if ok else 'FAIL'}  {c['text'][:55]:55} -> {r['status']}")
            if not ok:
                failures.append(("it", c, r))

        print("\nVendor agent")
        vendors = json.loads((ROOT / "data" / "vendors_sample.json").read_text())
        for vendor, expected in zip(vendors, cases["vendor_expected_status_by_sample_index"]):
            total += 1
            r = platform.vendor.onboard(vendor)
            ok = r["status"] == expected
            print(f"  {'PASS' if ok else 'FAIL'}  {vendor['legal_name']:30} -> {r['status']}")
            if not ok:
                failures.append(("vendor", vendor["legal_name"], r))

        audit = platform.audit.verify()
        total += 1
        print(f"\nAudit chain: {'PASS' if audit['ok'] else 'FAIL'} ({audit['entries']} entries)")
        if not audit["ok"]:
            failures.append(("audit", audit, None))

    passed = total - len(failures)
    print(f"\nRESULT: {passed}/{total} passed")
    for area, case, result in failures:
        print(f"  FAILED {area}: {case}\n         got: {result}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
