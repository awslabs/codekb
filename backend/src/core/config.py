"""
Configuration management system for the Code Knowledge Base.

This module provides configuration loading, validation, and access
functionality supporting YAML configuration files with environment
variable overrides and validation.
"""

import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging
import os
from .interfaces import IConfigurationManager
from .llm_settings import (
    defaults_env_overrides,
    workload_env_overrides,
    workload_int_env_vars,
)


logger = logging.getLogger(__name__)


# Tracks whether .env has already been loaded so we only do it once per process
_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Load environment variables from .env files into os.environ.

    Searches (in order, first match wins) for a .env file in:
      1. $CODEKB_DOTENV (explicit override)
      2. Current working directory
      3. Repository root — walks up from this file through parent directories
         looking for the first .env sibling to a known project marker
         (pyproject.toml or .git).

    python-dotenv is already a project dependency. Values already present in
    os.environ are NOT overridden, so real shell exports still win.

    This is a no-op on subsequent calls within the same process.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True  # set early so failures don't cause retries

    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv not installed; skipping .env loading")
        return

    candidates: List[Path] = []

    explicit = os.environ.get("CODEKB_DOTENV")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    candidates.append(Path.cwd() / ".env")

    # Walk up from this file to find a repo-root .env. Stop at filesystem root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.exists():
            candidates.append(candidate)
            break
        # Stop walking once we pass the repo root
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            break

    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=str(path), override=False)
            logger.info(f"Loaded environment from {path}")
            return

    logger.debug("No .env file found; relying on shell environment only")


# Env-var suffixes that mark credentials we must never log verbatim. Using
# suffix-match so MAX_TOKENS / CLIENT_ID stay visible but *_TOKEN, *_SECRET,
# *_PASSWORD etc. don't leak.
_SECRET_SUFFIXES = (
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_TOKEN",
    "_API_KEY",
    "_PRIVATE_KEY",
    "_ACCESS_KEY",
    "_ACCESS_KEY_ID",
    "_SECRET_KEY",
    "_SECRET_ACCESS_KEY",
)


def redact_header_list(raw: str) -> str:
    """Mask credential-bearing entries in a ``k=v,k=v`` header string.

    Used for OTEL_EXPORTER_OTLP[_LOGS]_HEADERS, where secrecy is per key: a
    vendor API key must never be logged, but the CloudWatch routing values
    (x-aws-log-group / x-aws-log-stream) are the first thing you need when logs
    do not arrive. Falls back to whole-value redaction if it cannot be parsed.
    """
    # Lazy import: logging_utils imports this module at import time.
    from .logging_utils import redact_params

    pairs: Dict[str, Any] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            pairs[key] = value.strip()

    if not pairs:
        return f"<redacted, len={len(raw)}>"
    return ",".join(f"{k}={v}" for k, v in redact_params(pairs).items())


def _redact(env_var: str, value: str) -> str:
    """Return a safe representation of an env var value for logging."""
    upper = env_var.upper()
    if upper.endswith("_HEADERS"):
        return redact_header_list(value)
    if any(upper.endswith(suffix) for suffix in _SECRET_SUFFIXES):
        return f"<redacted, len={len(value)}>"
    return value


