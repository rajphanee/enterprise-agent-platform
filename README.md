# Enterprise Agent Platform

A small, readable reference implementation of three enterprise AI agents that share one governed platform:

1. **Knowledge assistant** – permission-aware Q&A. Every sentence must cite a source document; if it can't, it declines instead of guessing.
2. **IT support agent** – an LLM classifies the ticket, but a policy engine written in code decides what runs automatically, what waits for a human approval, and what goes straight to an engineer.
3. **Vendor onboarding agent** – deterministic checks (tax ID, bank name match, routing checksum, insurance, duplicates, watchlist), parallel finance/legal review, and an LLM-drafted fix list for the vendor.

Every decision from every agent is written to a **hash-chained audit log** that detects edits and deletions.

It runs **fully offline** (no API key, no model download, only `pytest` to install) for development, tests, and CI. Set `AGENT_MODE=claude` to use the Anthropic API instead.

---

## 1. Folder layout

```
enterprise-agent-platform/
├── agent_platform/            # the application package
│   ├── app.py                 # build_platform(): wires everything together  <- START HERE
│   ├── cli.py                 # command line (python -m agent_platform ...)
│   ├── config.py              # all settings, read from env / .env
│   ├── errors.py              # every expected error type
│   ├── audit.py               # hash-chained audit log
│   ├── llm.py                 # OfflineLLM + ClaudeLLM (the only file that talks to a model)
│   ├── knowledge/
│   │   ├── store.py           # TF-IDF search with role filter applied BEFORE scoring
│   │   └── assistant.py       # citation gate, per-role cache, declines
│   ├── it_agent/
│   │   ├── policy.py          # routing rules (code, not prompt)
│   │   ├── approvals.py       # approval queue + separation of duties
│   │   └── agent.py           # ticket flow + simulated automations
│   └── vendor/
│       ├── validation.py      # deterministic checks, no LLM
│       └── agent.py           # outcome routing + parallel reviews
├── data/                      # sample documents, tickets, vendors, registry seed, watchlist
├── tests/                     # 47 unit tests (pytest)
├── evals/                     # golden-case evals for agent behaviour
├── scripts/validate.sh        # one-command validation (Mac/Linux)
├── scripts/validate.ps1       # same for Windows PowerShell
├── .github/workflows/ci.yml   # runs validate.sh on Python 3.9 / 3.11 / 3.12
├── .env.example               # copy to .env
└── var/                       # created at runtime: audit log, approvals, registry (git-ignored)
```

---

## 2. Prerequisites

| Need | Version | Check with |
|---|---|---|
| Python | 3.9 or newer | `python3 --version` (Windows: `python --version`) |
| pip | any recent | `python3 -m pip --version` |
| git | optional, for pushing to GitHub | `git --version` |

No database, no Docker, no model download.

---

## 3. Setup (5 minutes)

### Mac / Linux

```bash
unzip enterprise-agent-platform.zip
cd enterprise-agent-platform

python3 -m venv .venv               # isolated Python environment
source .venv/bin/activate           # prompt now shows (.venv)
python -m pip install -r requirements.txt
cp .env.example .env                # optional - defaults already work
```

### Windows (PowerShell)

```powershell
Expand-Archive enterprise-agent-platform.zip -DestinationPath .
cd enterprise-agent-platform

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

---

## 4. Validate everything with one command

```bash
bash scripts/validate.sh                                            # Mac / Linux
powershell -ExecutionPolicy Bypass -File scripts\validate.ps1       # Windows
```

Expected ending:

```
==> 4/7 Unit tests
47 passed
    PASS: unit tests
==> 5/7 Evals
    PASS: RESULT: 26/26 passed
==> 6/7 End-to-end demo
    PASS: chain ok=True entries=25
==> 7/7 Error handling
    PASS: bad role -> exit 1
    PASS: missing file -> exit 1
    PASS: tampered audit -> exit 3

ALL CHECKS PASSED
```

The script uses a throwaway temp folder, so it never touches your `var/` data.

### Or run each check by hand

| # | Command | What you should see |
|---|---|---|
| 1 | `python -m pytest -q` | `47 passed` |
| 2 | `python evals/run_evals.py` | `RESULT: 26/26 passed`, exit code 0 |
| 3 | `python -m agent_platform demo` | all three flows print, ends with `chain ok=True` |
| 4 | `python -m agent_platform audit verify` | `"ok": true` |
| 5 | `python -m agent_platform --help` | list of commands |

Check an exit code: `echo $?` (Mac/Linux) or `echo $LASTEXITCODE` (PowerShell).

---

## 5. The flows

### Flow 1 – Knowledge assistant

```
question + role
     │
     ├─ validate (non-empty, ≤1000 chars, role known) ── bad ──► ValidationError (exit 1)
     │
     ├─ cache hit for (role, question)? ── yes ──► return cached answer (cached=true)
     │
     ├─ search(question, role)      ◄── chunks the role can't see are removed BEFORE scoring
     │      no match / score < MIN_RETRIEVAL_SCORE ──► DECLINE no_relevant_documents
     │
     ├─ LLM drafts answer           ── LLM error ──► DECLINE llm_error (fail closed)
     │
     ├─ citation gate
     │      empty / INSUFFICIENT_CONTEXT ──► DECLINE
     │      any sentence without [doc:ID] ──► DECLINE uncited_sentence
     │      cites a doc not retrieved     ──► DECLINE cited_unretrieved_document
     │
     └─ cache (per role) + audit ──► ANSWERED
