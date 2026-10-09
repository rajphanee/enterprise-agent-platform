"""
audit.py - append-only, hash-chained audit log.

HOW THE CHAIN WORKS
-------------------
Each line in var/audit.jsonl is one JSON entry:

    {"seq": 3, "ts": "...", "agent": "it_agent", "action": "ticket_routed",
     "actor": "bob", "details": {...},
     "prev_hash": "<hash of entry 2>", "hash": "<sha256 of this entry>"}

  - "hash" is SHA-256 over every field of the entry except "hash" itself.
  - "prev_hash" is the hash of the entry before it (entry 1 uses 64 zeros).

So if anyone EDITS an old line, its hash no longer matches.
If anyone DELETES a middle line, the next line's prev_hash points at nothing.
If anyone DELETES the last lines, the separate "audit.head" file (which always
holds the latest seq + hash) no longer matches the file.

NOTE / LIMITATION:
  This detects tampering; it does not prevent it. Someone who can rewrite BOTH
  files and recompute every hash can fake history. In production you would ship
  the head hash somewhere the attacker cannot write (WORM bucket, SIEM, etc.).
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .errors import AuditIntegrityError

GENESIS_HASH = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_hash(entry: dict) -> str:
    """SHA-256 of the entry (minus its own 'hash' field) in a stable JSON form."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    # sort_keys + fixed separators => same dict always gives the same bytes
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.head_path = self.path.with_suffix(".head")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One lock per AuditLog object. The vendor agent writes from threads,
        # so without this two entries could get the same seq number.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ read
    def read_entries(self) -> list:
        """Return every entry. Raises AuditIntegrityError if a line is not JSON."""
        if not self.path.exists():
            return []
        entries = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise AuditIntegrityError(f"audit line {line_no} is not valid JSON: {exc}")
        return entries

    def tail(self, count: int = 10) -> list:
        if count <= 0:
            return []
        return self.read_entries()[-count:]

    # ----------------------------------------------------------------- write
    def record(self, agent: str, action: str, actor: str = "system", details: dict = None) -> dict:
        """Append one entry and return it."""
        with self._lock:
            # NOTE: reading the whole file each time is O(n). Fine for a demo
            # (thousands of lines). For millions, keep the head in memory/DB.
            entries = self.read_entries()
            previous = entries[-1] if entries else None

            entry = {
                "seq": (previous["seq"] + 1) if previous else 1,
                "ts": _utc_now(),
                "agent": agent,
                "action": action,
                "actor": actor or "unknown",
                "details": details or {},
                "prev_hash": previous["hash"] if previous else GENESIS_HASH,
            }
            # Round-trip through JSON first so things like tuples/dates become
            # exactly what verify() will later read back from disk.
            entry = json.loads(json.dumps(entry, default=str, ensure_ascii=False))
            entry["hash"] = compute_hash(entry)

            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # make sure it is really on disk

            self._write_head(entry["seq"], entry["hash"])
            return entry

    def _write_head(self, seq: int, hash_value: str) -> None:
        tmp = self.head_path.with_suffix(".head.tmp")
        tmp.write_text(json.dumps({"seq": seq, "hash": hash_value}), encoding="utf-8")
        os.replace(tmp, self.head_path)  # atomic swap, never a half-written file

    # ---------------------------------------------------------------- verify
    def verify(self) -> dict:
        """
        Walk the whole chain. Never raises for tampering - it returns a report:
            {"ok": bool, "entries": int, "problems": [str, ...]}
        """
        problems = []
        try:
            entries = self.read_entries()
        except AuditIntegrityError as exc:
            return {"ok": False, "entries": 0, "problems": [str(exc)]}

        expected_prev = GENESIS_HASH
        for index, entry in enumerate(entries):
            expected_seq = index + 1
            seq = entry.get("seq")
            if seq != expected_seq:
                problems.append(f"entry #{expected_seq}: seq is {seq} (missing or reordered entries)")
            if entry.get("prev_hash") != expected_prev:
                problems.append(f"entry seq={seq}: prev_hash does not link to the previous entry")
            if entry.get("hash") != compute_hash(entry):
                problems.append(f"entry seq={seq}: content was modified (hash mismatch)")
            expected_prev = entry.get("hash")

        # Tail-truncation check against the head file.
        if self.head_path.exists():
            try:
                head = json.loads(self.head_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                problems.append("audit.head is corrupted")
                head = None
            if head is not None:
                last = entries[-1] if entries else {"seq": 0, "hash": GENESIS_HASH}
                if head.get("seq") != last.get("seq") or head.get("hash") != last.get("hash"):
                    problems.append(
                        f"head says last entry is seq={head.get('seq')} but log ends at "
                        f"seq={last.get('seq')} (entries removed from the end?)"
                    )
        elif entries:
            problems.append("audit.head is missing but the log has entries")

        return {"ok": not problems, "entries": len(entries), "problems": problems}
