"""Tests for sensibo.api._scrub: never let apiKey escape into loggable text."""

from __future__ import annotations

from sensibo.api._scrub import REDACTED, scrub_text, scrub_url


def test_scrub_url_redacts_api_key_value() -> None:
    url = "https://home.sensibo.com/api/v2/users/me/pods?apiKey=SUPERSECRET&fields=%2A"
    scrubbed = scrub_url(url)
    assert "SUPERSECRET" not in scrubbed
    assert REDACTED in scrubbed


def test_scrub_url_preserves_other_query_params() -> None:
    url = "https://home.sensibo.com/api/v2/users/me/pods?apiKey=SUPERSECRET&fields=%2A"
    scrubbed = scrub_url(url)
    assert "fields=%2A" in scrubbed or "fields=*" in scrubbed


def test_scrub_url_preserves_path_and_host() -> None:
    url = "https://home.sensibo.com/api/v2/pods/abc123?apiKey=KEY123"
    scrubbed = scrub_url(url)
    assert "https://home.sensibo.com/api/v2/pods/abc123" in scrubbed


def test_scrub_url_is_noop_when_no_api_key_param() -> None:
    url = "https://home.sensibo.com/api/v2/pods/abc123?fields=%2A"
    assert scrub_url(url) == url


def test_scrub_url_handles_multiple_query_params_with_api_key_anywhere() -> None:
    url = "https://home.sensibo.com/api/v2/pods/x?fields=%2A&apiKey=ABC&limit=20"
    scrubbed = scrub_url(url)
    assert "ABC" not in scrubbed
    assert "limit=20" in scrubbed


def test_scrub_url_falls_back_gracefully_on_malformed_url() -> None:
    # Deliberately malformed (unterminated IPv6 host) - urlsplit raises ValueError.
    malformed = "http://[::1/path?apiKey=SUPERSECRET"
    scrubbed = scrub_url(malformed)
    assert "SUPERSECRET" not in scrubbed


def test_scrub_text_redacts_api_key_query_param_pattern() -> None:
    text = "GET https://home.sensibo.com/api/v2/pods?apiKey=ZZZTOP failed"
    scrubbed = scrub_text(text)
    assert "ZZZTOP" not in scrubbed


def test_scrub_text_also_redacts_a_known_raw_key_value() -> None:
    text = "the key RAWKEYVALUE was rejected"
    scrubbed = scrub_text(text, api_key="RAWKEYVALUE")
    assert "RAWKEYVALUE" not in scrubbed
