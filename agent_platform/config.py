"""
config.py - one place for every setting.

All settings come from environment variables (or a local .env file), so you
never edit code to switch between offline mode and Claude mode.

  AGENT_MODE                 offline (default) | claude
  ANTHROPIC_API_KEY          required only when AGENT_MODE=claude
  CLAUDE_MODEL               model name used in Claude mode
  AGENT_STATE_DIR            where runtime files go (audit log, approvals...). Default ./var
  MIN_RETRIEVAL_SCORE        knowledge assistant declines below this similarity (0..1)
  MIN_CLASSIFIER_CONFIDENCE  IT tickets below this confidence go to a human (0..1)

NOTE: we parse .env by hand (a few lines) instead of adding python-dotenv,
      to keep the dependency list tiny.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

# Folder that contains agent_platform/, data/, tests/ ...
PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_MODES = ("offline", "claude")
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


@dataclass
class Settings:
    mode: str = "offline"
    anthropic_api_key: str = ""
    claude_model: str = DEFAULT_CLAUDE_MODEL
    data_dir: Path = PROJECT_ROOT / "data"
    state_dir: Path = PROJECT_ROOT / "var"
    min_retrieval_score: float = 0.10
    min_classifier_confidence: float = 0.60
    llm_max_retries: int = 2
    llm_timeout_seconds: float = 30.0

    # ---- derived file paths (kept here so nobody hard-codes paths) ----
    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def approvals_file(self) -> Path:
        return self.state_dir / "approvals.json"

    @property
    def vendor_registry_file(self) -> Path:
        # Runtime copy. Seeded from data/vendor_registry.json on first run,
        # so running the demo never edits the files you committed to git.
        return self.state_dir / "vendor_registry.json"

    @property
    def documents_file(self) -> Path:
        return self.data_dir / "documents.json"

    @property
    def watchlist_file(self) -> Path:
        return self.data_dir / "watchlist.json"

    @property
    def registry_seed_file(self) -> Path:
        return self.data_dir / "vendor_registry.json"


def _read_dotenv(path: Path) -> dict:
    """Very small .env reader: KEY=VALUE per line, '#' comments, optional quotes."""
    values = {}
    if not path.exists():
        return values
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f".env line {line_no} has no '=': {raw!r}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_fraction(env: dict, name: str, default: float) -> float:
    """Read a number between 0 and 1, with a clear error if it is wrong."""
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number between 0 and 1, got {raw!r}")
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"{name} must be between 0 and 1, got {value}")
    return value


def load_settings(env: dict = None, use_dotenv: bool = True) -> Settings:
    """
    Build a Settings object.

    env: pass a dict in tests so we never depend on the real machine's env.
         Real environment variables win over values found in .env.
    """
    merged = {}
    if use_dotenv:
        merged.update(_read_dotenv(PROJECT_ROOT / ".env"))
    merged.update(dict(os.environ) if env is None else env)

    mode = merged.get("AGENT_MODE", "offline").strip().lower()
    if mode not in VALID_MODES:
        raise ConfigError(f"AGENT_MODE must be one of {VALID_MODES}, got {mode!r}")

    api_key = merged.get("ANTHROPIC_API_KEY", "").strip()
    if mode == "claude" and not api_key:
        raise ConfigError(
            "AGENT_MODE=claude but ANTHROPIC_API_KEY is empty. "
            "Set the key, or use AGENT_MODE=offline."
        )

    settings = Settings(
        mode=mode,
        anthropic_api_key=api_key,
        claude_model=(merged.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL).strip(),
        min_retrieval_score=_parse_fraction(merged, "MIN_RETRIEVAL_SCORE", Settings.min_retrieval_score),
        min_classifier_confidence=_parse_fraction(
            merged, "MIN_CLASSIFIER_CONFIDENCE", Settings.min_classifier_confidence
        ),
    )

    state_dir = merged.get("AGENT_STATE_DIR", "").strip()
    if state_dir:
        settings.state_dir = Path(state_dir).expanduser().resolve()

    # Make sure the state folder exists. Fail loudly if we cannot create it.
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot create state folder {settings.state_dir}: {exc}")

    return settings
