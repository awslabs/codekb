"""Tests for safe_extract_zip and the upload-size cap it derives from.

safe_extract_zip takes an explicit ``max_extract_bytes`` limit. The zip-bomb
guard rejects an archive whose declared uncompressed total exceeds that limit
before any bytes are written, so a rejected archive leaves no partial
extraction behind. The limit itself is derived by the service from the
configured compressed-upload cap (fail-closed if unconfigured).
"""

import zipfile
from unittest.mock import patch

import pytest

from src.service.codekb_service import CodeKBService, safe_extract_zip

_10_MIB = 10 * 1024 * 1024


def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


class TestSafeExtractZip:
    def test_extracts_normal_archive(self, tmp_path):
        src = _make_zip(tmp_path / "ok.zip", [("a.txt", b"hello"), ("dir/b.txt", b"world")])
        dest = tmp_path / "out"
        with zipfile.ZipFile(src) as zf:
            safe_extract_zip(zf, dest, _10_MIB)
        assert (dest / "a.txt").read_bytes() == b"hello"
        assert (dest / "dir" / "b.txt").read_bytes() == b"world"

    def test_rejects_path_traversal(self, tmp_path):
        src = _make_zip(tmp_path / "evil.zip", [("../escape.txt", b"x")])
        dest = tmp_path / "out"
        with zipfile.ZipFile(src) as zf:
            with pytest.raises(ValueError, match="path traversal|absolute path"):
                safe_extract_zip(zf, dest, _10_MIB)

    def test_rejects_zip_bomb_over_size_cap(self, tmp_path):
        # 2 MiB uncompressed but highly compressible: tiny on disk, large expanded.
        src = _make_zip(tmp_path / "bomb.zip", [("big.bin", b"\0" * (2 * 1024 * 1024))])
        dest = tmp_path / "out"
        with zipfile.ZipFile(src) as zf:
            with pytest.raises(ValueError, match="exceeding the .* extraction limit"):
                safe_extract_zip(zf, dest, 1 * 1024 * 1024)  # 1 MiB cap
        # No partial extraction: dest may exist (mkdir) but must be empty.
        assert not dest.exists() or not any(dest.iterdir())

    def test_allows_archive_within_cap(self, tmp_path):
        src = _make_zip(tmp_path / "ok.zip", [("small.bin", b"\0" * 1024)])
        dest = tmp_path / "out"
        with zipfile.ZipFile(src) as zf:
            safe_extract_zip(zf, dest, 1 * 1024 * 1024)
        assert (dest / "small.bin").stat().st_size == 1024

    def test_non_positive_cap_disables_check(self, tmp_path):
        src = _make_zip(tmp_path / "big.zip", [("big.bin", b"\0" * (2 * 1024 * 1024))])
        dest = tmp_path / "out"
        with zipfile.ZipFile(src) as zf:
            safe_extract_zip(zf, dest, 0)  # 0 disables the size check
        assert (dest / "big.bin").stat().st_size == 2 * 1024 * 1024


class TestMaxUploadBytes:
    def test_reads_configured_cap(self):
        codekb_service = object.__new__(CodeKBService)
        with patch("src.service.codekb_service.get_config", return_value=500):
            assert codekb_service._max_upload_bytes() == 500 * 1024 * 1024

    def test_fails_closed_when_unconfigured(self):
        codekb_service = object.__new__(CodeKBService)
        with patch("src.service.codekb_service.get_config", return_value=None):
            with pytest.raises(RuntimeError, match="max_upload_size_mb is not configured"):
                codekb_service._max_upload_bytes()

    def test_extract_cap_derives_from_upload_cap_and_ratio(self):
        codekb_service = object.__new__(CodeKBService)
        with patch("src.service.codekb_service.get_config", return_value=500):
            expected = 500 * 1024 * 1024 * CodeKBService._MAX_DECOMPRESSION_RATIO
            assert codekb_service._max_extract_bytes() == expected
