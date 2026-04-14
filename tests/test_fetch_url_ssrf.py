from __future__ import annotations

import pytest

from certified_turtles.tools.fetch_url import _is_safe_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/secret",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://evil.com/",
    ],
)
def test_ssrf_blocked(url):
    assert not _is_safe_url(url)


def test_public_url_allowed():
    # dns resolution might fail in CI, so we test with a well-known public IP
    assert _is_safe_url("http://8.8.8.8/")
