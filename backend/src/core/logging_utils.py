"""
Logging and error handling utilities for the Code Knowledge Base.

This module provides centralized logging configuration and error handling
utilities following the standard Python logging framework.
"""

import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Optional, Dict, Any
from .config import get_config

DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Default byte cap when dumping large payloads at DEBUG. 0 = unlimited.
# Override via logging.payload_max_chars in config or CODEKB_LOG_PAYLOAD_MAX env.
DEFAULT_PAYLOAD_MAX_CHARS = 4_000

# Third-party loggers that are chatty by default. They sit at
# logging.third_party_level (WARNING unless set). DEBUG opts into the full boto
# request/response wire trace, strands agent events, and per-request HTTP lines.
_NOISY_LOGGERS = (
    "urllib3",
    "botocore",
    "boto3",
    "s3transfer",
    "httpcore",
    "httpx",
    "strands",
    "mcp",
    "jwt",
)


def _disable_uvicorn_access_log() -> None:
    """Silence ``uvicorn.access``, whatever the exporter.

    ``log_requests`` is the access log and always runs. Detaching and refusing to
    propagate is what uvicorn's own ``--no-access-log`` does.
    """
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False


def _export_uvicorn_startup_logging() -> None:
    """Send uvicorn's startup lines to the sink as well as the console.

    Its own handler is left attached so the console keeps the line naming the bound
    host and port, which is the operator's confirmation that the service came up.
    """
    logging.getLogger("uvicorn").propagate = True


def _announce_log_destination() -> None:
    """Tell the console where the logs went, since they no longer appear there."""
    from .observability import describe_destination

    # Scrubbed like any other record: this bypasses the handlers, so their
    # SecretRedactingFilter never sees it, and endpoints can carry credentials.
    print(
        scrub_secrets(f"Logs are exported to {describe_destination()}, not to this console."),
        file=sys.stderr,
    )


