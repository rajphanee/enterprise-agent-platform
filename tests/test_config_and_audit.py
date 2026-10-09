"""Tests for settings loading and the hash-chained audit log."""

import json

import pytest

from agent_platform.audit import AuditLog
from agent_platform.config import load_settings
from agent_platform.errors import ConfigError


# ----------------------------------------------------------------- config
def test_default_mode_is_offline(tmp_path):
    s = load_settings(env={"AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)
    assert s.mode == "offline"


def test_claude_mode_without_key_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env={"AGENT_MODE": "claude", "AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)


def test_bad_mode_and_bad_threshold_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(env={"AGENT_MODE": "gpt", "AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)
    with pytest.raises(ConfigError):
        load_settings(env={"MIN_RETRIEVAL_SCORE": "abc", "AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)
    with pytest.raises(ConfigError):
        load_settings(env={"MIN_CLASSIFIER_CONFIDENCE": "1.5", "AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)


# ------------------------------------------------------------------ audit
def _log_with_entries(tmp_path, count=3):
    log = AuditLog(tmp_path / "audit.jsonl")
    for n in range(count):
        log.record("test", "event", "tester", {"n": n})
    return log


def test_clean_chain_verifies(tmp_path):
    log = _log_with_entries(tmp_path)
    report = log.verify()
    assert report == {"ok": True, "entries": 3, "problems": []}


def test_edited_entry_is_detected(tmp_path):
    log = _log_with_entries(tmp_path)
    lines = log.path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["details"]["n"] = 999  # quietly change history
    lines[1] = json.dumps(entry)
    log.path.write_text("\n".join(lines) + "\n")

    report = log.verify()
    assert not report["ok"]
    assert any("modified" in p for p in report["problems"])


def test_deleted_middle_entry_is_detected(tmp_path):
    log = _log_with_entries(tmp_path)
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    assert not log.verify()["ok"]


def test_deleted_last_entry_is_detected_by_head_file(tmp_path):
    log = _log_with_entries(tmp_path)
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n")  # chain itself still links fine
    report = log.verify()
    assert not report["ok"]
    assert any("removed from the end" in p for p in report["problems"])


def test_garbage_line_reports_instead_of_crashing(tmp_path):
    log = _log_with_entries(tmp_path)
    with log.path.open("a") as fh:
        fh.write("this is not json\n")
    report = log.verify()
    assert not report["ok"]
