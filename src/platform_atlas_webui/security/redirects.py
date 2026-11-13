"""Same-origin redirect URL builder.

Redirects that embed a request value (an environment or session name) are built
here from a fixed local prefix plus one percent-encoded path segment, so the
result can never carry a scheme, host, ``//`` prefix, or path escape.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote, urlencode

from fastapi import HTTPException


def local_url(
    prefix: str,
    segment: str,
    *,
    suffix: str = "",
    query: Mapping[str, str] | None = None,
) -> str:
    """Return ``<prefix>/<segment><suffix>[?<query>]`` as a same-origin path.

    ``prefix`` is a trusted literal such as ``"/environments"`` (one leading
    ``/``, never ``//``). ``segment`` is encoded with no safe characters, so a
    ``/`` in it becomes ``%2F``. Raises HTTPException(400) for an empty segment
    or a ``.`` / ``..`` segment.
    """
    if not prefix.startswith("/") or prefix.startswith("//"):
        raise ValueError(f"redirect prefix must be a local path: {prefix!r}")
    if segment in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Invalid name")
    url = f"{prefix}/{quote(segment, safe='')}{suffix}"
    if query:
        url += "?" + urlencode(query)
    return url
