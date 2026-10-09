"""
conftest.py - shared pytest fixtures.

Every test gets its OWN temporary state folder (tmp_path), so tests never
touch ./var and can run in any order, in parallel, on any machine.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

# Make "import agent_platform" work even if the package is not pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_platform.app import build_platform  # noqa: E402
from agent_platform.config import load_settings  # noqa: E402

FIXED_TODAY = date(2026, 10, 6)  # keeps insurance-expiry tests stable forever


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={"AGENT_MODE": "offline", "AGENT_STATE_DIR": str(tmp_path)}, use_dotenv=False)


@pytest.fixture
def platform(settings):
    return build_platform(settings, today=FIXED_TODAY)
