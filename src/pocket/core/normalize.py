"""Stage 0 (§4.1): clean the raw text before anything reads it.

Case is kept on purpose (merchant names, notes). The raw message is stored
untouched; this only affects what the pipeline sees.
"""

from __future__ import annotations

import re
import unicodedata

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"})
_SIGNATURE = re.compile(
    r"\n+\s*(?:--\s*\n.*|sent from my \w+.*|get outlook for \w+.*|sent via \w+.*)$",
    re.IGNORECASE | re.DOTALL,
)
_WS = re.compile(r"[ \t ]+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _ZERO_WIDTH.sub("", t)
    t = t.translate(_DEVANAGARI_DIGITS).translate(_QUOTES)
    t = _SIGNATURE.sub("", t)
    t = _WS.sub(" ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
