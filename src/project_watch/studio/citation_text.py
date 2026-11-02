"""Prose cleanup shared by analysis generation and the published view."""
import re

_INLINE_CODES = re.compile(r"\s*[（(]\s*[A-Z]\d{1,3}(?:\s*[、,，/]\s*[A-Z]\d{1,3})*\s*[)）]")


def strip_inline_codes(text) -> str:
    """Source codes written into prose, e.g. "（S2、S22）". Citations live in refs, which are renumbered for display."""
    return _INLINE_CODES.sub("", str(text or "")).strip()
