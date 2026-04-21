"""
Structured JSON logging config. Enabled when env JSON_LOGS=1.

Each log record becomes one JSON line, safe for CloudWatch / Datadog / Loki
ingestion. Extra fields attached via `logger.info("...", extra={"foo": "bar"})`
land at the top level of the JSON object.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
        return json.dumps(payload)


def configure_logging() -> None:
    root = logging.getLogger()
    if os.getenv("JSON_LOGS") == "1":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