class ConfigurationManager(IConfigurationManager):
    """
    Configuration manager implementation supporting YAML files.

    Supports hierarchical configuration with dot notation access,
    environment variable overrides, and configuration validation.
    """

    # Path to bundled default config inside the package
    _PACKAGE_CONFIG = Path(__file__).parent / "default.yaml"

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize configuration manager.

        Args:
            config_path: Optional path to configuration file
        """
        self._config: Dict[str, Any] = {}
        self._config_path: Optional[Path] = config_path
        self._loaded = False

    def load_configuration(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Load configuration from YAML file.

        Configuration is loaded with the following precedence (later overrides earlier):
        1. Bundled default.yaml (base configuration)
        2. CODEKB_* environment variables (from shell, .env file, or ECS task definition)

        Args:
            config_path: Optional path to configuration file. Passing this (or
                calling ``reload_configuration``) forces a re-read; otherwise
                the manager is idempotent — repeated calls return the cached
                config without re-parsing YAML or re-emitting log output.

        Returns:
            Dictionary containing configuration values

        Raises:
            ConfigError: If the bundled default.yaml is missing or cannot be parsed.
        """
        if self._loaded and config_path is None:
            return self._config

        # Load .env into os.environ before reading env overrides
        _load_dotenv_once()

        if config_path:
            self._config_path = Path(config_path)

        # Determine configuration file path
        if self._config_path is None:
            self._config_path = self._find_default_config()

        # Load base configuration.
        if not (self._config_path and self._config_path.exists()):
            raise ConfigError(
                f"Configuration file not found at {self._config_path!r}. "
                "The bundled default.yaml must ship with the package."
            )
        try:
            with open(self._config_path, "r", encoding="utf-8") as file:
                self._config = yaml.safe_load(file) or {}
        except Exception as e:
            raise ConfigError(
                f"Failed to load configuration from {self._config_path}: {e}"
            ) from e
        logger.info(f"Configuration loaded from {self._config_path}")

        # Apply local.yaml overrides (user-specific)
        self._apply_local_overrides()

        # Apply environment variable overrides (highest precedence)
        self._apply_env_overrides()

        self._validate_language_registry()
        self._validate_generated_file_patterns()

        self._loaded = True
        return self._config

    def _validate_language_registry(self) -> None:
        """Fail the load when a language is both supported and unsupported.

        A supported language is parsed and an unsupported one is counted as
        skipped, so a language in both leaves the behaviour decided by read
        order. Refusing beats starting on an ambiguous registry.
        """
        analysis = self._config.get("analysis")
        if not isinstance(analysis, dict):
            return

        supported = analysis.get("supported_languages")
        unsupported = analysis.get("unsupported_languages")
        if not isinstance(supported, dict) or not isinstance(unsupported, dict):
            return

        both = sorted(set(supported) & set(unsupported))
        if both:
            keys = ", ".join(both)
            raise ValueError(
                f"Invalid configuration: {keys} appears in both "
                "analysis.supported_languages and analysis.unsupported_languages; "
                "a language must be listed in exactly one"
            )

    def _validate_generated_file_patterns(self) -> None:
        """Fail the load when a language's generated-file pattern list is malformed.

        The list is hand-edited, so it is untrusted input: silently ignoring a
        malformed one would leave the user believing files are excluded when
        they are not. A present value must be a list of non-blank text; an
        absent or empty list means no exclusion and is valid, since emptying a
        list is how exclusion is turned off.
        """
        analysis = self._config.get("analysis")
        if not isinstance(analysis, dict):
            return

        supported = analysis.get("supported_languages")
        if not isinstance(supported, dict):
            return

        for language, lang_config in supported.items():
            if not isinstance(lang_config, dict):
                continue
            if "generated_file_patterns" not in lang_config:
                continue

            patterns = lang_config["generated_file_patterns"]
            if not isinstance(patterns, list):
                raise ValueError(
                    f"Invalid configuration: analysis.supported_languages.{language}."
                    f"generated_file_patterns must be a list of patterns, "
                    f"got {type(patterns).__name__}"
                )

            for entry in patterns:
                if not isinstance(entry, str) or not entry.strip():
                    raise ValueError(
                        f"Invalid configuration: analysis.supported_languages.{language}."
                        f"generated_file_patterns holds an invalid entry {entry!r}; "
                        "every entry must be non-blank text"
                    )
                # A pattern is matched against the file's BASENAME, so one
                # carrying a path separator can never fire. Refused rather than
                # accepted-and-inert: a silently dead pattern is the very
                # "believes files are excluded when they are not" case above.
                if "/" in entry or "\\" in entry:
                    raise ValueError(
                        f"Invalid configuration: analysis.supported_languages.{language}."
                        f"generated_file_patterns holds {entry!r}, which contains a path "
                        "separator; patterns are matched against the file name alone, so "
                        "this could never match. Use a name pattern such as '*.g.dart'"
                    )

    def log_effective_configuration(self) -> None:
        """Emit INFO-level lines summarizing what was loaded and what's in effect.

        Called explicitly by setup_logging() after the standard handlers are
        installed, so the output formats the same as the rest of the app's
        logs (plain one line per record).

        Both sections are driven by ``_ENV_OVERRIDES``. Non-secret values only:
        values are redacted by env-var name, so credential suffixes and header
        lists never print verbatim.
        """
        logger.info("effective CodeKB configuration:")
        logger.info(f"  base config:  {self._config_path or '(fallback defaults)'}")

        # Registered env vars that are set in this process
        present = {
            env_var: os.environ[env_var] for env_var in self._ENV_OVERRIDES if env_var in os.environ
        }
        if present:
            logger.info("  env overrides:")
            for env_var in sorted(present):
                display = _redact(env_var, present[env_var])
                logger.info(f"    {env_var}={display} -> {self._ENV_OVERRIDES[env_var]}")
        else:
            logger.info("  env overrides: (none)")

        # Resolved values actually used at runtime
        fields = self._summary_fields()
        width = max((len(config_key) for config_key in fields), default=0)
        logger.info("  resolved values:")
        for config_key in sorted(fields):
            display = self._format_summary_value(config_key, fields[config_key])
            logger.info(f"    {config_key:<{width}} = {display}")

        self._log_llm_settings()

    def _log_llm_settings(self) -> None:
        """Log each workload's resolved LLM settings and where each value came from."""
        from .llm_settings import Workload, resolve  # lazy: llm_settings reads this config

        logger.info("  llm settings per workload:")
        for workload in Workload:
            try:
                logger.info(f"    {workload.value}: {resolve(workload).describe()}")
            except Exception as e:  # a bad value must not hide the rest of the summary
                logger.error(f"    {workload.value}: invalid LLM settings: {e}")

    # Config paths logged as a presence flag rather than a value: identifiers we
    # keep out of the log even though they are not credentials.
    _PRESENCE_ONLY_PATHS: set = {
        "authn.cognito.user_pool_id",
    }

    def _summary_fields(self) -> Dict[str, str]:
        """Map each overridable config path to one env var that feeds it.

        Inverts _ENV_OVERRIDES, first declaration winning. The env var serves
        only to pick the redaction rule, which keys off the name's suffix.
        """
        fields: Dict[str, str] = {}
        for env_var, config_key in self._ENV_OVERRIDES.items():
            fields.setdefault(config_key, env_var)
        return fields

    def _format_summary_value(self, config_key: str, env_var: str) -> str:
        """Render one resolved config value for the startup summary."""
        value = self._get_nested_value(self._config, config_key)
        if config_key in self._PRESENCE_ONLY_PATHS:
            return "(set)" if value else "(unset)"
        if value is None or value == "":
            return "(unset)"
        return _redact(env_var, str(value))

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.

        Args:
            key: Configuration key (supports dot notation for nested values)
            default: Default value if key is not found

        Returns:
            Configuration value or default
        """
        if not self._loaded:
            self.load_configuration()

        return self._get_nested_value(self._config, key, default)

    def validate_configuration(self) -> List[str]:
        """
        Validate the current configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        if not self._loaded:
            self.load_configuration()

        errors: List[str] = []

        # Validate required sections
        required_sections = ["logging", "analysis", "storage"]
        for section in required_sections:
            if section not in self._config:
                errors.append(f"Missing required configuration section: {section}")

        # Validate logging configuration
        if "logging" in self._config:
            logging_config = self._config["logging"]
            if "level" not in logging_config:
                errors.append("Missing logging.level configuration")
            elif logging_config["level"] not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
                errors.append(f"Invalid logging level: {logging_config['level']}")

        # Validate analysis configuration
        if "analysis" in self._config:
            analysis_config = self._config["analysis"]
            if "supported_languages" not in analysis_config:
                errors.append("Missing analysis.supported_languages configuration")
            elif not isinstance(analysis_config["supported_languages"], dict):
                errors.append("analysis.supported_languages must be a dictionary")

        # Validate storage configuration
        if "storage" in self._config:
            storage_config = self._config["storage"]
            if "export_format" in storage_config:
                if storage_config["export_format"] not in ["json", "yaml"]:
                    errors.append(
                        f"Invalid storage.export_format: {storage_config['export_format']}"
                    )

        return errors

    def get_supported_languages(self) -> Dict[str, Dict[str, Any]]:
        """
        Get supported programming languages configuration.

        Returns:
            Dictionary of language configurations
        """
        return self.get_config_value("analysis.supported_languages", {})

    def get_exclude_patterns(self) -> List[str]:
        """
        Get directory/file exclusion patterns.

        Returns:
            List of exclusion patterns
        """
        return self.get_config_value("analysis.exclude_patterns", [])

    def get_logging_config(self) -> Dict[str, Any]:
        """
        Get logging configuration.

        Returns:
            Logging configuration dictionary
        """
        return self.get_config_value("logging", {})

    def get_storage_config(self) -> Dict[str, Any]:
        """
        Get storage configuration.

        Returns:
            Storage configuration dictionary
        """
        return self.get_config_value("storage", {})

    def get_cli_config(self) -> Dict[str, Any]:
        """
        Get CLI configuration.

        Returns:
            CLI configuration dictionary
        """
        return self.get_config_value("cli", {})

    def is_development_mode(self) -> bool:
        """
        Check if running in development mode.

        Returns:
            True if development mode is enabled
        """
        return self.get_config_value("development.debug_mode", False)

    def _find_default_config(self) -> Path:
        """
        Find the default configuration file.

        Search order:
        1. Bundled package config      (installed via uv tool / pip)
        2. Source-tree relative path   (editable install fallback)

        Returns:
            Path to default configuration file
        """
        search_paths = [
            self._PACKAGE_CONFIG,
            Path(__file__).parent.parent.parent.parent / "config" / "default.yaml",
        ]

        for path in search_paths:
            if path.exists():
                return path

        return self._PACKAGE_CONFIG

    def _apply_local_overrides(self) -> None:
        """No-op. local.yaml support has been removed.

        All configuration is now driven by default.yaml + environment variables
        (via CODEKB_* env vars or a .env file loaded by python-dotenv).
        """

    # Mapping of environment variables to config dot-paths
    _ENV_OVERRIDES: Dict[str, str] = {
        "CODEKB_EXCLUDE_PATTERNS_EXTRA": "analysis.exclude_patterns",
        "CODEKB_STORAGE_BACKEND": "storage.backend",
        "CODEKB_S3_BUCKET": "storage.s3.bucket_name",
        "CODEKB_S3_REGION": "storage.s3.region",
        "CODEKB_S3_READ_TIMEOUT": "storage.s3.read_timeout",
        "CODEKB_S3_VECTORS_BUCKET": "storage.s3_vectors.bucket_name",
        "CODEKB_S3_VECTORS_REGION": "storage.s3_vectors.region",
        "CODEKB_S3_VECTORS_INDEX": "storage.s3_vectors.index_name",
        "CODEKB_S3_VECTORS_DEFAULT_TOP_K": "storage.s3_vectors.default_top_k",
        "CODEKB_LOG_LEVEL": "logging.level",
        "CODEKB_LOG_PAYLOAD_MAX": "logging.payload_max_chars",
        # LLM shared defaults. The model and max_tokens keep their original
        # CODEKB_BEDROCK_* names; the per-workload overrides are generated
        # below from llm_settings.Workload.
        "CODEKB_BEDROCK_MODEL_ID": "llm.defaults.model_id",
        "CODEKB_BEDROCK_MAX_TOKENS": "llm.defaults.max_tokens",
        **defaults_env_overrides(),
        **workload_env_overrides(),
        "CODEKB_LLM_STARTUP_CHECK": "llm.startup_check",
        # Legacy web chat extended thinking
        "CODEKB_BEDROCK_REASONING": "llm.workloads.chat.reasoning",
        "CODEKB_BEDROCK_REASONING_BUDGET_TOKENS": "llm.workloads.chat.reasoning_budget_tokens",
        # Bedrock connection
        "CODEKB_BEDROCK_REGION": "llm.providers.bedrock.region",
        "CODEKB_BEDROCK_READ_TIMEOUT": "llm.providers.bedrock.read_timeout",
        # Always-on secret/PII guardrail. id + version are provisioned by the
        # deploy (CDK/CLI); enabled defaults true in default.yaml.
        "CODEKB_BEDROCK_GUARDRAIL_ENABLED": "llm.providers.bedrock.guardrail.enabled",
        "CODEKB_BEDROCK_GUARDRAIL_ID": "llm.providers.bedrock.guardrail.id",
        "CODEKB_BEDROCK_GUARDRAIL_VERSION": "llm.providers.bedrock.guardrail.version",
        "CODEKB_SUMMARIZATION_CONCURRENCY": "summarization.concurrency",
        "CODEKB_SUMMARIZATION_INPUT_TOKEN_BUDGET": "summarization.input_token_budget",
        "CODEKB_SUMMARIZATION_WARN_AFTER_STALE_S": "summarization.warn_after_stale_s",
        "CODEKB_SUMMARIZATION_FAIL_AFTER_STALE_S": "summarization.fail_after_stale_s",
        # Cognito auth
        "CODEKB_COGNITO_USER_POOL_ID": "authn.cognito.user_pool_id",
        "CODEKB_COGNITO_REGION": "authn.cognito.region",
        "CODEKB_COGNITO_DOMAIN_URL": "authn.cognito.domain_url",
        "CODEKB_COGNITO_MCP_CLIENT_ID": "authn.cognito.mcp_client_id",
        # Security response headers
        "CODEKB_SECURITY_HEADERS_ENABLED": "security.headers.enabled",
        "CODEKB_SECURITY_CSP": "security.headers.content_security_policy",
        "CODEKB_SECURITY_CSP_REPORT_ONLY": "security.headers.csp_report_only",
        "CODEKB_SECURITY_HSTS": "security.headers.hsts",
        # Feature flags
        "CODEKB_ZIP_UPLOAD_ENABLED": "features.zip_upload_enabled",
        "CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL": "features.allow_credentials_in_git_url",
        "CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES": "features.allow_private_git_addresses",
        "CODEKB_OVERVIEW_GENERATION_ENABLED": "features.overview_generation_enabled",
        # Per-user git credentials
        "CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN": "gitcreds.kms_key_arn",
        "CODEKB_GIT_CREDENTIALS_PROVIDERS": "gitcreds.providers",
        # Optional custom (self-hosted) host per provider, as a full http(s) URL
        # with an optional port (e.g. https://github.example.com:8443).
        "CODEKB_GITHUB_CUSTOM_HOST": "gitcreds.github_custom_host",
        "CODEKB_GITLAB_CUSTOM_HOST": "gitcreds.gitlab_custom_host",
        "CODEKB_BITBUCKET_CUSTOM_HOST": "gitcreds.bitbucket_custom_host",
        # Authentication requirement. CODEKB_REQUIRE_AUTHENTICATION is the canonical
        # name; CODEKB_REQUIRE_AUTH is a backward-compatible alias.
        "CODEKB_REQUIRE_AUTHENTICATION": "authn.require_authentication",
        "CODEKB_REQUIRE_AUTH": "authn.require_authentication",
        # Space authorization
        "CODEKB_REQUIRE_AUTHORIZATION": "authz.require_authorization",
        "CODEKB_AUTHZ_REPO_CACHE_TTL_HOURS": "authz.repo_cache_ttl_hours",
        "CODEKB_AUTHZ_REPO_CACHE_MAXSIZE": "authz.repo_cache_maxsize",
        # Service runtime
        "CODEKB_MAX_CACHED_SPACES": "service.max_cached_spaces",
        "CODEKB_PIPELINE_POOL_SIZE": "service.pipeline_pool_size",
        # Local source cache. The root is deliberately not configurable — see
        # SOURCE_CACHE_ROOT in service/source_provider.py.
        "CODEKB_SOURCE_CACHE_MAX_SIZE_GB": "source_cache.max_size_gb",
        "CODEKB_SOURCE_CACHE_REFRESH_CHECK_TTL_HOURS": "source_cache.refresh_check_ttl_hours",
        # Observability — OpenTelemetry log export. Uses the STANDARD OTEL_* names
        # (not CODEKB_*) so the OTel SDK and other in-process OTel libraries (e.g.
        # Strands) read the same env vars this app reads via get_config. Generic
        # names are listed before their logs-specific variants so the logs-specific
        # one wins when both are set.
        "OTEL_LOGS_EXPORTER": "observability.logs.exporter",
        # Logs-specific only: the generic OTEL_EXPORTER_OTLP_ENDPOINT is a base
        # URL needing "/v1/logs" appended, which this app's explicit-endpoint
        # exporter would skip, silently POSTing to the base URL.
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "observability.logs.endpoint",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "observability.logs.protocol",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "observability.logs.protocol",
        "OTEL_EXPORTER_OTLP_HEADERS": "observability.logs.headers",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "observability.logs.headers",
        "OTEL_SERVICE_NAME": "observability.logs.service_name",
        "OTEL_RESOURCE_ATTRIBUTES": "observability.logs.resource_attributes",
        "OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES": "observability.logs.cloudwatch_stream_rotation_minutes",
    }

    # Environment variables whose values must be coerced to int
    _INT_ENV_VARS: set = {
        "CODEKB_BEDROCK_MAX_TOKENS",
        "CODEKB_BEDROCK_READ_TIMEOUT",
        "CODEKB_BEDROCK_REASONING_BUDGET_TOKENS",
        "CODEKB_LOG_PAYLOAD_MAX",
        "CODEKB_S3_READ_TIMEOUT",
        "CODEKB_S3_VECTORS_DEFAULT_TOP_K",
        "CODEKB_SUMMARIZATION_CONCURRENCY",
        "CODEKB_SUMMARIZATION_INPUT_TOKEN_BUDGET",
        "CODEKB_SUMMARIZATION_WARN_AFTER_STALE_S",
        "CODEKB_SUMMARIZATION_FAIL_AFTER_STALE_S",
        "CODEKB_MAX_CACHED_SPACES",
        "CODEKB_PIPELINE_POOL_SIZE",
        "OTEL_CLOUDWATCH_LOG_STREAM_ROTATION_MINUTES",
        "CODEKB_AUTHZ_REPO_CACHE_TTL_HOURS",
        "CODEKB_AUTHZ_REPO_CACHE_MAXSIZE",
        *workload_int_env_vars(),
    }

    # Int env vars whose invalid value is kept, not skipped, so startup
    # validation (validate_llm_settings) fails on it. Skipping would make the
    # workload silently inherit the shared value.
    _STRICT_INT_ENV_VARS: set = workload_int_env_vars()

    # Environment variables where an empty value means "not set", so the
    # setting inherits instead of becoming "". An env var can't express YAML
    # null, and per-workload LLM settings rely on inheritance.
    _EMPTY_IS_UNSET_ENV_VARS: set = {
        *defaults_env_overrides(),
        *workload_env_overrides(),
    }

    # Environment variables whose values must be coerced to float.
    _FLOAT_ENV_VARS: set = {
        "CODEKB_SOURCE_CACHE_MAX_SIZE_GB",
        "CODEKB_SOURCE_CACHE_REFRESH_CHECK_TTL_HOURS",
    }

    # Environment variables whose values must be coerced to bool
    # (accepts "1"/"true"/"yes"/"on" case-insensitively; anything else is False)
    _BOOL_ENV_VARS: set = {
        "CODEKB_LLM_STARTUP_CHECK",
        "CODEKB_BEDROCK_REASONING",
        "CODEKB_BEDROCK_GUARDRAIL_ENABLED",
        "CODEKB_SECURITY_HEADERS_ENABLED",
        "CODEKB_SECURITY_CSP_REPORT_ONLY",
        "CODEKB_ZIP_UPLOAD_ENABLED",
        "CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL",
        "CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES",
        "CODEKB_OVERVIEW_GENERATION_ENABLED",
        "CODEKB_REQUIRE_AUTHORIZATION",
    }

    # Environment variables that EXTEND a list-valued config key instead of
    # replacing it. The comma/newline-separated entries are appended to the list
    # already loaded from default.yaml, so the values shipped there are always
    # kept and a deployment only names the extra entries it wants. Entries are
    # stripped, empties dropped, duplicates ignored (first-seen order preserved).
    _LIST_EXTEND_ENV_VARS: set = {
        "CODEKB_EXCLUDE_PATTERNS_EXTRA",
    }

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides (highest precedence)."""
        # Authentication flag: CODEKB_REQUIRE_AUTHENTICATION is the canonical name and
        # CODEKB_REQUIRE_AUTH its backward-compatible alias;
        canonical_raw = os.environ.get("CODEKB_REQUIRE_AUTHENTICATION")
        alias_raw = os.environ.get("CODEKB_REQUIRE_AUTH")
        canonical_val = None if canonical_raw is None else canonical_raw.strip().lower() != "false"
        alias_val = None if alias_raw is None else alias_raw.strip().lower() != "false"
        if canonical_val is not None and alias_val is not None and canonical_val != alias_val:
            logger.warning(
                "Conflicting authentication flags: CODEKB_REQUIRE_AUTHENTICATION=%r and its "
                "backward-compatible alias CODEKB_REQUIRE_AUTH=%r resolve to different values. "
                "Failing closed: authentication is REQUIRED. Set both to the same value, or use "
                "only the canonical CODEKB_REQUIRE_AUTHENTICATION.",
                canonical_raw,
                alias_raw,
            )
            self._set_nested_value(self._config, "authn.require_authentication", True)
        elif canonical_val is not None:
            self._set_nested_value(self._config, "authn.require_authentication", canonical_val)
        elif alias_val is not None:
            self._set_nested_value(self._config, "authn.require_authentication", alias_val)
        for env_var, config_key in self._ENV_OVERRIDES.items():
            if env_var in ("CODEKB_REQUIRE_AUTHENTICATION", "CODEKB_REQUIRE_AUTH"):
                continue
            value = os.environ.get(env_var)
            if value is not None and env_var in self._EMPTY_IS_UNSET_ENV_VARS and not value.strip():
                continue
            if value is not None:
                if env_var in self._LIST_EXTEND_ENV_VARS:
                    self._extend_list_config(config_key, value)
                    continue
                if env_var in self._INT_ENV_VARS:
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        if env_var in self._STRICT_INT_ENV_VARS:
                            # Keep the bad value so startup validation rejects it
                            # instead of silently inheriting another setting.
                            self._set_nested_value(self._config, config_key, value)
                            continue
                        logger.warning(f"Invalid integer value for {env_var}: {value!r}, skipping")
                        continue
                elif env_var in self._FLOAT_ENV_VARS:
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid numeric value for {env_var}: {value!r}, skipping")
                        continue
                elif env_var in self._BOOL_ENV_VARS:
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                self._set_nested_value(self._config, config_key, value)
                logger.debug(f"Config override from env: {env_var} -> {config_key}")

    def _extend_list_config(self, config_key: str, raw: str) -> None:
        """Append comma/newline-separated entries to a list config value.

        The values shipped in default.yaml are kept and listed first; entries are
        stripped, empties dropped, duplicates ignored (first-seen order kept).
        """
        current = self._get_nested_value(self._config, config_key, [])
        merged: List[str] = list(current) if isinstance(current, list) else []
        for item in raw.replace("\n", ",").split(","):
            item = item.strip()
            if item and item not in merged:
                merged.append(item)
        self._set_nested_value(self._config, config_key, merged)
        logger.debug(f"Config list-extend from env -> {config_key}")

    def _set_nested_value(self, config: Dict[str, Any], key: str, value: Any) -> None:
        """Set a nested config value using dot notation."""
        keys = key.split(".")
        current = config
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> None:
        """
        Deep merge override dict into base dict (modifies base in place).

        Args:
            base: Base dictionary to merge into
            override: Override dictionary with values to apply
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _get_nested_value(self, config: Dict[str, Any], key: str, default: Any = None) -> Any:
        """
        Get nested configuration value using dot notation.

        Args:
            config: Configuration dictionary
            key: Dot-separated key path
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key.split(".")
        current = config

        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default

        return current


# Global configuration instance
_config_manager: Optional[ConfigurationManager] = None


def get_config_manager() -> ConfigurationManager:
    """
    Get the global configuration manager instance.

    Returns:
        Global ConfigurationManager instance
    """
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigurationManager()
    return _config_manager


def get_config(key: str, default: Any = None) -> Any:
    """
    Convenience function to get configuration value.

    Args:
        key: Configuration key (supports dot notation)
        default: Default value if key not found

    Returns:
        Configuration value or default
    """
    return get_config_manager().get_config_value(key, default)


def get_positive_int(key: str) -> int:
    """Return a required positive int whose default lives in ``default.yaml``.

    Fails loud (``ConfigurationError``) on a missing, non-integer, or < 1 value, so
    a bad override is caught instead of silently falling back to a magic default.
    """
    from .logging_utils import ConfigurationError  # lazy: logging_utils imports config

    raw = get_config(key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{key} must be an integer, got {raw!r}")
    if value < 1:
        raise ConfigurationError(f"{key} must be >= 1, got {value}")
    return value


def env_var_for(config_key: str) -> Optional[str]:
    """The env var that feeds a config dot-path, or ``None`` if none maps to it."""
    for env_var, key in ConfigurationManager._ENV_OVERRIDES.items():
        if key == config_key:
            return env_var
    return None


class ConfigError(RuntimeError):
    """Raised when a required configuration key is absent from the loaded config."""


# Unique sentinel so require_config can tell "key absent" apart from a
# legitimately configured None/0/"" value.
_REQUIRED = object()


def require_config(key: str) -> Any:
    """Return a configuration value that MUST be present — no inline default.

    Values that ship in ``default.yaml`` are read through this rather than
    ``get_config(key, <literal>)`` so the bundled ``default.yaml`` stays the
    single source of truth. A missing key fails fast with a clear error instead
    of silently falling back to a hardcoded literal that can drift from the yaml.
    """
    value = get_config_manager().get_config_value(key, _REQUIRED)
    if value is _REQUIRED:
        raise ConfigError(
            f"Required configuration key '{key}' is missing from the loaded "
            f"configuration. It must be defined in default.yaml."
        )
    return value


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Convenience function to load configuration.

    Args:
        config_path: Optional path to configuration file

    Returns:
        Loaded configuration dictionary
    """
    return get_config_manager().load_configuration(config_path)
