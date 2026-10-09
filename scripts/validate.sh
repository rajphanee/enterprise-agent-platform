#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# validate.sh - one command that proves the project works on YOUR machine.
#
#   bash scripts/validate.sh
#
# What it does (stops at the first failure):
#   1. checks Python version (3.9+)
#   2. checks pytest is installed
#   3. byte-compiles every file (catches syntax errors fast)
#   4. runs the unit tests
#   5. runs the golden-case evals
#   6. runs the full demo against a throwaway state folder
#   7. checks error handling: bad input -> exit 1, tampered audit -> exit 3
#
# It never touches ./var - everything runs in a temp folder that is deleted.
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."            # always run from the project root
PY="${PYTHON:-python3}"            # override with: PYTHON=python bash scripts/validate.sh
TMP_STATE="$(mktemp -d)"
trap 'rm -rf "$TMP_STATE"' EXIT    # clean up even if a step fails
export AGENT_STATE_DIR="$TMP_STATE"
export AGENT_MODE="${AGENT_MODE:-offline}"

step() { echo; echo "==> $1"; }
ok()   { echo "    PASS: $1"; }
fail() { echo "    FAIL: $1"; exit 1; }

step "1/7 Python version"
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  && ok "$("$PY" --version)" || fail "Python 3.9+ required"

step "2/7 pytest installed"
"$PY" -c 'import pytest' 2>/dev/null && ok "pytest found" \
  || fail "run: $PY -m pip install -r requirements.txt"

step "3/7 Syntax check"
"$PY" -m compileall -q agent_platform evals tests >/dev/null && ok "all files compile" || fail "syntax error"

step "4/7 Unit tests"
"$PY" -m pytest -q tests && ok "unit tests" || fail "unit tests"

step "5/7 Evals"
"$PY" evals/run_evals.py >/tmp/eap_evals.log 2>&1 && ok "$(tail -1 /tmp/eap_evals.log)" \
  || { cat /tmp/eap_evals.log; fail "evals"; }

step "6/7 End-to-end demo"
"$PY" -m agent_platform demo >/tmp/eap_demo.log 2>&1 && ok "$(tail -1 /tmp/eap_demo.log)" \
  || { cat /tmp/eap_demo.log; fail "demo"; }

step "7/7 Error handling"
set +e
"$PY" -m agent_platform ask "hello" --role ceo >/dev/null 2>&1; code=$?
[ "$code" -eq 1 ] && ok "bad role -> exit 1" || fail "bad role gave exit $code (expected 1)"
"$PY" -m agent_platform vendor --file nope.json >/dev/null 2>&1; code=$?
[ "$code" -eq 1 ] && ok "missing file -> exit 1" || fail "missing file gave exit $code (expected 1)"
sed -i.bak 's/"alice"/"mallory"/' "$TMP_STATE/audit.jsonl"     # tamper on purpose
"$PY" -m agent_platform audit verify >/dev/null 2>&1; code=$?
[ "$code" -eq 3 ] && ok "tampered audit -> exit 3" || fail "tamper not detected (exit $code)"
set -e

echo
echo "ALL CHECKS PASSED"
