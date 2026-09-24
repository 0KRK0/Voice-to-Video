"""One rule for what a configured provider endpoint means.

There were two rules, which is one too many. `HttpTextGenerationProvider` and
`HttpImageGenerationProvider` appended their own path to whatever was
configured, so they wanted a *base* — `https://api.openai.com/v1`. The speech
adapters used the configured value verbatim, so they wanted a *full* URL —
`https://api.openai.com/v1/audio/transcriptions`. Nothing said so. `.env.example`
listed all five together under one heading as though they took the same kind of
value.

The failure that produces is quiet and confusing: an operator sets every
endpoint to the full URL, which is the natural reading, and speech works while
text and images return 404 from the vendor. The error surfaces as "the provider
rejected us", so it reads as a credential problem, and the credential is fine.

So: accept both. Each adapter names the path it speaks, and this decides whether
the configured value already carries it.

Deliberately not clever. It does not guess at near-misses or rewrite a path that
points somewhere real but wrong — `…/v1/responses` configured for an adapter
that speaks `/chat/completions` is a different API, not a formatting variant,
and silently redirecting it would hide a genuine misconfiguration behind a
request that happens to work.
"""

from __future__ import annotations


def resolve_endpoint(endpoint: str, path: str) -> str:
    """The URL to call, from a configured endpoint that may be base or full.

    ``path`` is the adapter's own suffix, with a leading slash
    (``"/chat/completions"``). A configured endpoint that already ends with it
    is used unchanged; anything else has it appended.

    >>> resolve_endpoint("https://api.openai.com/v1", "/chat/completions")
    'https://api.openai.com/v1/chat/completions'
    >>> resolve_endpoint("https://api.openai.com/v1/chat/completions", "/chat/completions")
    'https://api.openai.com/v1/chat/completions'
    """
    base = endpoint.rstrip("/")
    suffix = "/" + path.strip("/")
    return base if base.endswith(suffix) else base + suffix


__all__ = ["resolve_endpoint"]
