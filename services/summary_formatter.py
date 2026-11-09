# -*- coding: utf-8 -*-
"""Форматтер verified тезисов в связный текст (БЕЗ LLM).

Логика:
1. Для каждого verified:
   - claim ≈ quote (fuzzy > 70) -> только quote: «quote» [MM:SS]
   - иначе -> claim — «quote» [MM:SS]
2. Склейка в абзацы (2-3 предложения).
3. Без LLM -> 0 галлюцинаций.
"""

try:
    from rapidfuzz import fuzz as _fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

CLAIM_QUOTE_DEDUP = 70
SENTENCES_PER_PARAGRAPH = 3


def _format_duration(seconds) -> str:
    if not seconds:
        return ""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _normalize(s: str) -> str:
    import re
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_duplicate(claim: str, quote: str) -> bool:
    """claim ≈ quote (fuzzy > 70)?"""
    if not claim or not quote:
        return False
    if not FUZZY_AVAILABLE:
        c = _normalize(claim)
        q = _normalize(quote)
        return c in q or q in c
    c = _normalize(claim)
    q = _normalize(quote)
    if not c or not q:
        return False
    return _fuzz.partial_ratio(c, q) > CLAIM_QUOTE_DEDUP


def _format_one(v: dict) -> str:
    """Форматирует один verified в предложение."""
    claim = (v.get("claim") or "").strip()
    quote = (v.get("quote") or "").strip()
    ts = _format_duration(v.get("start", 0))
    ts_str = f" [{ts}]" if ts else ""

    if not claim and not quote:
        return ""

    # claim ≈ quote -> только quote
    if _is_duplicate(claim, quote) and quote:
        q = quote[:250] + ("…" if len(quote) > 250 else "")
        return f"«{q}»{ts_str}"

    # claim + quote (разные)
    if claim and quote:
        q = quote[:250] + ("…" if len(quote) > 250 else "")
        return f"{claim} — «{q}»{ts_str}"

    # только claim
    if claim:
        return f"{claim}{ts_str}"

    # только quote
    q = quote[:250] + ("…" if len(quote) > 250 else "")
    return f"«{q}»{ts_str}"


def format_verified(verified: list) -> str:
    """Связный текст из verified (абзацы по 2-3 предложения)."""
    if not verified:
        return ""
    sentences = []
    for v in verified:
        s = _format_one(v)
        if s:
            sentences.append(s)
    if not sentences:
        return ""

    paragraphs = []
    for i in range(0, len(sentences), SENTENCES_PER_PARAGRAPH):
        chunk = sentences[i:i + SENTENCES_PER_PARAGRAPH]
        para = ". ".join(chunk)
        if not para.endswith((".", "!", "?")):
            para += "."
        paragraphs.append(para)
    return "\n\n".join(paragraphs)
