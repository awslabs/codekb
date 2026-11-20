"""
Knowledge Base Manager for persistence operations.

This module handles serialization, deserialization, and S3 storage operations
for knowledge base import/export functionality.
"""

import json
from typing import Any, Dict
from datetime import datetime

from ..core.models import KnowledgeBase
from ..core.config import get_config, get_positive_int
from ..core.logging_utils import get_logger, ErrorContext, StorageError
import boto3
from botocore.exceptions import ClientError

logger = get_logger(__name__)


class StagingCommitResult:
    """Result of a staging commit operation.

    Attributes:
        success: Whether the commit completed without errors.
        refresh_errors: Dictionary of error keys to descriptions for partial failures.
    """

    def __init__(self, success: bool, refresh_errors: Dict[str, str] | None = None) -> None:
        self.success = success
        self.refresh_errors = refresh_errors or {}


class KnowledgeBaseManager:
    """
    Manager for knowledge base persistence operations.

    Handles export/import of knowledge bases to/from S3.
    """

    def __init__(self):
        """Initialize knowledge base manager."""
        self.storage_config = get_config("storage", {})
        self._s3_client = None

    @property
    def s3_client(self):
        """Lazy initialization of S3 client.

        ``max_pool_connections`` is bumped above botocore's default of 10
        because several read paths (``list_spaces``, ``list_hyperspaces``,
        delete cleanup) fan out ``GetObject`` calls across threads. With the
        default pool, parallel workers would serialize waiting for a
        connection.
        """
        if self._s3_client is None:
            from botocore.config import Config

            config = Config(
                max_pool_connections=64,
                signature_version="s3v4",
                read_timeout=get_positive_int("storage.s3.read_timeout"),
                s3={
                    "addressing_style": "virtual",
                    # us-east-1's default is the global s3.amazonaws.com endpoint,
                    # which caused the original 307 for buckets in other regions.
                    # Forcing the regional endpoint closes that gap while letting
                    # botocore resolve the partition-specific host (amazonaws.com,
                    # amazonaws.com.cn for China, GovCloud/ISO suffixes, etc.).
                    "us_east_1_regional_endpoint": "regional",
                },
            )
            client_kwargs: Dict[str, Any] = {"config": config}

            # Pin the client to the configured bucket region; botocore derives the
            # correct partition-aware regional endpoint from it (do NOT hand-build
            # the endpoint URL — that assumes the aws partition's amazonaws.com
            # suffix and breaks in China/GovCloud/ISO). When no region is
            # configured, defer to boto3's standard resolution chain.
            region = self._get_s3_config().get("region")
            if region:
                client_kwargs["region_name"] = region

            self._s3_client = boto3.client("s3", **client_kwargs)
        return self._s3_client

    def _get_s3_config(self) -> Dict[str, Any]:
        """Get S3 configuration for knowledge base storage."""
        return self.storage_config.get("s3", {})

    def _get_s3_bucket(self) -> str:
        """Get S3 bucket name for knowledge base storage with validation."""
        bucket_name = self._get_s3_config().get("bucket_name")
        if not bucket_name:
            raise StorageError("S3 bucket_name not configured in storage.s3")
        return bucket_name

    def _get_s3_key(self, space_id: str) -> str:
        """Get S3 object key for space knowledge base."""
        return f"spaces/{space_id}/knowledge_base.json"

    def export_knowledge_base(self, knowledge_base: KnowledgeBase) -> Dict[str, Any]:
        """
        Export knowledge base to S3.

        Args:
            knowledge_base: KnowledgeBase to export

        Returns:
            Dictionary with export result information

        Raises:
            StorageError: If export fails
        """
        location = f"S3 (space: {knowledge_base.space_id})"
        with ErrorContext(logger, f"export knowledge base to {location}"):
            logger.info(f"Exporting knowledge base to {location}")

            # Validate knowledge base
            validation_result = self._validate_knowledge_base(knowledge_base)
            if validation_result["errors"]:
                raise StorageError(
                    f"Knowledge base validation failed: {validation_result['errors']}"
                )

            # Serialize to dictionary
            kb_dict = knowledge_base.to_dict()

            # Add export metadata
            export_timestamp = datetime.now().isoformat()
            kb_dict["export_metadata"] = {
                "export_timestamp": export_timestamp,
                "exporter_version": "1.0",
            }

            # Export as JSON and get file size
            file_size = self._export_json(kb_dict)

            result = {
                "status": "success",
                "location": f"s3://{self._get_s3_bucket()}/{self._get_s3_key(knowledge_base.space_id)}",
                "file_size": file_size,
                "export_timestamp": export_timestamp,
                "components_exported": len(knowledge_base.components),
            }

            logger.info(
                f"Export complete: {len(knowledge_base.components)} components, {file_size} bytes"
            )
            return result

    def import_knowledge_base(self, space_id: str) -> KnowledgeBase:
        """
        Import knowledge base from S3.

        Args:
            space_id: Space identifier (required)

        Returns:
            Loaded KnowledgeBase object

        Raises:
            StorageError: If import fails
        """
        location = f"S3 (space: {space_id})"
        with ErrorContext(logger, f"import knowledge base from {location}"):
            logger.info(f"Importing knowledge base from {location}")

            if not self._exists(space_id):
                logger.info(f"Knowledge base not found: {location}")
                return None

            # Import JSON
            kb_dict = self._import_json(space_id)

            # Validate imported data
            validation_result = self._validate_import_data(kb_dict)
            if validation_result["errors"]:
                raise StorageError(f"Import validation failed: {validation_result['errors']}")

            # Create knowledge base from dictionary
            knowledge_base = KnowledgeBase.from_dict(kb_dict)

            logger.info(f"Import complete: {len(knowledge_base.components)} components loaded")
            return knowledge_base

    def _export_json(self, kb_dict: Dict[str, Any]) -> int:
        """Export knowledge base as JSON to S3.

        Returns:
            Size of exported content in bytes
        """
        space_id = kb_dict.get("space_id")
        if not space_id:
            raise StorageError("Knowledge base missing space_id")
        json_content = json.dumps(kb_dict, indent=2, ensure_ascii=False)
        self._write_to_s3(space_id, json_content)
        return len(json_content)

    def _write_to_s3(self, space_id: str, content: str) -> None:
        """Write content to S3 as regular object."""
        bucket_name = self._get_s3_bucket()
        s3_key = self._get_s3_key(space_id)
        logger.debug(
            f"s3.put_object: s3://{bucket_name}/{s3_key} ({len(content):,} bytes, "
            f"content_type=application/json)"
        )
        try:
            self.s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=content.encode("utf-8"),
                ContentType="application/json",
            )
            logger.debug(f"s3.put_object ok: s3://{bucket_name}/{s3_key}")
        except ClientError as e:
            logger.error(
                f"s3.put_object failed: s3://{bucket_name}/{s3_key} "
                f"(body {len(content):,} bytes): {e}",
                exc_info=True,
            )
            raise StorageError(f"Failed to upload to S3: {e}")

    def _import_json(self, space_id: str) -> Dict[str, Any]:
        """Import knowledge base JSON from S3."""
        json_content = self._read_from_s3(space_id)

        try:
            return json.loads(json_content)
        except json.JSONDecodeError as e:
            raise StorageError(f"Invalid JSON format: {e}")

    def _read_from_s3(self, space_id: str) -> str:
        """Read content from S3 regular object."""
        bucket_name = self._get_s3_bucket()
        s3_key = self._get_s3_key(space_id)
        logger.debug(f"s3.get_object: s3://{bucket_name}/{s3_key}")
        try:
            response = self.s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            content = response["Body"].read().decode("utf-8")
            logger.debug(f"s3.get_object ok: s3://{bucket_name}/{s3_key} ({len(content):,} bytes)")
            return content
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchKey":
                logger.debug(f"s3.get_object NoSuchKey: s3://{bucket_name}/{s3_key}")
                raise StorageError(f"Knowledge base not found in S3: {space_id}")
            logger.error(f"s3.get_object failed: s3://{bucket_name}/{s3_key}: {e}", exc_info=True)
            raise StorageError(f"Failed to download from S3: {e}")

    def _exists(self, space_id: str) -> bool:
        """Check if knowledge base exists in S3.

        Args:
            space_id: Space identifier

        Returns:
            True if knowledge base exists, False otherwise
        """
        bucket_name = self._get_s3_bucket()
        s3_key = self._get_s3_key(space_id)
        try:
            self.s3_client.head_object(Bucket=bucket_name, Key=s3_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.debug(f"Knowledge base not found in S3: s3://{bucket_name}/{s3_key}")
                return False
            logger.warning(
                f"head_object failed bucket: {bucket_name}, key: {s3_key}, error: {e.response['Error'].get('Code')}"
            )
            raise StorageError(f"Failed to check S3 object existence: {e}")

    def _validate_knowledge_base(self, knowledge_base: KnowledgeBase) -> Dict[str, Any]:
        """Validate knowledge base before export."""
        errors = []
        warnings = []

        if not knowledge_base.source_directory:
            errors.append("Missing source directory")

        if not knowledge_base.components:
            warnings.append("No components in knowledge base")
        else:
            for i, component in enumerate(knowledge_base.components):
                if not component.id:
                    errors.append(f"Component {i} missing ID")
                if not component.name:
                    errors.append(f"Component {i} missing name")
                if not component.file_path:
                    errors.append(f"Component {i} missing file path")

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    def _validate_import_data(self, kb_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Validate imported knowledge base data."""
        errors = []
        warnings = []

        # Check required top-level fields
        for field in ["source_directory", "components", "version"]:
            if field not in kb_dict:
                errors.append(f"Missing required field: {field}")

        # Check version compatibility
        version = kb_dict.get("version", "1.0")
        if version not in ["1.0"]:
            warnings.append(f"Unknown version: {version}, attempting to import anyway")

        # Validate components structure
        components = kb_dict.get("components", [])
        if not isinstance(components, list):
            errors.append("Components must be a list")
        elif components:  # Only validate if not empty
            required_comp_fields = ["id", "name", "type", "file_path", "line_number", "language"]
            for i, comp_dict in enumerate(components):
                if not isinstance(comp_dict, dict):
                    errors.append(f"Component {i} must be a dictionary")
                else:
                    for field in required_comp_fields:
                        if field not in comp_dict:
                            errors.append(f"Component {i} missing required field: {field}")

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    # ── Staging helpers for space refresh ────────────────────────────────

    @staticmethod
    def _staging_kb_key(space_id: str) -> str:
        """S3 key for the staged knowledge base JSON."""
        return f"spaces/{space_id}/knowledge_base.json.staging"

    @staticmethod
    def _canonical_source_key(space_id: str) -> str:
        """S3 key for the canonical source tarball."""
        return f"spaces/{space_id}/source.tar.gz"

    @staticmethod
    def _staging_source_key(space_id: str) -> str:
        """S3 key for the staged source tarball."""
        return f"spaces/{space_id}/source.tar.gz.staging"

    def _write_staging_kb(
        self,
        space_id: str,
        kb_data: Dict[str, Any],
        source_archive_bytes: bytes,
    ) -> None:
        """Write staged knowledge base JSON and source tarball to S3.

        Both artifacts are written to `.staging` sibling keys. Neither the
        canonical ``knowledge_base.json`` nor ``source.tar.gz`` is modified.

        Args:
            space_id: Space identifier.
            kb_data: Serialized knowledge base dictionary (will be JSON-encoded).
            source_archive_bytes: Raw bytes of the gzipped source tarball.

        Raises:
            StorageError: If either staging write fails.
        """
        bucket = self._get_s3_bucket()
        kb_staging_key = self._staging_kb_key(space_id)
        source_staging_key = self._staging_source_key(space_id)

        # Write knowledge_base.json.staging
        kb_json = json.dumps(kb_data, indent=2, ensure_ascii=False).encode("utf-8")
        logger.debug(
            f"s3.put_object: s3://{bucket}/{kb_staging_key} ({len(kb_json):,} bytes, staging KB)"
        )
        try:
            self.s3_client.put_object(
                Bucket=bucket,
                Key=kb_staging_key,
                Body=kb_json,
                ContentType="application/json",
            )
        except ClientError as e:
            logger.error(
                f"s3.put_object failed (staging KB): s3://{bucket}/{kb_staging_key}: {e}",
                exc_info=True,
            )
            raise StorageError(f"Failed to write staging KB to S3: {e}")

        # Write source.tar.gz.staging
        logger.debug(
            f"s3.put_object: s3://{bucket}/{source_staging_key} "
            f"({len(source_archive_bytes):,} bytes, staging source tarball)"
        )
        try:
            self.s3_client.put_object(
                Bucket=bucket,
                Key=source_staging_key,
                Body=source_archive_bytes,
                ContentType="application/gzip",
            )
        except ClientError as e:
            # Best-effort cleanup of the KB staging key we already wrote
            try:
                self.s3_client.delete_object(Bucket=bucket, Key=kb_staging_key)
            except ClientError:
                logger.warning(
                    f"Failed to clean up staging KB key after source staging failure: "
                    f"s3://{bucket}/{kb_staging_key}"
                )
            logger.error(
                f"s3.put_object failed (staging source): s3://{bucket}/{source_staging_key}: {e}",
                exc_info=True,
            )
            raise StorageError(f"Failed to write staging source tarball to S3: {e}")

        logger.info(
            f"Staging artifacts written for space {space_id}: "
            f"KB={len(kb_json):,} bytes, source={len(source_archive_bytes):,} bytes"
        )

    def _commit_staging_kb(self, space_id: str) -> StagingCommitResult:
        """Swap staged artifacts into their canonical locations via CopyObject.

        Ordering:
          1. CopyObject source.tar.gz.staging → source.tar.gz
          2. CopyObject knowledge_base.json.staging → knowledge_base.json
          3. Delete both staging keys (best-effort)

        If step 1 (source tarball swap) fails, the method raises StorageError
        and the caller is responsible for rollback (canonical state is untouched).

        If step 2 (KB swap) fails, the source tarball has already been swapped
        but the canonical KB still points at the prior component set. This is
        recorded as a ``completed_with_errors`` outcome with
        ``refresh_errors["kb_swap"]`` populated.

        Args:
            space_id: Space identifier.

        Returns:
            StagingCommitResult indicating success or partial failure.

        Raises:
            StorageError: If the source tarball CopyObject fails (step 1).
        """
        bucket = self._get_s3_bucket()
        kb_staging_key = self._staging_kb_key(space_id)
        kb_canonical_key = self._get_s3_key(space_id)
        source_staging_key = self._staging_source_key(space_id)
        source_canonical_key = self._canonical_source_key(space_id)

        # Step 1: CopyObject source.tar.gz.staging → source.tar.gz
        logger.debug(
            f"s3.copy_object: s3://{bucket}/{source_staging_key} → "
            f"s3://{bucket}/{source_canonical_key}"
        )
        try:
            self.s3_client.copy_object(
                Bucket=bucket,
                Key=source_canonical_key,
                CopySource={"Bucket": bucket, "Key": source_staging_key},
            )
        except ClientError as e:
            logger.error(
                f"s3.copy_object failed (source tarball swap): "
                f"s3://{bucket}/{source_staging_key} → "
                f"s3://{bucket}/{source_canonical_key}: {e}",
                exc_info=True,
            )
            raise StorageError(f"Failed to swap staging source tarball to canonical: {e}")

        # Step 2: CopyObject knowledge_base.json.staging → knowledge_base.json
        logger.debug(
            f"s3.copy_object: s3://{bucket}/{kb_staging_key} → s3://{bucket}/{kb_canonical_key}"
        )
        try:
            self.s3_client.copy_object(
                Bucket=bucket,
                Key=kb_canonical_key,
                CopySource={"Bucket": bucket, "Key": kb_staging_key},
            )
        except ClientError as e:
            logger.error(
                f"s3.copy_object failed (KB swap): "
                f"s3://{bucket}/{kb_staging_key} → "
                f"s3://{bucket}/{kb_canonical_key}: {e}",
                exc_info=True,
            )
            # Source tarball was already swapped but KB swap failed.
            # Return completed_with_errors so the caller can record it.
            return StagingCommitResult(
                success=False,
                refresh_errors={
                    "kb_swap": (
                        f"CopyObject for knowledge_base.json failed after source.tar.gz "
                        f"was already swapped: {e}"
                    )
                },
            )

        # Step 3: Delete staging keys (best-effort)
        for staging_key in (source_staging_key, kb_staging_key):
            try:
                self.s3_client.delete_object(Bucket=bucket, Key=staging_key)
                logger.debug(f"s3.delete_object ok: s3://{bucket}/{staging_key}")
            except ClientError as e:
                logger.warning(
                    f"s3.delete_object failed (staging cleanup, non-fatal): "
                    f"s3://{bucket}/{staging_key}: {e}"
                )

        logger.info(f"Staging commit complete for space {space_id}")
        return StagingCommitResult(success=True)