def setup_logging(config: Optional[Dict[str, Any]] = None) -> None:
    """
    Set up logging configuration based on config settings.

    ``OTEL_LOGS_EXPORTER`` selects the sink. ``none`` keeps a text handler on
    stdout; ``otlp`` hands off to the OpenTelemetry handler once it is built, the
    text handler having covered the startup window. Both carry a
    ``SecretRedactingFilter``.

    Args:
        config: Optional logging configuration dictionary
    """
    if config is None:
        config = get_config("logging", {})

    format_string = config.get("format", DEFAULT_LOG_FORMAT)

    # CODEKB_LOG_LEVEL is the only control, and it lives on the root logger.
    # Handlers stay open so a record that passes here reaches the sink.
    log_level = getattr(logging, config.get("level", "INFO").upper(), logging.INFO)

    # Clear existing handlers to avoid duplicates on re-init
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    # One instance shared by every handler — it mutates the record in place, so
    # whichever sink filters first scrubs it for the rest.
    secret_filter = SecretRedactingFilter()

    # Carries records until the OTel handler is built, and is detached once it is.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(format_string))
    console_handler.addFilter(secret_filter)
    root_logger.addHandler(console_handler)

    # Set separately from log_level, since these loggers narrate each HTTP call
    # and AWS request. Set before the exporter is built: constructing it calls
    # CloudWatch, and botocore narrates every step of that call.
    third_party_level = getattr(
        logging, str(config.get("third_party_level", "WARNING")).upper(), logging.WARNING
    )
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(third_party_level)

    # Safe to override uvicorn's config here: it configures logging when Config is
    # constructed and imports this app afterwards, so this runs last.
    _disable_uvicorn_access_log()

    # The exporter is selected by OTEL_LOGS_EXPORTER, which config maps to
    # observability.logs.exporter. It is mandatory: an absent value means the log
    # destination is unknown, so startup stops here.
    otel_exporter = get_config("observability.logs.exporter", None)
    if not otel_exporter:
        raise ConfigurationError(
            "OTEL_LOGS_EXPORTER is not set. CodeKB requires an explicit "
            "OpenTelemetry logs exporter. Set OTEL_LOGS_EXPORTER=otlp (export to "
            "an OTLP endpoint such as CloudWatch) or OTEL_LOGS_EXPORTER=none "
            "(write logs to stdout) in the environment (.env / ECS task "
            "definition) and restart."
        )

    from .observability import ObservabilityError, build_log_handler, selected_exporter

    # Validated here rather than inside the exporter, because none never reaches
    # it: an unrecognised value would otherwise be treated as otlp.
    try:
        otel_exporter = selected_exporter()
    except ObservabilityError as exc:
        raise ConfigurationError(str(exc)) from exc

    # none keeps the text handler above as its sink, so it stays readable and
    # never builds an OpenTelemetry pipeline. otlp hands off to the OTel handler,
    # which renders each record as an OTel log record for the backend.
    if otel_exporter != "none":
        try:
            otel_handler = build_log_handler()
        except ObservabilityError as exc:
            # Refuse to start rather than serve traffic with logs going nowhere.
            # Only startup/config faults reach here; runtime export errors stay
            # non-fatal so a CloudWatch blip cannot take down a healthy task.
            raise ConfigurationError(
                f"OpenTelemetry log export could not be initialised: {exc}"
            ) from exc

        otel_handler.addFilter(secret_filter)
        root_logger.addHandler(otel_handler)

        # The OTel handler is the sink, so hand off to it. Detach without close();
        # the stream is shared.
        root_logger.removeHandler(console_handler)

        # Uvicorn's loggers have propagate=False, so its startup lines would never
        # reach the sink.
        _export_uvicorn_startup_logging()
        _announce_log_destination()

    logging.getLogger("codekb").debug(
        f"Logging initialized: level={logging.getLevelName(log_level)} exporter={otel_exporter}"
    )

    # Log the effective configuration through our own handlers so the
    # summary formats consistently with the rest of the app's output.
    try:
        from .config import get_config_manager

        get_config_manager().log_effective_configuration()
    except Exception as exc:  # pragma: no cover
        logging.getLogger("codekb").debug(f"Skipped config summary logging: {exc}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def _resolve_payload_max_chars() -> int:
    """Max characters per logged payload. 0 means unlimited."""
    env = os.environ.get("CODEKB_LOG_PAYLOAD_MAX")
    if env is not None:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    try:
        cfg = get_config("logging", {}) or {}
    except Exception:  # pragma: no cover — config not initialized yet
        return DEFAULT_PAYLOAD_MAX_CHARS
    return int(cfg.get("payload_max_chars", DEFAULT_PAYLOAD_MAX_CHARS))


def format_payload(payload: Any, *, max_chars: Optional[int] = None) -> str:
    """Format a payload (dict/list/str/bytes) for DEBUG logging.

    - dicts/lists are pretty-printed as JSON (falling back to repr on failure)
    - bytes are decoded best-effort as UTF-8
    - strings pass through
    - output is truncated to ``max_chars`` (or the resolved config default) and
      annotated with original length so you can tell when something was cut

    Callers should still guard with ``logger.isEnabledFor(logging.DEBUG)`` when the
    payload is expensive to compute.
    """
    if max_chars is None:
        max_chars = _resolve_payload_max_chars()

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = repr(payload)
    elif isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, default=str, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(payload)

    if max_chars and len(text) > max_chars:
        return f"{text[:max_chars]}… [truncated, full_len={len(text)}]"
    return text


# Parameter/header names whose values are secrets and must never be logged.
# Covers OAuth/OIDC fields and the API-key headers used by OTLP vendors.
_SENSITIVE_PARAM_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "code",
        "code_verifier",
        "client_secret",
        "authorization",
        "assertion",
        "client_assertion",
        "api-key",
        "x-api-key",
        "x-honeycomb-team",
        "dd-api-key",
    }
)


def redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``params`` with sensitive values masked for logging.

    Use before logging OAuth token exchanges, OTLP headers, or any mapping that
    may carry credentials. Matches on the key, so non-secret entries stay
    readable; values are replaced with a fixed marker, never a prefix of the real
    value. Keys containing ``token``, ``secret``, ``password``, or ``credential``
    are caught too, so an unrecognised vendor's credential header still masks.
    """
    redacted: Dict[str, Any] = {}
    for key, value in params.items():
        lowered = key.lower()
        if lowered in _SENSITIVE_PARAM_KEYS or any(
            marker in lowered for marker in ("token", "secret", "password", "credential")
        ):
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


_REDACTED = "***REDACTED***"

# A value is either quoted — including Python's b'...' repr, which is how botocore
# renders request headers — or a bare run up to the next delimiter. The quoted form
# must come first: matching bare would stop at the opening quote and leave the
# secret behind it intact.
_VALUE = r'(?:b?["\'][^"\']*["\']|[^\s,;&}\]"\']+)'

# Keys may carry their own quotes and a trailing one before the separator, as in
# a dict repr: {'X-Amz-Security-Token': b'...'}.
_SEP = r'["\']?\s*[:=]\s*'


def _build_secret_patterns() -> tuple:
    """Regexes that mask secret values while keeping the key visible."""
    # Longest-first so client_assertion wins over assertion.
    keys = "|".join(sorted((re.escape(k) for k in _SENSITIVE_PARAM_KEYS), key=len, reverse=True))
    return (
        # Authorization consumes the whole value, scheme included, or
        # "Authorization: Bearer <jwt>" would mask only the word "Bearer".
        re.compile(rf'(?i)(\b["\']?authorization{_SEP})(?:b?["\'][^"\']*["\']|[^,;}}\]"\']+)'),
        # Known key names. \b stops status_code from matching code.
        re.compile(rf'(?i)(["\']?\b(?:{keys})\b{_SEP}){_VALUE}'),
        # secret / password / credential are never benign in a key name, so they
        # match anywhere within it — aws_secret_access_key included.
        re.compile(rf"(?i)(\b[\w.\-]*(?:secret|password|credential)[\w.\-]*{_SEP}){_VALUE}"),
        # Key-shaped names. token(?!s)\b keeps input_tokens, max_tokens and
        # token_usage readable; the *_key forms allow a suffix so
        # aws_access_key_id is covered, matching _SECRET_SUFFIXES in config.
        re.compile(
            rf"(?i)(\b[\w.\-]*(?:token(?!s)\b|(?:api|access|private|signing)[_-]?key[\w.\-]*)"
            rf"{_SEP}){_VALUE}"
        ),
        # SigV4 Credential= / Signature=, and botocore's "Signature:\n<hex>".
        re.compile(rf"(?i)(\b(?:credential|signature)\s*[:=]\s*){_VALUE}"),
        # Bare "Bearer <token>" with no preceding key.
        re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
        # URL userinfo: https://user:pass@host. No key name to match on, so the
        # trailing @ identifies it — and stays a lookahead, since the substitution
        # keeps only group 1 and would otherwise eat the @ and join the two halves.
        # The lookahead is also what spares https://host:8080/path.
        re.compile(r"(://[^/@:\s]*:)[^/@\s]+(?=@)"),
    )


_SECRET_PATTERNS = _build_secret_patterns()


def scrub_secrets(text: str) -> str:
    """Mask credential values found anywhere in a log line.

    Every pattern runs on every record. An earlier substring pre-check saved
    ~3 us per record but silently disabled any pattern whose trigger word it
    did not list, which let real credentials through three separate times.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + _REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Strip credential values from every record before it is emitted.

    Attached to the handlers rather than the root logger: logger-level filters
    only see records created on that logger, so third-party output (botocore,
    strands, httpx) would bypass them. Handler-level filters see everything that
    reaches the sink, including the OpenTelemetry export.

    Covers the message, its args, and attached exception tracebacks.

    Credentials are recognised as delimited pairs — ``key=value`` or ``key: value``
    — plus bare ``Bearer <token>``. A secret logged as undelimited prose, such as
    ``logger.info("token %s", tok)``, is not matched; widening that would mask
    ordinary text like ``token usage``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            original = record.getMessage()
            scrubbed = scrub_secrets(original)
            if scrubbed != original:
                record.msg = scrubbed
                record.args = ()

            # A traceback object cannot be rewritten, so when it carries a secret
            # the rendered text is scrubbed and moved onto the message. botocore and
            # urllib3 exceptions quote signed URLs and auth headers. Left in place
            # when clean, so the exporter keeps its structured exception fields.
            if record.exc_info:
                rendered = "".join(traceback.format_exception(*record.exc_info))
                safe = scrub_secrets(rendered)
                if safe != rendered:
                    record.exc_info = None
                    record.exc_text = None
                    record.msg = f"{record.getMessage()}\n{safe}"
                    record.args = ()

            # The OpenTelemetry handler exports every attribute on the record, so
            # values passed through ``extra={...}`` reach the sink without ever
            # appearing in the message. Scrub them in place.
            for key, value in list(record.__dict__.items()):
                if isinstance(value, str):
                    safe_value = scrub_secrets(value)
                    if safe_value != value:
                        record.__dict__[key] = safe_value
        except Exception:  # never let redaction drop a log record
            pass
        return True


