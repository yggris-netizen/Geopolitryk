# -*- coding: utf-8 -*-
"""D3: Нейтральный пересказ интервью (Map-Reduce, без NLI).

Один проход Map (task="medium" -> 120b): theses + qa_pairs.
Код-проверка цитат: _find_quote_in_segments (in/fuzzy).
Reduce (task="high" -> qwen): нейтральный пересказ, близко к тексту.
Имена/названия: _fix_names (Whisper-ошибки).
Fallback: простой пересказ из тезисов.
"""

import re
import time

from config import (
    COMMENTARY_CHUNK_CHARS, COMMENTARY_FINAL_TOKENS,
    COMMENTARY_MIN_CHARS, MAX_COMMENTARY_QUOTES,
    GROQ_DELAY_BETWEEN_CHUNKS,
)
from services.summarization import _call_groq_with_retry, _extract_json
from scripts.verify_summary import _find_quote_in_segments


def _fmt_dur(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# ═══════════════════════════════════════════════════════════
# D3: исправление имён/названий (Whisper-ошибки)
# ═══════════════════════════════════════════════════════════
_NAME_FIXES = [
    ("Армус", "Ормуз"), ("армус", "Ормуз"), ("АРМУС", "ОРМУЗ"),
    ("Хексет", "Хегсет"), ("хексет", "Хегсет"),
    ("Хексета", "Хегсета"), ("ХЕКСЕТ", "ХЕГСЕТ"),
    ("Старлинг", "Старлинк"), ("старлинг", "Старлинк"),
    ("СТАРЛИНГ", "СТАРЛИНК"),
    ("Диппи Морган", "JP Morgan"), ("диппи морган", "JP Morgan"),
    ("Кёнинсберг", "Кёнигсберг"), ("кёнинсберг", "Кёнигсберг"),
    ("Кенингсберг", "Кёнигсберг"), ("кенингсберг", "Кёнигсберг"),

    # Геополитические акронимы (ИМЭМО РАН, списки сокращений)
    ("ТАТЭС", "АТЭС"), ("татэс", "АТЭС"), ("Татэс", "АТЭС"),
    ("АСЕСЕАН", "АСЕАН"), ("асесеан", "АСЕАН"), ("Асесеан", "АСЕАН"),
    ("АСЕАН", "АСЕАН"),  # оставить (часто правильно)
    ("БРИКС", "БРИКС"),  # оставить
    ("ШОС", "ШОС"),      # оставить
    ("ОДКБ", "ОДКБ"),    # оставить
    ("СНВ", "СНВ"),      # оставить
    ("ДНЯО", "ДНЯО"),    # оставить
    ("МАГАТЭ", "МАГАТЭ"),# оставить
    ("ЕАЭС", "ЕАЭС"),    # оставить
    ("СВО", "СВО"),      # оставить
    ("СВПД", "СВПД"),    # оставить

]


def _fix_names(text: str) -> str:
    """Исправляет имена/названия в тексте (Whisper-ошибки)."""
    if not text:
        return text
    for wrong, right in _NAME_FIXES:
        text = text.replace(wrong, right)
    return text


# ═══════════════════════════════════════════════════════════
# Map: извлечение тезисов + qa_pairs из чанка
# ═══════════════════════════════════════════════════════════
def _extract_theses_chunk(chunk: str, chunk_offset_sec: float = 0) -> dict:
    """Map: важные тезисы + связки "вопрос→ответ" из чанка."""
    prompt = f"""Ты — аналитик. Прочитай фрагмент интервью и выдели ТОЛЬКО важное.

ТЕКСТ:
---
{chunk}
---

ЗАДАЧА:
1. Найди ВАЖНЫЕ ТЕЗИСЫ по темам: геополитика, политика, экономика.
2. ИГНОРИРУЙ: приветствия, даты ("сегодня 9 ноября"), шутки, междометия, воду ("на самом деле", "вот-вот").
3. Тезисы формулируй БЛИЗКО К ТЕКСТУ: сохраняй смысл и формулировки эксперта, но ИСПРАВЛЯЙ ошибки распознавания (например, «Армус» → «Ормуз», «Хексет» → «Хегсет»). 1-2 предложения на тезис, грамматически корректно.
4. Если есть ЧЁТКАЯ СВЯЗКА "вопрос ведущего -> развёрнутый ответ эксперта" — выдели отдельно:
   - question: дословный вопрос ведущего
   - quote: дословный ответ эксперта (1-3 предложения)
   - start: секунды от начала фрагмента

Верни ТОЛЬКО JSON, без markdown, без пояснений:
{{"theses": ["тезис 1", "тезис 2"], "qa_pairs": [{{"question": "...", "quote": "...", "start": 0.0}}]}}

JSON:"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.2,
            reasoning_effort="low",
            task="low",
        )
        if not raw:
            print(f"      [WARN]  theses: пустой ответ")
            return {"theses": [], "qa_pairs": []}
        data = _extract_json(raw)
        if data is None:
            print(f"      [WARN]  theses: не JSON, raw={raw[:150]!r}")
            return {"theses": [], "qa_pairs": []}
        if isinstance(data, dict):
            theses = data.get("theses", []) or []
            qa_pairs = data.get("qa_pairs", []) or []
            for qa in qa_pairs:
                if isinstance(qa, dict) and "start" in qa:
                    qa["start"] = float(qa.get("start", 0)) + chunk_offset_sec
            return {"theses": theses, "qa_pairs": qa_pairs}
    except Exception as e:
        print(f"      [WARN]  theses: {type(e).__name__}: {e!r}")
    return {"theses": [], "qa_pairs": []}


# ═══════════════════════════════════════════════════════════
# Код-проверка: цитаты есть в транскрипте? (in/fuzzy)
# ═══════════════════════════════════════════════════════════
def _verify_qa_pairs(qa_pairs: list, segments: list) -> list:
    verified = []
    for qa in qa_pairs:
        quote = (qa.get("quote") or "").strip()
        if not quote:
            continue
        seg, seg_idx = _find_quote_in_segments(quote, segments)
        if seg:
            verified.append({
                "question": qa.get("question", ""),
                "quote": quote,
                "start": float(seg.get("start", 0)),
            })
            print(f"   [OK] qa verified: {quote[:60]}")
        else:
            print(f"   [ERR] qa not_found: {quote[:60]}")
    return verified


# ═══════════════════════════════════════════════════════════
# Reduce: нейтральный пересказ
# ═══════════════════════════════════════════════════════════
def _build_commentary(theses: list, qa_pairs: list) -> str:
    if not theses and not qa_pairs:
        return ""

    theses_text = "\n".join(f"- {t}" for t in theses[:20]) if theses else "(нет)"
    qa_text = "\n".join(
        f'- Вопрос: «{qa.get("question", "")}» -> Ответ: «{qa.get("quote", "")}» [{_fmt_dur(qa.get("start", 0))}]'
        for qa in qa_pairs[:MAX_COMMENTARY_QUOTES]
    ) if qa_pairs else "(нет)"

    prompt = f"""Ты — аналитик. Напиши нейтральный пересказ интервью.

ТЕЗИСЫ (из интервью):
{theses_text}

СВЯЗКИ "вопрос->ответ" (цитаты, проверенные):
{qa_text}

ПРАВИЛА:
1. Связный текст (3-5 абзацев), нейтральный, информативный.
2. Фактов НЕ добавляй — только из тезисов/цитат.
3. Пиши БЛИЗКО К ТЕКСТУ: пересказывай тезисы, не добавляй от себя, минимум словесных оборотов. Грамматически корректный литературный русский, без ошибок.
4. Шутки/междометия эксперта — НЕ упоминай.
5. Для 1-3 КЛЮЧЕВЫХ связок — используй прямые цитаты: «На вопрос "..." эксперт ответил: "..." [MM:SS]». Имена собственные и географические названия можно исправлять (Армус → Ормуз, Хексет → Хегсет, Старлинг → Старлинк, Диппи Морган → JP Morgan, Кёнинсберг → Кёнигсберг), не меняя смысл.
6. Тон — нейтральный, аналитический, БЕЗ сарказма и оценок.
7. Без markdown, без "Вот текст:", без пояснений.

Верни ТОЛЬКО текст (3-5 абзацев):
"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=COMMENTARY_FINAL_TOKENS,
            temperature=0.5,
            reasoning_effort="low",
            task="high",
        )
        return (raw or "").strip()
    except Exception as e:
        print(f"      [WARN]  commentary: {type(e).__name__}: {e!r}")
        return ""


