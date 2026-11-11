# -*- coding: utf-8 -*-
"""Проверка цитат: exact/fuzzy>90 + фильтр мусора (приветствия, вопросы, обрывки)."""
import json
import re

try:
    from rapidfuzz import fuzz as _fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

from scripts.verify_summary import _find_quote_in_segments
from config import MAX_QUOTES

DEDUP_THRESHOLD = 90
# MAX_QUOTES импортируется из config
MIN_QUOTE_LEN = 40

# Мусорные фразы (приветствия, прощания, междометия)
_BAD_PHRASES = (
    # приветствия
    "добрый вечер", "добрый день", "доброе утро", "здравствуйте",
    "всем спасибо", "всем большое спасибо", "присоединяетесь",
    "друзья, всем", "рад вас видеть", "рад приветствовать",
    # прощания
    "до свидания", "не теряйте оптимизм", "спасибо за внимание",
    "всем пока", "увидимся", "с вами был",
    # междометия
    "да, да", "да да", "нет, нет", "нет нет",
    "это показывает уровень", "это всё", "вот так вот",
    "ну вот", "ну что", "в общем-то", "как-то так",
)

# Слова, с которых не должна начинаться цитата (обрывки, вопросы ведущего)
_BAD_STARTS = (
    "котор", "этот", "эта", "это ", "его ", "её ", "ее ", "их ",
    "там ", "тут ", "тогда ", "потом ", "затем ", "также ",
    "причем", "причём", "поэтому ", "но ", "и ", "а ",
    "вопрос ",   # цитата = вопрос ведущего
)


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _is_garbage(quote: str) -> str:
    q = (quote or "").strip()
    if len(q) < MIN_QUOTE_LEN:
        return f"too_short ({len(q)}<{MIN_QUOTE_LEN})"

    # Вопрос, а не цитата эксперта
    if q.rstrip().endswith("?"):
        return "is_question"

    q_low = q.lower()
    for bad in _BAD_PHRASES:
        if bad in q_low:
            return f"bad_phrase ({bad!r})"

    q_stripped = q_low.lstrip("«\"' ")
    for bad in _BAD_STARTS:
        if q_stripped.startswith(bad):
            return f"bad_start ({bad!r})"

    letters = sum(1 for c in q if c.isalpha())
    if letters < 15:
        return f"few_letters ({letters})"
    return ""


def _is_dup(quote: str, existing: list) -> bool:
    if not FUZZY_AVAILABLE:
        q = _normalize(quote)
        for e in existing:
            eq = _normalize(e.get("quote", ""))
            if q and (q in eq or eq in q):
                return True
        return False
    q = _normalize(quote)
    if not q:
        return False
    for e in existing:
        eq = _normalize(e.get("quote", ""))
        if not eq:
            continue
        if _fuzz.partial_ratio(q, eq) > DEDUP_THRESHOLD:
            return True
    return False


def verify_quotes(quotes_json: str, segments: list):
    empty = {"total": 0, "not_found": 0, "garbage": 0, "duplicates": 0, "verified": 0}
    if not quotes_json or not segments:
        return [], empty

    try:
        quotes = json.loads(quotes_json)
    except json.JSONDecodeError:
        return [], empty

    if not isinstance(quotes, list):
        return [], empty

    stats = {"total": len(quotes), "not_found": 0, "garbage": 0, "duplicates": 0, "verified": 0}
    verified = []

    for q in quotes:
        if not isinstance(q, dict):
            continue
        quote = (q.get("quote") or "").strip()
        if not quote:
            continue

        reason = _is_garbage(quote)
        if reason:
            stats["garbage"] += 1
            print(f"   [DEL]  garbage ({reason}): {quote[:50]}")
            continue

        if _is_dup(quote, verified):
            stats["duplicates"] += 1
            print(f"   [RETRY] duplicate: {quote[:50]}")
            continue

        seg, seg_idx = _find_quote_in_segments(quote, segments)
        if not seg:
            stats["not_found"] += 1
            print(f"   [ERR] not_found: {quote[:60]}")
            continue

        verified.append({
            "quote": quote,
            "start": float(seg.get("start", 0)),
            "seg_idx": seg_idx,
        })
        stats["verified"] += 1

    verified.sort(key=lambda v: v.get("start", 0))
    if len(verified) > MAX_QUOTES:
        verified = verified[:MAX_QUOTES]

    print(f"   [OK] verified: {len(verified)}")
    return verified, stats