```

Try it:

```bash
python -m agent_platform ask "How many PTO days do I get per year?" --role employee
python -m agent_platform ask "What are the salary bands for Senior Engineer?" --role employee   # declined
python -m agent_platform ask "What are the salary bands for Senior Engineer?" --role hr         # answered
```

Roles: `employee`, `engineer`, `hr`, `finance`, `admin`. Each document in `data/documents.json` lists its `allowed_roles`.

### Flow 2 – IT support agent

```
ticket {id, requester, text}
     │
     ├─ validate ── bad ──► ValidationError
     ├─ LLM classifies → {intent, confidence}   (LLM down → intent "unknown")
     ├─ policy.decide()   first matching rule wins:
     │     R1 unknown intent                          → ESCALATE
     │     R2 production (intent OR text mentions it) → ESCALATE   (always)
     │     R3 confidence < MIN_CLASSIFIER_CONFIDENCE  → ESCALATE
     │     R4 risk table: low → AUTO, medium → NEEDS_APPROVAL, high → ESCALATE
     │
     ├─ AUTO           → run automation → resolved   (automation fails → escalated)
     ├─ NEEDS_APPROVAL → approval queue → pending_approval
     └─ ESCALATE       → it-engineering-queue → escalated

approve(id, approver):  approver ≠ requester, item must be pending → run → executed | failed
reject(id, approver, reason):  reason required → rejected
```

Try it:

```bash
python -m agent_platform ticket --file data/tickets_sample.json
python -m agent_platform approvals list --status pending
python -m agent_platform approvals approve APR-0001 --approver carol-manager
python -m agent_platform approvals approve APR-0001 --approver carol-manager   # error: already executed
python -m agent_platform approvals reject  APR-0002 --approver it-lead --reason "No license budget"
python -m agent_platform ticket --text "password locked simulate-failure" --requester zoe   # failure path
```

The ticket sample includes a trick: T-1007 *"install the database client on the prod jump box"* is classified as `software_install`, but rule R2 still escalates it because the text mentions prod.

### Flow 3 – Vendor onboarding

```
vendor JSON
     │
     ├─ run_all_checks()   required fields, email, EIN, bank holder name, routing checksum,
     │                     account number, insurance coverage/expiry, duplicate, watchlist
     │
     ├─ WATCHLIST_HIT      → rejected (sent to compliance; vendor gets a neutral message)
     ├─ DUPLICATE_VENDOR   → rejected_duplicate
     ├─ fixable issues     → needs_fixes + LLM fix list (LLM down → plain fallback list)
     └─ no issues          → finance + legal reviews IN PARALLEL
                               both approved     → approved (added to var/vendor_registry.json)
                               any needs_info    → pending_review
                               any reviewer crash→ pending_manual_review
