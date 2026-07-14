"""Scrub the ``apiKey`` query parameter out of anything that might get logged.

The Sensibo API takes the API key as a query parameter (``?apiKey=...``), not a
header (see ``docs/sensibo-api.md``, "Base URL and auth"). That means every URL
:mod:`sensibo.api` builds carries the raw key, and this is the one place that
knows how to strip it back out before a URL reaches an exception message, a log
line, or a repr.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"
_KEY_PARAM = "apiKey"
_NAIVE_PATTERN = re.compile(r"(apiKey=)[^&\s]+")


def scrub_url(url: str) -> str:
    """Return ``url`` with the ``apiKey`` query-parameter value replaced.

    Safe on a URL with no ``apiKey`` param (returned unchanged) and on a
    malformed URL that ``urlsplit`` can't parse (falls back to a naive regex
    scrub rather than raising — a scrub helper must never itself throw on bad
    input, or a caller's error-handling path could crash while trying to be
    safe).
    """
    try:
        parts = urlsplit(url)
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return _NAIVE_PATTERN.sub(r"\1" + REDACTED, url)

    if not any(key == _KEY_PARAM for key, _ in query_pairs):
        return url

    scrubbed_pairs = [(key, REDACTED if key == _KEY_PARAM else value) for key, value in query_pairs]
    new_query = urlencode(scrubbed_pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def scrub_text(text: str, api_key: str | None = None) -> str:
    """Scrub any ``apiKey=...`` query-param pattern from free text.

    If ``api_key`` (the raw key value) is passed, also blanket-replaces any
    literal occurrence of it — useful when scrubbing a response body or error
    message that might have echoed the key back outside of a URL.
    """
    scrubbed = _NAIVE_PATTERN.sub(r"\1" + REDACTED, text)
    if api_key:
        scrubbed = scrubbed.replace(api_key, REDACTED)
    return scrubbed
