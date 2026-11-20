"""The AWS-client guard must cover botocore sessions, not just boto3.client."""

import boto3
import botocore.session
import pytest


def test_boto3_client_rejects_unsupported_service():
    with pytest.raises(ValueError, match="Unsupported"):
        boto3.client("logs", region_name="us-east-1")


def test_botocore_session_rejects_unsupported_service():
    """src.core.observability builds its CloudWatch client this way."""
    with pytest.raises(ValueError, match="Unsupported"):
        botocore.session.Session().create_client("logs", region_name="us-east-1")


def test_supported_service_still_reaches_the_mock():
    assert boto3.client("s3") is not None
