"""Safe JSON embedding for inline ``<script>`` blocks (SEC-08)."""

from __future__ import annotations

import json
from typing import Any


def script_json(obj: Any) -> str:
    """Serialize *obj* to JSON that is safe to place inside ``<script>``.

    Escapes ``<``, ``>``, ``&`` and U+2028/2029 as ``\\uXXXX`` so ``</script>``
    or ``<!--`` inside a value cannot break out of the script element. The
    result is still valid JSON/JS that parses to the identical value.
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
