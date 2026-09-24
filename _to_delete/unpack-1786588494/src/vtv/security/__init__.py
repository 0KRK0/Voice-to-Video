"""Stage 25 — the security layer.

Gathered into one package so that a reviewer auditing "how does this system
decide who may do what, and what does it refuse to accept" reads one directory
rather than following a thread through the whole codebase.

Everything here fails closed. An unrecognised credential, an unresolvable
permission, a file whose bytes do not match its claimed type, a URL that
resolves to private address space — all are refused, and refusal is recorded.
"""

from vtv.security.audit import AuditLog, redact
from vtv.security.authz import Authorizer, require
from vtv.security.keys import ApiKeySecret, hash_secret, mint_api_key, verify_secret
from vtv.security.limits import RateLimiter, RateLimitPolicy, RateLimitVerdict
from vtv.security.net import UrlGuard, is_public_address
from vtv.security.paths import safe_join, safe_storage_key
from vtv.security.uploads import UploadPolicy, UploadVerdict, inspect_upload

__all__ = [
    "ApiKeySecret",
    "AuditLog",
    "Authorizer",
    "RateLimitPolicy",
    "RateLimitVerdict",
    "RateLimiter",
    "UploadPolicy",
    "UploadVerdict",
    "UrlGuard",
    "hash_secret",
    "inspect_upload",
    "is_public_address",
    "mint_api_key",
    "redact",
    "require",
    "safe_join",
    "safe_storage_key",
    "verify_secret",
]
