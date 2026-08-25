"""Structured logging for lunad.

One JSON object per line, rotated by stdlib RotatingFileHandler. JSON because
the log is meant to be greppable *and* machine-readable: Phase 3's bar widget
and the consolidation pass both want to read it without a parser.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from typing import Any

from . import config

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Anything passed via ``extra=`` lands as a top-level key, so call sites can
    do ``log.info("ask", extra={"req_id": rid, "chars": n})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _safe(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return str(value)


def safe_extra(payload: dict[str, Any]) -> dict[str, Any]:
    """Make a dict safe to pass as ``extra=``.

    ``logging.makeRecord`` raises KeyError if an extra key shadows a LogRecord
    attribute (``message``, ``args``, ``name``, ...). Structured payloads built
    from exceptions routinely contain ``message``, so they are prefixed rather
    than dropped: losing the field would be worse than renaming it.
    """
    return {(f"x_{k}" if k in _RESERVED else k): v for k, v in payload.items()}


def setup(level: int = logging.INFO, stderr: bool = True) -> logging.Logger:
    """Configure the root logger. Safe to call twice (handlers are replaced)."""
    config.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_PATH,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if stderr:
        # systemd captures stderr into the journal; keep it human-readable.
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)

    return logging.getLogger("lunad")
