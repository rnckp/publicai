"""Metadata-only diagnostics with explicit telemetry opt-in."""

import json
import logging
import os
from datetime import UTC, datetime

from rich.console import Console
from rich.logging import RichHandler

from publicai.config import TelemetrySettings


class JsonFormatter(logging.Formatter):
    """Format internal event names without serializing exception payloads or locals."""

    def format(self, record: logging.LogRecord) -> str:
        """Return a credential-safe JSON log line."""
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "exception_type": getattr(record, "error_type", None),
        }
        for key in (
            "discovery_id",
            "claim_path",
            "review_status",
            "citation_count",
            "source_ids",
            "claim_count",
            "review_counts",
            "unexpected_checks",
            "blocking_issues",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


class CliLogFormatter(logging.Formatter):
    """Show concise internal events beside Rich progress output."""

    def format(self, record: logging.LogRecord) -> str:
        """Avoid raw JSON and untrusted exception text in the terminal."""
        labels = {
            "discovery_completed": "Discovery completed",
            "discovery_failed": "Discovery failed",
        }
        label = labels.get(record.getMessage(), record.getMessage().replace("_", " ").title())
        error_type = getattr(record, "error_type", None)
        return f"{label} ({error_type})" if error_type else label


class CliLogFilter(logging.Filter):
    """Keep review events in the CLI progress trace instead of printing them twice."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a log needs a separate Rich line."""
        return not record.getMessage().startswith("evidence_review_")


def configure(settings: TelemetrySettings, *, console: Console | None = None) -> None:
    """Enable stderr application logging and optional content-free AI spans.

    Standard OTEL_EXPORTER_OTLP_* environment settings can send spans to a
    local Aspire collector when telemetry is explicitly enabled.
    """
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    handler = (
        RichHandler(
            console=console,
            show_time=False,
            show_level=False,
            show_path=False,
            markup=False,
            highlighter=None,
        )
        if console is not None
        else logging.StreamHandler()
    )
    handler.setFormatter(CliLogFormatter() if console is not None else JsonFormatter())
    if console is not None:
        handler.addFilter(CliLogFilter())
    logger = logging.getLogger("publicai")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Third-party request/exception logs can contain URLs, prompts, or credentials.
    for name in (
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "openai",
        "pydantic_ai",
        "exa_py",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if settings.enabled:
        import logfire

        logfire.configure(
            service_name=settings.service_name,
            service_version="0.1.0",
            send_to_logfire=settings.send_to_logfire,
            console=False,
            inspect_arguments=False,
        )
        logfire.instrument_pydantic_ai(
            include_content=False,
            include_binary_content=False,
            include_model_request_parameters=False,
        )
