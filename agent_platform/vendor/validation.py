"""
validation.py - deterministic vendor checks. No LLM anywhere in this file.

Each check returns a list of Issue objects (empty list = passed).

Severity:
  "blocking" - we stop. The vendor cannot fix it by editing a form
               (watchlist hit, already in our registry).
  "fixable"  - the vendor can correct it (typo in tax id, expired insurance...).

Checks:
  required fields | email | US tax id (EIN) | bank account name matches legal name |
  bank routing checksum | insurance coverage + expiry | duplicate vendor | watchlist
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

BLOCKING = "blocking"
FIXABLE = "fixable"

MIN_INSURANCE_COVERAGE = 1_000_000
INSURANCE_WARNING_DAYS = 30

# EIN prefixes the IRS has never assigned. Small list, good enough for a demo.
INVALID_EIN_PREFIXES = {"00", "07", "08", "09", "17", "18", "19", "28", "29",
                        "49", "69", "70", "78", "79", "89", "96", "97"}
EIN_RE = re.compile(r"^\d{2}-\d{7}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
COMPANY_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "limited", "plc"}

REQUIRED_FIELDS = ("legal_name", "tax_id", "contact_email", "bank", "insurance")


@dataclass
class Issue:
    code: str
    field: str
    message: str
    severity: str = FIXABLE

    def to_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "message": self.message, "severity": self.severity}


def normalize_name(name: str) -> str:
    """'Acme Cloud Services, L.L.C.' -> 'acme cloud services'"""
    text = (name or "").lower().replace(".", "")          # L.L.C. -> llc
    text = re.sub(r"[^a-z0-9]+", " ", text).split()     # punctuation -> spaces
    while text and text[-1] in COMPANY_SUFFIXES:          # drop trailing suffixes
        text.pop()
    return " ".join(text)


# ------------------------------------------------------------------- checks
def check_required(vendor: dict) -> list:
    issues = []
    for name in REQUIRED_FIELDS:
        if not vendor.get(name):
            issues.append(Issue("MISSING_FIELD", name, f"'{name}' is required."))
    if vendor.get("bank") and not isinstance(vendor["bank"], dict):
        issues.append(Issue("BAD_FORMAT", "bank", "'bank' must be an object."))
    if vendor.get("insurance") and not isinstance(vendor["insurance"], dict):
        issues.append(Issue("BAD_FORMAT", "insurance", "'insurance' must be an object."))
    return issues


def check_email(email: str) -> list:
    if email and not EMAIL_RE.match(email):
        return [Issue("BAD_EMAIL", "contact_email", f"Contact email '{email}' is not valid.")]
    return []


def check_tax_id(tax_id: str) -> list:
    if not tax_id:
        return []  # already reported by check_required
    if not EIN_RE.match(tax_id):
        return [Issue("BAD_TAX_ID", "tax_id", "Tax ID must look like 12-3456789 (EIN format).")]
    if tax_id[:2] in INVALID_EIN_PREFIXES:
        return [Issue("BAD_TAX_ID", "tax_id", f"Tax ID prefix {tax_id[:2]} is not a valid EIN prefix.")]
    if tax_id.replace("-", "") == "0" * 9:
        return [Issue("BAD_TAX_ID", "tax_id", "Tax ID cannot be all zeros.")]
    return []


def routing_checksum_ok(routing: str) -> bool:
    """ABA routing check: 3*(d1+d4+d7) + 7*(d2+d5+d8) + (d3+d6+d9) must be divisible by 10."""
    if not re.fullmatch(r"\d{9}", routing or ""):
        return False
    d = [int(c) for c in routing]
    total = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return total % 10 == 0


def check_bank(legal_name: str, bank) -> list:
    if not isinstance(bank, dict):
        return []
    issues = []
    holder = bank.get("account_holder_name", "")
    if not holder:
        issues.append(Issue("MISSING_FIELD", "bank.account_holder_name", "Bank account holder name is required."))
    elif normalize_name(holder) != normalize_name(legal_name):
        issues.append(Issue(
            "BANK_NAME_MISMATCH", "bank.account_holder_name",
            f"Bank account is held by '{holder}', which does not match legal name '{legal_name}'.",
        ))
    if not routing_checksum_ok(str(bank.get("routing_number", ""))):
        issues.append(Issue("BAD_ROUTING", "bank.routing_number", "Routing number must be 9 digits with a valid checksum."))
    if not re.fullmatch(r"\d{4,17}", str(bank.get("account_number", ""))):
        issues.append(Issue("BAD_ACCOUNT", "bank.account_number", "Account number must be 4-17 digits."))
    return issues


def check_insurance(insurance, today: date) -> list:
    if not isinstance(insurance, dict):
        return []
    issues = []
    try:
        coverage = float(insurance.get("coverage_amount", 0))
    except (TypeError, ValueError):
        coverage = 0
    if coverage < MIN_INSURANCE_COVERAGE:
        issues.append(Issue(
            "LOW_COVERAGE", "insurance.coverage_amount",
            f"General liability coverage must be at least ${MIN_INSURANCE_COVERAGE:,}.",
        ))
    try:
        expiry = date.fromisoformat(str(insurance.get("expiry_date", "")))
    except ValueError:
        issues.append(Issue("BAD_DATE", "insurance.expiry_date", "Insurance expiry must be a date like 2027-01-31."))
        return issues
    if expiry <= today:
        issues.append(Issue("INSURANCE_EXPIRED", "insurance.expiry_date", f"Insurance expired on {expiry}. Upload a current certificate."))
    elif expiry <= today + timedelta(days=INSURANCE_WARNING_DAYS):
        issues.append(Issue("INSURANCE_EXPIRING", "insurance.expiry_date",
                            f"Insurance expires on {expiry} (within {INSURANCE_WARNING_DAYS} days). Upload the renewal."))
    return issues


def check_duplicate(vendor: dict, registry: list) -> list:
    name = normalize_name(vendor.get("legal_name", ""))
    tax_id = vendor.get("tax_id")
    for existing in registry:
        if tax_id and existing.get("tax_id") == tax_id:
            return [Issue("DUPLICATE_VENDOR", "tax_id",
                          f"Tax ID already belongs to vendor {existing.get('vendor_id')}.", BLOCKING)]
        if name and normalize_name(existing.get("legal_name", "")) == name:
            return [Issue("DUPLICATE_VENDOR", "legal_name",
                          f"A vendor with this name already exists ({existing.get('vendor_id')}).", BLOCKING)]
    return []


def check_watchlist(vendor: dict, watchlist: list) -> list:
    name = normalize_name(vendor.get("legal_name", ""))
    blocked = {normalize_name(entry) for entry in watchlist}
    if name and name in blocked:
        # NOTE: the message is internal. We never tell the vendor WHY they were stopped.
        return [Issue("WATCHLIST_HIT", "legal_name", "Name matches a sanctions/watchlist entry.", BLOCKING)]
    return []


def run_all_checks(vendor: dict, registry: list, watchlist: list, today: date) -> list:
    """Run every check and return all issues found (not just the first)."""
    issues = check_required(vendor)
    issues += check_email(vendor.get("contact_email", ""))
    issues += check_tax_id(vendor.get("tax_id", ""))
    issues += check_bank(vendor.get("legal_name", ""), vendor.get("bank"))
    issues += check_insurance(vendor.get("insurance"), today)
    issues += check_duplicate(vendor, registry)
    issues += check_watchlist(vendor, watchlist)
    return issues