def _fallback_theses(theses: list, qa_pairs: list) -> str:
    parts = []
    if theses:
        parts.append(" ".join(t.strip() for t in theses[:8] if t))
    for qa in qa_pairs[:2]:
        q = qa.get("quote", "")
        if q:
            parts.append(f"«{q}»")
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════
# Главная: D3 (один проход Map, без consensus)
# ═══════════════════════════════════════════════════════════
def generate_commentary(transcript_text: str, transcript_segments: list) -> str:
    """D3: Map-Reduce, один проход, без NLI."""
    if not transcript_text or not transcript_segments:
        return ""

    chunks = []
    for i in range(0, len(transcript_text), COMMENTARY_CHUNK_CHARS):
        chunk = transcript_text[i:i + COMMENTARY_CHUNK_CHARS]
        offset = 0.0
        acc = 0
        for seg in transcript_segments:
            acc += len(seg.get("text", "")) + 1
            if acc >= i:
                offset = float(seg.get("start", 0))
                break
        chunks.append((chunk, offset))

    all_theses = []
    all_qa = []
    for idx, (chunk, off) in enumerate(chunks, 1):
        print(f"      {idx}/{len(chunks)}...")
        result = _extract_theses_chunk(chunk, off)
        all_theses.extend(result.get("theses", []))
        all_qa.extend(result.get("qa_pairs", []))
        if idx < len(chunks):
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)

    print("[SEARCH] Проверка цитат (in/fuzzy)...")
    verified_qa = _verify_qa_pairs(all_qa, transcript_segments)

    print("[STYLE] Reduce: пересказ...")
    text = _build_commentary(all_theses, verified_qa)
    if len(text) < COMMENTARY_MIN_CHARS:
        print(f"      [WARN]  commentary < {COMMENTARY_MIN_CHARS} -> fallback")
        text = _fallback_theses(all_theses, verified_qa)

    return _fix_names(text.strip())