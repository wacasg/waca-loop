"""Safe reference ids for Lite policy/candidate plumbing."""

from __future__ import annotations

import re
from hashlib import sha256

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./#-]{0,119}$")


def safe_source_ref(source_kind: str, source_ref: str) -> str:
    """Return a non-secret source reference.

    Raw URLs, email addresses, query tokens, and free-form pasted text are reduced
    to a stable digest. The caller keeps the original in its own system of record.
    """
    ref = source_ref.strip()
    if _SAFE_REF.fullmatch(ref) and "://" not in ref and "@" not in ref:
        return ref
    digest = sha256(ref.encode("utf-8")).hexdigest()[:16]
    safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "_", source_kind.strip() or "source")[:32]
    return f"{safe_kind}:ref:{digest}"


__all__ = ["safe_source_ref"]
