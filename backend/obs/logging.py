"""Structured JSON logging (SDD §4).

Every log line — ours and Pipecat's, since both use the global loguru
logger — becomes one JSON object on stdout:

    {"ts", "severity", "message", "component", ...extra fields}

`severity` follows GCP Cloud Logging's field convention, so a later deploy
to Cloud Run/GKE indexes log levels with zero code changes. Locally,
`docker compose logs` shows the same JSON.

Extra fields (session_id, turn_id, event, duration_ms, ...) are attached
with `logger.bind(...)` at call sites and land at the top level of the line.
"""

import json
import os
import sys

from loguru import logger

# loguru level name -> Cloud Logging severity
_SEVERITY = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


def _sink(message) -> None:
    record = message.record
    line = {
        "ts": record["time"].isoformat(),
        "severity": _SEVERITY.get(record["level"].name, "DEFAULT"),
        "message": record["message"],
        "component": record["extra"].get("component", record["name"]),
    }
    for key, value in record["extra"].items():
        if key != "component":
            line[key] = value
    if record["exception"]:
        line["exception"] = str(record["exception"])
    print(json.dumps(line, default=str), file=sys.stdout, flush=True)


def setup_logging() -> None:
    """Replace loguru's default sink with the JSON sink. Call once at boot."""
    logger.remove()
    logger.add(_sink, level=os.getenv("LOG_LEVEL", "DEBUG"))
