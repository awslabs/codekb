"""Encrypted-at-rest storage for per-user git credentials.

One S3 object per user at ``users/{sub}/gitcreds.json`` holds a map keyed
by provider. Each provider entry keeps non-secret fields (username, host) in
clear and the token as a sealed envelope:

  data key   — 256-bit, fresh per write, from KMS GenerateDataKey
  content    — AES-256-GCM(token) under a random 96-bit nonce, 128-bit tag
  wrapping   — the data key sealed by a KMS key
  binding    — KMS encryption context {user_sub, provider, purpose} as AAD,
               rebuilt from the caller's own verified sub on decrypt

The plaintext data key is never stored. Decrypt rebuilds the context from the
caller's ``sub``, so KMS refuses to unwrap one user's object for another.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..core.config import get_config
from ..core.logging_utils import StorageError, get_logger
from ..persistence.knowledge_base_manager import KnowledgeBaseManager

logger = get_logger(__name__)

_PURPOSE = "git-pat"

# Non-secret fields kept in clear per provider entry (everything except the
# sealed-token triple).
_CLEAR_FIELDS = ("username", "host")
_SEALED_FIELDS = ("wrapped_key", "nonce", "ciphertext")


def _canonical_aad(ctx: dict) -> bytes:
    """Serialize the encryption context byte-identically for AAD.

    Sorted keys + fixed separators: encrypt and decrypt must produce the exact
    same bytes or GCM verification fails.
    """
    return json.dumps(ctx, sort_keys=True, separators=(",", ":")).encode()


class GitCredentialStore:
    """Read/write per-user credential objects, one S3 object per ``sub``.

    S3 is reached through the shared ``KnowledgeBaseManager`` client. The KMS
    client is lazy and injectable (tests substitute a fake, since the suite
    guards ``boto3.client`` to S3-family services only).
    """

    def __init__(
        self,
        kb_manager: Optional[KnowledgeBaseManager] = None,
        kms_client: Any = None,
    ) -> None:
        self.kb_manager = kb_manager or KnowledgeBaseManager()
        self._kms_client = kms_client

    # ── clients / config ────────────────────────────────────────────────

    @property
    def kms(self) -> Any:
        if self._kms_client is None:
            region = get_config("storage.s3.region") or None
            self._kms_client = (
                boto3.client("kms", region_name=region) if region else boto3.client("kms")
            )
        return self._kms_client

    def _kms_key_arn(self) -> str:
        arn = (get_config("gitcreds.kms_key_arn", "") or "").strip()
        if not arn:
            raise StorageError(
                "Git credentials are not configured: set CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN "
                "to the KMS key ARN."
            )
        return arn

    def _s3_key(self, sub: str) -> str:
        return f"users/{sub}/gitcreds.json"

    # ── envelope ────────────────────────────────────────────────────────

    def _context(self, sub: str, provider: str) -> dict:
        return {"user_sub": sub, "provider": provider, "purpose": _PURPOSE}

    def _seal(self, token: str, sub: str, provider: str) -> dict:
        ctx = self._context(sub, provider)
        dk = self.kms.generate_data_key(
            KeyId=self._kms_key_arn(), KeySpec="AES_256", EncryptionContext=ctx
        )
        nonce = _random_nonce()
        ciphertext = AESGCM(dk["Plaintext"]).encrypt(nonce, token.encode(), _canonical_aad(ctx))
        # The plaintext data key is dropped here — only the wrapped copy persists.
        return {
            "wrapped_key": base64.b64encode(dk["CiphertextBlob"]).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    def _unseal(self, entry: dict, sub: str, provider: str) -> str:
        ctx = self._context(sub, provider)
        try:
            dk = self.kms.decrypt(
                CiphertextBlob=base64.b64decode(entry["wrapped_key"]),
                EncryptionContext=ctx,
                KeyId=self._kms_key_arn(),
            )["Plaintext"]
        except ClientError as e:
            # A context/key mismatch is KMS refusing — treat as "no usable token"
            # rather than leaking which check failed.
            raise StorageError(f"Could not decrypt credential for provider '{provider}'") from e
        try:
            return AESGCM(dk).decrypt(
                base64.b64decode(entry["nonce"]),
                base64.b64decode(entry["ciphertext"]),
                _canonical_aad(ctx),
            ).decode()
        except InvalidTag as e:
            # Corrupted nonce/ciphertext or AAD mismatch — fail as a storage
            # error, not an unhandled 500.
            raise StorageError(f"Could not decrypt credential for provider '{provider}'") from e

    # ── S3 object I/O ───────────────────────────────────────────────────

    def _read(self, sub: str) -> dict:
        bucket = self.kb_manager._get_s3_bucket()
        key = self._s3_key(sub)
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=key)
        except self.kb_manager.s3_client.exceptions.NoSuchKey:
            return {}
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return {}
            raise
        return json.loads(resp["Body"].read().decode("utf-8"))

    def _write(self, sub: str, obj: dict) -> None:
        bucket = self.kb_manager._get_s3_bucket()
        key = self._s3_key(sub)
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    # ── public API ──────────────────────────────────────────────────────

    def list_providers(self, sub: str) -> list[dict]:
        """``[{provider, username, status}]`` where status is connected or expired."""
        obj = self._read(sub)
        out: list[dict] = []
        for provider, entry in obj.items():
            if not isinstance(entry, dict):
                continue
            status = "expired" if entry.get("invalid") is True else "connected"
            out.append(
                {
                    "provider": provider,
                    "username": entry.get("username", ""),
                    "status": status,
                }
            )
        out.sort(key=lambda e: e["provider"])
        return out

    def has(self, sub: str, provider: str) -> bool:
        entry = self._read(sub).get(provider)
        return isinstance(entry, dict) and all(f in entry for f in _SEALED_FIELDS)

    def get_username(self, sub: str, provider: str) -> Optional[str]:
        entry = self._read(sub).get(provider)
        return entry.get("username") if isinstance(entry, dict) else None

    def get_host(self, sub: str, provider: str) -> Optional[str]:
        entry = self._read(sub).get(provider)
        return entry.get("host") if isinstance(entry, dict) else None

    def hosts_by_provider(self, sub: str) -> dict[str, Optional[str]]:
        """``{provider: stored self-hosted host or None}`` for the caller, one read.

        Lets the clone path map a self-hosted git host back to the provider the
        user saved it under (the public-host registry can't know custom hosts).
        """
        return {
            provider: entry.get("host")
            for provider, entry in self._read(sub).items()
            if isinstance(entry, dict)
        }

    def get_token(self, sub: str, provider: str) -> Optional[str]:
        """Decrypt and return the stored token, or ``None`` if not connected."""
        entry = self._read(sub).get(provider)
        if not isinstance(entry, dict) or not all(f in entry for f in _SEALED_FIELDS):
            return None
        return self._unseal(entry, sub, provider)

    def get_credential(self, sub: str, provider: str) -> tuple[Optional[str], Optional[str]]:
        """``(host, token)`` for one provider in a single read; ``token`` decrypted,
        or ``None`` when not connected. Avoids a second read when a caller needs both."""
        entry = self._read(sub).get(provider)
        if not isinstance(entry, dict):
            return None, None
        host = entry.get("host")
        token = (
            self._unseal(entry, sub, provider)
            if all(f in entry for f in _SEALED_FIELDS)
            else None
        )
        return host, token

    def get_credential_state(
        self, sub: str, provider: str
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """``(invalid, host, token)`` for one provider in a single read.

        Replaces the gate's old ``is_invalid`` + ``get_credential`` two-read pair.
        An invalid entry skips unsealing (rejected anyway); otherwise ``token`` is
        decrypted, or ``None`` when not connected.
        """
        entry = self._read(sub).get(provider)
        if not isinstance(entry, dict):
            return False, None, None
        if entry.get("invalid") is True:
            return True, entry.get("host"), None
        token = (
            self._unseal(entry, sub, provider)
            if all(f in entry for f in _SEALED_FIELDS)
            else None
        )
        return False, entry.get("host"), token

    def put(
        self,
        sub: str,
        provider: str,
        username: str,
        token: str,
        host: Optional[str] = None,
    ) -> None:
        """Seal ``token`` and upsert the provider entry. Clears the invalid flag."""
        obj = self._read(sub)
        entry: dict[str, Any] = {"username": username}
        if host:
            entry["host"] = host
        entry.update(self._seal(token, sub, provider))
        entry.pop("invalid", None)  # a new token is presumed valid
        obj[provider] = entry
        self._write(sub, obj)
        logger.info("Stored %s credential for user (sub=%s...)", provider, sub[:8])

    def mark_invalid(self, sub: str, provider: str) -> bool:
        """Flag a stored credential as rejected (a 401). Returns True if newly set."""
        obj = self._read(sub)
        entry = obj.get(provider)
        if not isinstance(entry, dict):
            return False
        if entry.get("invalid") is True:
            return False
        entry["invalid"] = True
        obj[provider] = entry
        self._write(sub, obj)
        logger.info("Flagged %s credential invalid for user (sub=%s...)", provider, sub[:8])
        return True

    def is_invalid(self, sub: str, provider: str) -> bool:
        """True when the stored credential is flagged invalid (expired/revoked)."""
        entry = self._read(sub).get(provider)
        return isinstance(entry, dict) and entry.get("invalid") is True

    def status(self, sub: str, provider: str) -> str:
        """Per-provider status for the UI: ``connected`` | ``not_set`` | ``expired``."""
        entry = self._read(sub).get(provider)
        if not isinstance(entry, dict) or not all(f in entry for f in _SEALED_FIELDS):
            return "not_set"
        return "expired" if entry.get("invalid") is True else "connected"

    def delete(self, sub: str, provider: str) -> bool:
        """Remove one provider entry. Returns True if something was removed."""
        obj = self._read(sub)
        if provider not in obj:
            return False
        obj.pop(provider)
        self._write(sub, obj)
        logger.info("Deleted %s credential for user (sub=%s...)", provider, sub[:8])
        return True


def _random_nonce() -> bytes:
    """12 fresh random bytes for AES-GCM. Never reused for a given data key."""
    import os

    return os.urandom(12)