class CodeKnowledgeBaseError(Exception):
    """Base exception class for Code Knowledge Base errors."""

    def __init__(self, message: str, error_code: Optional[str] = None):
        """
        Initialize the error.

        Args:
            message: Error message
            error_code: Optional error code for categorization
        """
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class ConfigurationError(CodeKnowledgeBaseError):
    """Exception raised for configuration-related errors."""

    pass


class ParseError(CodeKnowledgeBaseError):
    """Exception raised for code parsing errors."""

    def __init__(
        self, message: str, file_path: Optional[Path] = None, line_number: Optional[int] = None
    ):
        """
        Initialize the parse error.

        Args:
            message: Error message
            file_path: Optional file path where error occurred
            line_number: Optional line number where error occurred
        """
        super().__init__(message, "PARSE_ERROR")
        self.file_path = file_path
        self.line_number = line_number

    def __str__(self) -> str:
        """Return formatted error message."""
        if self.file_path and self.line_number:
            return f"{self.message} (at {self.file_path}:{self.line_number})"
        elif self.file_path:
            return f"{self.message} (in {self.file_path})"
        return self.message


class AnalysisError(CodeKnowledgeBaseError):
    """Exception raised for analysis-related errors."""

    pass


class StorageError(CodeKnowledgeBaseError):
    """Exception raised for storage/persistence errors."""

    pass


def log_error(logger: logging.Logger, error: Exception, context: Optional[str] = None) -> None:
    """
    Log an error with appropriate context information.

    Args:
        logger: Logger instance to use
        error: Exception to log
        context: Optional context information
    """
    error_msg = str(error)

    if context:
        error_msg = f"{context}: {error_msg}"

    if isinstance(error, CodeKnowledgeBaseError):
        if error.error_code:
            error_msg = f"[{error.error_code}] {error_msg}"

    logger.error(error_msg, exc_info=True)


def handle_exception(logger: logging.Logger, error: Exception, operation: str) -> None:
    """
    Handle an exception with consistent logging and re-raising.

    Args:
        logger: Logger instance to use
        error: Exception that occurred
        operation: Description of the operation that failed
    """
    log_error(logger, error, f"Failed to {operation}")

    # Re-raise as appropriate CodeKnowledgeBase error if not already
    if isinstance(error, CodeKnowledgeBaseError):
        raise error
    elif isinstance(error, FileNotFoundError):
        raise StorageError(f"File not found during {operation}: {error}")
    elif isinstance(error, PermissionError):
        raise StorageError(f"Permission denied during {operation}: {error}")
    elif isinstance(error, ValueError):
        raise AnalysisError(f"Invalid value during {operation}: {error}")
    else:
        raise CodeKnowledgeBaseError(f"Unexpected error during {operation}: {error}")


class ErrorContext:
    """Context manager for consistent error handling."""

    def __init__(self, logger: logging.Logger, operation: str):
        """
        Initialize error context.

        Args:
            logger: Logger instance to use
            operation: Description of the operation
        """
        self.logger = logger
        self.operation = operation

    def __enter__(self):
        """Enter the error context."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the error context and handle any exceptions."""
        if exc_type is not None:
            handle_exception(self.logger, exc_val, self.operation)
        return False  # Don't suppress the exception
