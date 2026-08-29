"""Safe lexical query helpers."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[\w]+", flags=re.UNICODE)


def fts_phrase_query(value: str) -> str:
    """Turn user text into an FTS expression containing only quoted terms.

    FTS5 has its own query language.  Passing arbitrary user text to ``MATCH``
    would expose operators and malformed syntax even when SQL parameters are
    used, so every extracted term is emitted as a quoted phrase.
    """

    terms = _TOKEN.findall(value.casefold())
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def like_pattern(value: str) -> str:
    """Escape a value for a ``LIKE ... ESCAPE '\\'`` fallback."""

    escaped = value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
