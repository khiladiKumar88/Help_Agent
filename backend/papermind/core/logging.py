"""Structured JSON logging with secret redaction.

Every record passes through RedactingFilter, which removes configured secret values and
common credential patterns before anything is written.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|pin|totp|authorization)(\s*[=:]\s*)([^\s,;&\"']+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
]
REDACTED = "***REDACTED***"

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, REDACTED)
        text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", text)  # bearer first
        text = _PATTERNS[0].sub(
            lambda m: (
                m.group(0)
                if m.group(3) == REDACTED or m.group(3).lower() == "bearer"
                else f"{m.group(1)}{m.group(2)}{REDACTED}"
            ),
            text,
        )
        return text

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: (REDACTED if _is_secret_key(k) else self.redact_obj(v)) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact_obj(v) for v in obj]
        return obj


_SECRET_KEY = re.compile(r"(^|[_-])(api_?key|secret|password|passwd|token|totp|pin|authorization)$", re.I)


def _is_secret_key(key: object) -> bool:
    """Exact-ish match so e.g. 'tokens_used' or 'pinned' are not masked, but 'access_token' is."""
    return bool(_SECRET_KEY.search(str(key)))


REDACTOR = Redactor()


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = REDACTOR.redact(record.getMessage())
        record.args = ()
        for key, val in list(record.__dict__.items()):
            if key not in _STD_ATTRS:
                record.__dict__[key] = REDACTED if _is_secret_key(key) else REDACTOR.redact_obj(val)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _STD_ATTRS:
                out[key] = val
        if record.exc_info:
            out["exc"] = REDACTOR.redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    REDACTOR.set_secrets(secrets or [])
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("ccxt", "urllib3", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
