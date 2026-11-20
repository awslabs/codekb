"""OpenTelemetry logs wiring.

Bridges the standard library ``logging`` output into an OpenTelemetry
``LoggingHandler`` and exports OTLP logs.

Configuration follows the app's central pattern: ``config`` is the single place
that loads the environment, and this module reads every value via ``get_config``.
The settings live under ``observability.logs.*`` and are populated by ``config``
from the **standard ``OTEL_*`` environment variables** (see ``_ENV_OVERRIDES`` in
``config.py``) — the same vars the OpenTelemetry SDK and other in-process OTel
libraries (e.g. Strands) read from the environment, so one set of standard
variables configures the whole process. There are no ``observability`` keys in
``default.yaml``: it is env-only. The exporter must be selected explicitly via
``OTEL_LOGS_EXPORTER`` — ``setup_logging`` refuses to start the app when it is
unset (there is no silent default).

- ``observability.logs.exporter``     (OTEL_LOGS_EXPORTER)             — ``none`` / ``otlp``
- ``observability.logs.endpoint``     (OTEL_EXPORTER_OTLP_LOGS_ENDPOINT)
- ``observability.logs.protocol``     (OTEL_EXPORTER_OTLP_[LOGS_]PROTOCOL) — ``http/protobuf`` / ``grpc``
- ``observability.logs.headers``      (OTEL_EXPORTER_OTLP_[LOGS_]HEADERS)
- ``observability.logs.service_name`` (OTEL_SERVICE_NAME)
- ``observability.logs.resource_attributes`` (OTEL_RESOURCE_ATTRIBUTES)
- ``observability.logs.cloudwatch_stream_rotation_minutes``
  (OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES) — CloudWatch only; minutes per log
  stream, ``0`` disables rotation. Default 1440. Not a standard OTel variable.

For CloudWatch, ``x-aws-log-stream`` is a stream **prefix**: the exporter appends
the rotation window and a per-container id and creates the stream itself.

Design (see ``otel-phase1-plan.md``):

- **Always on, fail-safe.** The handler is built unconditionally; any import or
  setup failure is logged once and swallowed, so app logging is never disrupted.
- **Once per process.** The ``LoggerProvider`` is a module-level singleton so
  repeated ``setup_logging()`` calls (API + MCP paths) don't leak providers.
- **CloudWatch direct** uses the AWS Distro (ADOT) SigV4-signing log exporter
  authenticated from the task role (HTTP). Any other endpoint uses the standard
  OTLP exporter — HTTP/protobuf by default, or gRPC when the protocol is ``grpc``.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import warnings
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional, Sequence

from .config import get_config

if TYPE_CHECKING:  # imported lazily at runtime to keep OTel optional
    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter

logger = logging.getLogger("codekb.observability")


class ObservabilityError(RuntimeError):
    """Log export cannot be set up as configured.

    Raised only during startup, where refusing to boot is preferable to running
    a service whose logs go nowhere. Runtime export failures stay non-fatal.
    """


# Module-level singleton — built once per process.
_provider: "Optional[LoggerProvider]" = None


# Matches a CloudWatch Logs OTLP endpoint, capturing the region:
#   https://logs.<region>.amazonaws.com/v1/logs
_CW_ENDPOINT_RE = re.compile(r"https?://logs\.([a-z0-9-]+)\.amazonaws\.com", re.IGNORECASE)

# Daily.
_DEFAULT_STREAM_ROTATION_MINUTES = 1440

# CloudWatch rejects ':' and '*' in stream names; '/' is the usual separator.
_STREAM_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9_./#-]")


def _cfg(key: str, default: str = "") -> str:
    """Read observability.logs.<key> from the central config as a string."""
    try:
        value = get_config(f"observability.logs.{key}", default)
    except Exception as exc:
        logger.debug("observability.logs.%s unreadable, using default: %s", key, exc)
        return default
    return default if value is None else str(value)


def _parse_kv(raw: str) -> Dict[str, str]:
    """Parse a ``k=v,k=v`` string (headers / resource attributes) into a dict."""
    out: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def _service_name() -> str:
    return _cfg("service_name", "codekb") or "codekb"


def selected_exporter() -> str:
    """Return the selected exporter: ``none`` or ``otlp``.

    Those are the only accepted values, and exactly one of them — matching the
    standard OpenTelemetry ``OTEL_LOGS_EXPORTER`` selector values (``none``
    disables export; see the OTel SDK spec). Anything else — empty, a comma
    list, an unrecognised name — is a fault, so logs cannot end up somewhere the
    operator did not choose.
    """
    raw = _cfg("exporter", "") or ""
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if len(names) != 1 or names[0] not in ("none", "otlp"):
        raise ObservabilityError(
            f"OTEL_LOGS_EXPORTER must be exactly one of none or otlp, got {raw!r}."
        )
    return names[0]


def describe_destination() -> str:
    """One-line description of where logs are going, for the boot notice."""
    endpoint = _cfg("endpoint", "")
    match = _CW_ENDPOINT_RE.match(endpoint)
    if match:
        headers = _parse_kv(_cfg("headers", ""))
        group = headers.get("x-aws-log-group", "?")
        return f"CloudWatch Logs group {group} in {match.group(1)}"
    return f"OTLP endpoint {endpoint}" if endpoint else "an unknown destination"


def _rotation_minutes() -> int:
    """Minutes per log stream. 0 disables time-based rotation."""
    try:
        raw = get_config(
            "observability.logs.cloudwatch_stream_rotation_minutes",
            _DEFAULT_STREAM_ROTATION_MINUTES,
        )
        minutes = int(raw)
    except Exception as exc:
        logger.warning(
            "OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES unusable, falling back to %s: %s",
            _DEFAULT_STREAM_ROTATION_MINUTES,
            exc,
        )
        return _DEFAULT_STREAM_ROTATION_MINUTES
    return max(0, minutes)


def _instance_id() -> str:
    """Per-container id for the stream name, stable across restarts.

    The ECS metadata URI's last segment is the container id, so no HTTP call is
    needed; elsewhere the hostname serves (in Docker it is the container id).
    """
    metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4") or os.getenv(
        "ECS_CONTAINER_METADATA_URI"
    )
    if metadata_uri:
        candidate = metadata_uri.rstrip("/").rsplit("/", 1)[-1]
        if candidate:
            return candidate[:32]
    try:
        return (socket.gethostname() or "unknown")[:32]
    except Exception:
        return "unknown"


def _stream_window(minutes: int, now: Optional[datetime] = None) -> str:
    """Path segment for the current rotation period; empty when disabled.

    Daily or coarser yields ``2026/09/09``, finer appends the window start.
    """
    if minutes <= 0:
        return ""
    now = now or datetime.now(timezone.utc)
    # Epoch-floored, not wall-clock: keeps multi-day intervals on their real
    # period and avoids a midnight break when the interval doesn't divide 24h.
    floored_epoch_minutes = (int(now.timestamp()) // 60 // minutes) * minutes
    window_start = datetime.fromtimestamp(floored_epoch_minutes * 60, tz=timezone.utc)
    if minutes % 1440 == 0:
        return window_start.strftime("%Y/%m/%d")
    return window_start.strftime("%Y/%m/%d/%H-%M")


def _resolve_stream_name(prefix: str, minutes: int) -> str:
    """Compose ``<prefix>/<window>/<instance>`` and sanitise it for CloudWatch."""
    parts = [prefix.strip("/"), _stream_window(minutes), _instance_id()]
    name = "/".join(part for part in parts if part)
    return _STREAM_NAME_INVALID_RE.sub("-", name)[:512]


def _ensure_log_stream(
    logs_client: object, log_group: str, log_stream: str, fatal: bool = False
) -> None:
    """Create the log stream, tolerating one that already exists.

    Needs ``logs:CreateLogStream``, already granted by ``logGroup.grantWrite``.

    ``fatal`` is set for the startup call, where this doubles as a live probe
    that logs can actually be written: a wrong log group, missing IAM, or bad
    region surfaces here rather than as silently discarded logs. Rotation calls
    it with ``fatal=False`` so a mid-flight failure never kills a serving task.
    """
    try:
        logs_client.create_log_stream(  # type: ignore[attr-defined]
            logGroupName=log_group, logStreamName=log_stream
        )
    except Exception as exc:
        # Already-exists is the normal case on restart; anything else matters. The
        # error code is the stable contract — botocore builds its exception classes
        # dynamically, so the class name is not one.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "ResourceAlreadyExistsException":
            return
        message = f"cannot create CloudWatch log stream {log_group}/{log_stream}: {exc}"
        if fatal:
            raise ObservabilityError(message) from exc
        logger.warning("Log stream rotation: %s", message)


def _build_rotating_cw_exporter_class() -> type:
    """Build the rotating exporter class (needs ADOT imported first)."""
    from amazon.opentelemetry.distro.exporter.otlp.aws.logs.otlp_aws_log_record_exporter import (  # type: ignore[import-untyped]  # noqa: E501
        OTLPAwsLogRecordExporter,
    )

    class RotatingCloudWatchLogExporter(OTLPAwsLogRecordExporter):
        """CloudWatch OTLP exporter that creates and rotates its own log stream.

        Streams are ``<prefix>/<window>/<instance>`` so each container gets its
        own. Required, not cosmetic: neither the endpoint nor ADOT creates a
        stream, and a shared one would interleave concurrent tasks.
        """

        def __init__(
            self,
            *args: object,
            stream_prefix: str = "codekb",
            rotation_minutes: int = _DEFAULT_STREAM_ROTATION_MINUTES,
            logs_client: object = None,
            **kwargs: object,
        ) -> None:
            # Read, not popped: the parent needs log_group to set its header.
            self._cw_log_group = str(kwargs.get("log_group") or "")
            self._cw_stream_prefix = stream_prefix
            self._cw_rotation_minutes = rotation_minutes
            self._cw_logs_client = logs_client
            self._cw_current_stream: Optional[str] = str(kwargs.get("log_stream") or "") or None
            self._cw_lock = threading.Lock()
            super().__init__(*args, **kwargs)
            if self._cw_current_stream and self._cw_logs_client and self._cw_log_group:
                _ensure_log_stream(
                    self._cw_logs_client,
                    self._cw_log_group,
                    self._cw_current_stream,
                    fatal=True,
                )

        def _ensure_current_stream(self) -> None:
            """Point the exporter at the stream for the current window."""
            desired = _resolve_stream_name(self._cw_stream_prefix, self._cw_rotation_minutes)
            if desired == self._cw_current_stream:
                return
            with self._cw_lock:
                if desired == self._cw_current_stream:
                    return
                if self._cw_logs_client is not None and self._cw_log_group:
                    _ensure_log_stream(self._cw_logs_client, self._cw_log_group, desired)
                # Unsigned header, so it can change between exports safely.
                self._session.headers["x-aws-log-stream"] = desired
                self._cw_current_stream = desired

        def export(self, batch: Sequence[object]) -> object:
            try:
                self._ensure_current_stream()
            except Exception as exc:  # never let rotation break export
                logger.warning("Log stream rotation failed: %s", exc)
            return super().export(batch)

    return RotatingCloudWatchLogExporter


def _build_cloudwatch_processor(endpoint: str) -> "LogRecordProcessor":
    """Build the ADOT SigV4 CloudWatch log processor (task-role credentials)."""
    import botocore.session  # type: ignore[import-untyped]
    from amazon.opentelemetry.distro.exporter.otlp.aws.logs._aws_cw_otlp_batch_log_record_processor import (  # type: ignore[import-untyped]  # noqa: E501
        AwsCloudWatchOtlpBatchLogRecordProcessor,
    )

    headers = _parse_kv(_cfg("headers", ""))
    log_group = headers.get("x-aws-log-group")
    # A prefix, not the final stream name — see RotatingCloudWatchLogExporter.
    stream_prefix = headers.get("x-aws-log-stream")
    if not log_group:
        raise ObservabilityError(
            "CloudWatch OTLP endpoint is configured but x-aws-log-group is missing "
            "from OTEL_EXPORTER_OTLP_[LOGS_]HEADERS; without it CloudWatch has "
            "nowhere to put the logs."
        )

    match = _CW_ENDPOINT_RE.match(endpoint)
    # Region comes from the endpoint URL; AWS_REGION is the AWS SDK's own var.
    region = (
        (match.group(1) if match else None)
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
    )

    botocore_session = botocore.session.Session()
    try:
        logs_client = botocore_session.create_client("logs", region_name=region)
    except Exception as exc:
        raise ObservabilityError(
            f"cannot build a CloudWatch Logs client for region {region!r}: {exc}"
        ) from exc

    rotation = _rotation_minutes()
    exporter_cls = _build_rotating_cw_exporter_class()
    exporter = exporter_cls(
        aws_region=region,
        session=botocore_session,
        log_group=log_group,
        log_stream=_resolve_stream_name(stream_prefix or "codekb", rotation),
        endpoint=endpoint,
        stream_prefix=stream_prefix or "codekb",
        rotation_minutes=rotation,
        logs_client=logs_client,
    )
    return AwsCloudWatchOtlpBatchLogRecordProcessor(  # type: ignore[no-any-return]
        exporter=exporter
    )


def _build_otlp_processor() -> "LogRecordProcessor":
    """Build a processor for the ``otlp`` sink; raises if it cannot be built."""
    endpoint = _cfg("endpoint", "")
    if not endpoint:
        raise ObservabilityError(
            "OTEL_LOGS_EXPORTER includes 'otlp' but no endpoint is configured. "
            "Set OTEL_EXPORTER_OTLP_LOGS_ENDPOINT to the full URL including the "
            "/v1/logs path, or use OTEL_LOGS_EXPORTER=none to log to stdout."
        )
    if _CW_ENDPOINT_RE.match(endpoint):
        return _build_cloudwatch_processor(endpoint)

    # Non-AWS vendor: standard OTLP exporter with explicit endpoint + headers
    # (from config). Protocol is selectable — 'grpc' for backends that support
    # it, else HTTP/protobuf (also what CloudWatch uses).
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    headers = _parse_kv(_cfg("headers", "")) or None
    protocol = (_cfg("protocol", "http/protobuf") or "http/protobuf").strip().lower()

    exporter: "LogRecordExporter"
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GrpcOTLPLogExporter,
        )

        exporter = GrpcOTLPLogExporter(endpoint=endpoint, headers=headers)
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as HttpOTLPLogExporter,
        )

        exporter = HttpOTLPLogExporter(endpoint=endpoint, headers=headers)

    return BatchLogRecordProcessor(exporter)


def build_log_handler() -> logging.Handler:
    """Return an OTel ``LoggingHandler`` bound to a once-per-process provider.

    Only for the ``otlp`` exporter. ``none`` is served by the text handler in
    ``setup_logging`` and never reaches the OpenTelemetry SDK.

    Raises ``ObservabilityError`` on any failure so startup stops rather than
    running with logs going nowhere.
    """
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk.resources import Resource
    except Exception as exc:
        # ADOT is a core dependency, so this means a broken image rather than an
        # optional feature being absent.
        raise ObservabilityError(f"OpenTelemetry/ADOT is not importable: {exc}") from exc

    global _provider
    try:
        if _provider is None:
            attributes: Dict[str, str] = {"service.name": _service_name()}
            attributes.update(_parse_kv(_cfg("resource_attributes", "")))
            provider = LoggerProvider(resource=Resource.create(attributes))
            provider.add_log_record_processor(_build_otlp_processor())
            set_logger_provider(provider)
            _provider = provider
        with warnings.catch_warnings():
            # LoggingHandler is marked deprecated in the (still-unstable) logs
            # SDK, but the suggested LoggingInstrumentor is a different tool
            # (trace-id injection into log text), not a stdlib->OTel bridge.
            # The handler remains the correct mechanism; silence the notice.
            warnings.simplefilter("ignore", DeprecationWarning)
            return LoggingHandler(level=logging.NOTSET, logger_provider=_provider)
    except ObservabilityError:
        raise
    except Exception as exc:
        raise ObservabilityError(f"log handler setup failed: {exc}") from exc


def shutdown_logging_provider() -> None:
    """Flush and shut down the log provider (call on app shutdown)."""
    global _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as exc:
            # The sink being shut down may not carry this, so it also goes to
            # stderr: a failed flush drops the final batch of records.
            logger.warning("OpenTelemetry log provider shutdown failed: %s", exc)
            print(f"OpenTelemetry log flush failed on shutdown: {exc}", file=sys.stderr)
