"""
errors.py - all custom exceptions live here.

WHY: If every module raises its own random exception (ValueError, KeyError, ...)
the CLI cannot tell "user typed something wrong" apart from "our code is broken".
So every *expected* failure inherits from PlatformError. The CLI catches
PlatformError, prints a friendly message and exits with code 1. Anything else
is a real bug and is allowed to crash with a full traceback.
"""


class PlatformError(Exception):
    """Base class for every expected error in this project."""

    # Short machine-friendly code. Shows up in CLI output and audit entries.
    code = "PLATFORM_ERROR"


class ConfigError(PlatformError):
    """Bad or missing settings (env vars, .env file, missing package)."""
    code = "CONFIG_ERROR"


class ValidationError(PlatformError):
    """Input from the user / caller is wrong (missing field, bad format...)."""
    code = "VALIDATION_ERROR"


class NotFoundError(PlatformError):
    """Something was looked up by id and does not exist."""
    code = "NOT_FOUND"


class PolicyError(PlatformError):
    """A governance rule blocked the action (e.g. approving your own ticket)."""
    code = "POLICY_VIOLATION"


class StorageError(PlatformError):
    """A state file (approvals, registry, documents) could not be read or written."""
    code = "STORAGE_ERROR"


class LLMError(PlatformError):
    """The language model call failed or returned something unusable."""
    code = "LLM_ERROR"


class AutomationError(PlatformError):
    """An automated IT action (password reset, etc.) failed while running."""
    code = "AUTOMATION_ERROR"


class AuditIntegrityError(PlatformError):
    """The audit log file is unreadable (e.g. a line is not valid JSON)."""
    code = "AUDIT_INTEGRITY_ERROR"
