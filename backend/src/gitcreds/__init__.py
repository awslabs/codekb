"""Per-user git credentials.

A user saves a personal access token for a git provider; the token is
validated against the provider, stored encrypted (S3 + KMS envelope), and
reused to clone private repositories.

Everything provider-specific sits behind the ``GitProvider`` interface in
``providers``; ``store`` holds the encrypted-at-rest credential objects.
"""