```

Try it:

```bash
python -m agent_platform vendor --file data/vendors_sample.json
```

Expected statuses: `approved`, `needs_fixes`, `rejected`, `rejected_duplicate`, `pending_review`. Run it a second time and Acme becomes `rejected_duplicate` – it was registered on the first run. Use `reset` to start clean.

### Audit log

```bash
python -m agent_platform audit tail -n 5
python -m agent_platform audit verify
```

**Tamper demo** (Mac/Linux):

```bash
python -m agent_platform demo
sed -i.bak 's/"alice"/"mallory"/' var/audit.jsonl     # edit history
python -m agent_platform audit verify; echo "exit=$?"  # ok=false, exit=3
python -m agent_platform reset --yes                   # start clean
```

---

## 6. CLI reference

| Command | Purpose |
|---|---|
| `ask "<question>" --role R [--user U]` | Knowledge assistant |
| `ticket --file F.json` / `ticket --text "..." --requester U [--id ID]` | IT agent |
| `approvals list [--status pending\|approved\|rejected\|executed\|failed]` | View queue |
| `approvals approve ID --approver U` | Approve and run |
| `approvals reject ID --approver U --reason "..."` | Reject |
| `vendor --file F.json` | Onboard one vendor or a list |
| `audit verify` / `audit tail -n N` | Check / view the audit log |
| `demo` | Run every flow once |
| `reset --yes` | Delete `var/` (audit, approvals, registry) |
| `-v` before any command | Debug logging |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Expected error – bad input, policy violation, missing file. Message on stderr as `ERROR [CODE]: ...` |
| 2 | Wrong command-line usage (argparse prints help) |
| 3 | Audit log tampering detected |

---

## 7. Error handling – how it is designed

All expected failures inherit from `PlatformError` (`errors.py`). The CLI catches those and prints one clean line; anything else is a real bug and shows a full traceback on purpose.

| Situation | What happens | Where |
|---|---|---|
| Missing/invalid setting, Claude mode without key | `ConfigError` at startup (fail fast) | `config.py` |
| Bad question, ticket, or vendor shape | `ValidationError` | each agent |
| LLM timeout / 5xx / rate limit | retried with backoff, then `LLMError` | `llm.py` |
| LLM fails during Q&A | **decline** (never guess) | `assistant.py` |
| LLM fails during ticket classification | intent `unknown` → **escalate** to a human | `it_agent/agent.py` |
| LLM fails drafting vendor fix list | plain fallback list, flow continues | `vendor/agent.py` |
| LLM returns an intent not in the list | `LLMError` → escalate | `llm.py` |
| Automation backend fails | ticket escalated / approval marked `failed` | `it_agent/agent.py` |
| Self-approval, double approval | `PolicyError` | `approvals.py` |
| Reviewer thread crashes | `pending_manual_review` | `vendor/agent.py` |
| Corrupted approvals / registry JSON | `StorageError` with fix hint | `approvals.py`, `vendor/agent.py` |
| Crash in the middle of a save | temp file + `os.replace()` – old file stays intact | all writers |
| Audit edited / middle line deleted / tail deleted | `verify` reports it, exit 3 | `audit.py` |

The principle across all three agents: **when unsure or broken, fail toward a human, never toward an action.**

---

## 8. Running with Claude

```bash
python -m pip install -r requirements-claude.txt
```

Edit `.env`:

```
AGENT_MODE=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-5-5
```

Then run the same commands, and the evals against the real model:

```bash
python -m agent_platform ask "How many PTO days do I get per year?" --role employee
python evals/run_evals.py
```

What changes in Claude mode: classification uses a **forced tool call** (`tool_choice`) whose `intent` field is an `enum` of the fixed list; answers are drafted by the model but still pass through the same citation gate; policy, approvals, validation and audit are identical. Unit tests never call the network – `tests/test_claude_llm.py` uses a fake SDK.

Never commit `.env`. It is in `.gitignore`.

---

## 9. Common changes (for developers)

**Add a document** – append to `data/documents.json` with `id`, `title`, `text`, `allowed_roles`. Paragraphs (blank-line separated) become chunks. Add a golden case to `evals/golden_cases.json`.

**Add an IT intent** – add it to `INTENTS` and `RISK_BY_INTENT` in `policy.py`, keywords in `OfflineLLM.KEYWORDS`, an automation function in `AUTOMATIONS` (only if it may ever run), then a test.

**Add a vendor check** – write `check_xxx()` in `validation.py` returning `Issue` objects, call it from `run_all_checks()`, add a test.

**Change thresholds** – `.env` (`MIN_RETRIEVAL_SCORE`, `MIN_CLASSIFIER_CONFIDENCE`), then rerun evals.

**Swapping in a real vector DB** – replace `KnowledgeStore.search()` with ChromaDB (or pgvector). Keep the rule that the role filter is part of the query itself, e.g. Chroma `where={"role_<name>": True}`, not a filter applied after results come back.

After any change: `bash scripts/validate.sh`.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named pytest` | Activate the venv, then `python -m pip install -r requirements.txt` |
| `No module named agent_platform` | Run commands from the project root folder |
| `python3: command not found` (Windows) | Use `python` instead of `python3` |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ERROR [CONFIG_ERROR] ... ANTHROPIC_API_KEY is empty` | Add the key to `.env` or set `AGENT_MODE=offline` |
| `ERROR [STORAGE_ERROR] ... approvals.json is corrupted` | `python -m agent_platform reset --yes` |
| Acme shows `rejected_duplicate` | Expected on a second run. `reset --yes` |
| `approve` says *already executed* | Each approval can be decided once – by design |
| `audit verify` exit 3 after editing files in `var/` | Expected – that is the tamper detection. `reset --yes` |

---

## 11. Known limitations (honest notes)

- Offline mode uses keyword rules and TF-IDF. It shows the *governance* flows, not model quality.
- The audit log detects tampering but can't prevent someone who rewrites both `audit.jsonl` and `audit.head`. Production: ship the head hash to write-once storage or a SIEM.
- Audit `record()` re-reads the file each time – fine for thousands of entries, not millions.
- Approved IT tickets keep their full text in memory; after a restart the stored 200-char summary is used.
- File locks are per process. Two CLI processes writing at the exact same moment could race; use a database for real concurrency.
- Automations, finance and legal reviewers are simulated functions with the same shape a real API call would have.
- Roles come from the `--role` flag. In production they must come from your identity provider, never from user input.

---

## 12. Push to GitHub

```bash
git init
git add .
git commit -m "Enterprise Agent Platform: knowledge, IT and vendor agents with audit log"
git branch -M main
git remote add origin https://github.com/<your-user>/enterprise-agent-platform.git
git push -u origin main
```

The included GitHub Actions workflow runs `scripts/validate.sh` on Python 3.9, 3.11 and 3.12 for every push.
