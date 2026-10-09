"""
cli.py - command line entry point.

Run `python -m agent_platform --help` to see every command.

EXIT CODES (scripts and CI rely on these):
  0  success
  1  expected error (bad input, policy violation, missing file...) - message printed
  2  wrong command-line usage (argparse prints help)
  3  audit verification found tampering
"""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .app import build_platform
from .config import load_settings
from .errors import PlatformError

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TAMPERED = 0, 1, 2, 3


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_json_file(path: str):
    """Read a JSON file given on the command line, with friendly errors."""
    file = Path(path)
    if not file.exists():
        raise PlatformError(f"file not found: {file}")
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlatformError(f"{file} is not valid JSON (line {exc.lineno}): {exc.msg}")


def _as_list(data):
    return data if isinstance(data, list) else [data]


# ------------------------------------------------------------------ commands
def cmd_ask(platform, args):
    _print(platform.knowledge.ask(args.question, role=args.role, user=args.user))
    return EXIT_OK


def cmd_ticket(platform, args):
    if args.file:
        tickets = _as_list(_read_json_file(args.file))
    elif args.text:
        tickets = [{"id": args.id, "requester": args.requester, "text": args.text}]
    else:
        raise PlatformError("give either --file tickets.json or --text \"...\"")
    _print([platform.it.handle_ticket(t) for t in tickets])
    return EXIT_OK


def cmd_approvals(platform, args):
    if args.action == "list":
        _print(platform.approvals.list(status=args.status))
    elif args.action == "approve":
        _print(platform.it.approve(args.approval_id, args.approver))
    elif args.action == "reject":
        _print(platform.it.reject(args.approval_id, args.approver, args.reason))
    return EXIT_OK


def cmd_vendor(platform, args):
    _print([platform.vendor.onboard(v) for v in _as_list(_read_json_file(args.file))])
    return EXIT_OK


def cmd_audit(platform, args):
    if args.action == "tail":
        _print(platform.audit.tail(args.n))
        return EXIT_OK
    report = platform.audit.verify()
    _print(report)
    return EXIT_OK if report["ok"] else EXIT_TAMPERED


def cmd_demo(platform, args):
    """Runs one example of every flow end to end. Great first command to try."""
    data = platform.settings.data_dir

    print("\n=== FLOW 1: Knowledge assistant ===")
    for question, role in [
        ("How many PTO days do I get per year?", "employee"),     # answered
        ("What are the salary bands for Senior Engineer?", "employee"),  # declined: no access
        ("What are the salary bands for Senior Engineer?", "hr"),        # answered
        ("How many PTO days do I get per year?", "employee"),     # served from cache
    ]:
        r = platform.knowledge.ask(question, role=role, user=f"demo-{role}")
        print(f"[{role:8}] {question}\n   -> {r['status']} (cached={r['cached']}): {r['answer']}")

    print("\n=== FLOW 2: IT support agent ===")
    approval_ids = []
    for result in (platform.it.handle_ticket(t) for t in _read_json_file(data / "tickets_sample.json")):
        print(f"{result['ticket_id']}: {result['intent']:16} -> {result['status']:16} ({result.get('rule')})")
        if result.get("approval_id"):
            approval_ids.append(result["approval_id"])
    if approval_ids:
        first = approval_ids[0]
        print(f"Approving {first} as 'it-manager' ->", platform.it.approve(first, "it-manager")["status"])

    print("\n=== FLOW 3: Vendor onboarding ===")
    for result in (platform.vendor.onboard(v) for v in _read_json_file(data / "vendors_sample.json")):
        codes = [i["code"] for i in result.get("issues", [])]
        print(f"{result['legal_name']:30} -> {result['status']:22} {codes or ''}")

    print("\n=== AUDIT ===")
    report = platform.audit.verify()
    print(f"chain ok={report['ok']} entries={report['entries']}")
    return EXIT_OK if report["ok"] else EXIT_TAMPERED


def cmd_reset(args):
    settings = load_settings()
    if not args.yes:
        print(f"This deletes everything in {settings.state_dir}. Re-run with --yes to confirm.")
        return EXIT_ERROR
    shutil.rmtree(settings.state_dir, ignore_errors=True)
    print(f"Deleted {settings.state_dir}")
    return EXIT_OK


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_platform", description="Enterprise Agent Platform CLI")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="ask the knowledge assistant a question")
    p.add_argument("question")
    p.add_argument("--role", required=True, help="employee | engineer | hr | finance | admin")
    p.add_argument("--user", default="cli-user")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("ticket", help="send IT tickets to the IT agent")
    p.add_argument("--file", help="JSON file with one ticket or a list of tickets")
    p.add_argument("--text", help="ticket text (instead of --file)")
    p.add_argument("--id", default="CLI-1")
    p.add_argument("--requester", default="cli-user")
    p.set_defaults(func=cmd_ticket)

    p = sub.add_parser("approvals", help="list / approve / reject pending IT actions")
    a = p.add_subparsers(dest="action", required=True)
    x = a.add_parser("list")
    x.add_argument("--status", choices=["pending", "approved", "rejected", "executed", "failed"])
    x = a.add_parser("approve")
    x.add_argument("approval_id")
    x.add_argument("--approver", required=True)
    x = a.add_parser("reject")
    x.add_argument("approval_id")
    x.add_argument("--approver", required=True)
    x.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("vendor", help="onboard vendors from a JSON file")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_vendor)

    p = sub.add_parser("audit", help="verify or view the audit log")
    a = p.add_subparsers(dest="action", required=True)
    a.add_parser("verify")
    x = a.add_parser("tail")
    x.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("demo", help="run every flow once with sample data")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("reset", help="delete runtime state in ./var")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=None)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "reset":
            return cmd_reset(args)
        platform = build_platform()
        return args.func(platform, args)
    except PlatformError as exc:
        # Expected problem: short message, no scary traceback.
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        # Happens when output is piped into `head` etc. Not an error for us.
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
