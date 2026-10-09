"""CLI: exit codes are a contract for scripts and CI - test them."""

import json

import pytest

from agent_platform.cli import EXIT_ERROR, EXIT_OK, EXIT_TAMPERED, main


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_MODE", "offline")
    return tmp_path


def test_ask_ok_and_bad_role(capsys):
    assert main(["ask", "How many PTO days do I get?", "--role", "employee"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "answered"
    assert main(["ask", "hi", "--role", "ceo"]) == EXIT_ERROR


def test_missing_file_is_a_clean_error(capsys):
    assert main(["vendor", "--file", "does-not-exist.json"]) == EXIT_ERROR
    assert "file not found" in capsys.readouterr().err


def test_tampering_gives_exit_code_3(isolated_state):
    assert main(["ticket", "--text", "VPN keeps disconnecting", "--requester", "bob"]) == EXIT_OK
    assert main(["audit", "verify"]) == EXIT_OK
    audit = isolated_state / "audit.jsonl"
    audit.write_text(audit.read_text().replace("bob", "mallory"))
    assert main(["audit", "verify"]) == EXIT_TAMPERED
