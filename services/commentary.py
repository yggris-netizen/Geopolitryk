# -*- coding: utf-8 -*-
"""D3: Нейтральный пересказ интервью (Map-Reduce, без NLI).

A+C: Map возвращает ТОЛЬКО question + start (без quote) — цитата берётся из сегментов.
C: чередование моделей Map — чётные чанки (1,3,5) → low (20b), нечётные (2,4,6) → medium (120b).
   Разные TPD-корзины → меньше TPD -> fallback.
Map: max_tokens=3000.
Reduce: task="high" (qwen) — нейтральный пересказ.
Fallback: fuzzy 85 (если код не нашёл сегмент).
Имена/названия: _fix_names (Whisper-ошибки).
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


def _safe_float(value, default: float = 0.0) -> float:
    """Безопасное приведение к float (защита от None, str, etc)."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
# A+C: цитата из сегментов (вокруг start)
# ═══════════════════════════════════════════════════════════
def _quote_from_segments(start: float, segments: list,
                         window: int = 2, max_len: int = 500) -> str:
    """Берёт цитату из сегментов вокруг start (±window)."""
    if not segments or start is None:
        return ""

    best_idx = -1
    best_diff = float("inf")
    for i, seg in enumerate(segments):
        seg_start = _safe_float(seg.get("start"), 0.0)
        diff = abs(seg_start - start)
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    if best_idx < 0:
        return ""

    lo = max(0, best_idx - window)
    hi = min(len(segments), best_idx + window + 1)
    quote_segments = segments[lo:hi]

    text = " ".join((s.get("text") or "").strip() for s in quote_segments).strip()
    if not text:
        return ""

    if len(text) > max_len:
        truncated = text[:max_len]
        for sep in (". ", "! ", "? "):
            pos = truncated.rfind(sep)
            if pos > max_len * 0.5:
                truncated = truncated[:pos + 1]
                break
        text = truncated.strip()

    return text


# ═══════════════════════════════════════════════════════════
# Map: извлечение тезисов + question + start (без quote)
# ═══════════════════════════════════════════════════════════
def _extract_theses_chunk(chunk: str, chunk_offset_sec: float = 0,
                          task: str = "low") -> dict:
    """Map: важные тезисы + связки (только question + start).

    task="low" (20b) / "medium" (120b) — чередование (C).
    """
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
   - question: дословный вопрос ведущего (скопируй символ в символ).
   - start: секунды от начала фрагмента (когда начинается ОТВЕТ эксперта).

Верни ТОЛЬКО JSON, без markdown, без пояснений:
{{"theses": ["тезис 1", "тезис 2"], "qa_pairs": [{{"question": "...", "start": 0.0}}]}}

JSON:"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,  # C: 2000 -> 3000 (JSON не обрезан)
            temperature=0.2,
            reasoning_effort="low",
            task=task,  # C: "low" (20b) или "medium" (120b)
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
                if isinstance(qa, dict):
                    start_raw = qa.get("start")
                    qa["start"] = _safe_float(start_raw, 0.0) + chunk_offset_sec
            return {"theses": theses, "qa_pairs": qa_pairs}
    except Exception as e:
        print(f"      [WARN]  theses: {type(e).__name__}: {e!r}")
    return {"theses": [], "qa_pairs": []}


# ═══════════════════════════════════════════════════════════
# A: проверка qa_pairs — цитата из сегментов (fallback: fuzzy 85)
# ═══════════════════════════════════════════════════════════
def _verify_qa_pairs(qa_pairs: list, segments: list) -> list:
    verified = []
    for qa in qa_pairs:
        question = (qa.get("question") or "").strip()
        start = _safe_float(qa.get("start"), 0.0)
        if not question:
            continue

        quote = _quote_from_segments(start, segments, window=2, max_len=500)

        if not quote:
            seg, seg_idx = _find_quote_in_segments(question, segments)
            if seg:
                idx = seg_idx if seg_idx is not None else 0
                lo = max(0, idx - 1)
                hi = min(len(segments), idx + 3)
                quote = " ".join((s.get("text") or "").strip() for s in segments[lo:hi]).strip()

        if quote and len(quote) >= 10:
            verified.append({
                "question": question,
                "quote": quote,
                "start": start,
            })
            print(f"   [OK] qa verified: {quote[:60]}")
        else:
            print(f"   [ERR] qa not_found: {question[:60]}")
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
# Главная: D3 (один проход Map, чередование моделей)
# ═══════════════════════════════════════════════════════════
def generate_commentary(transcript_text: str, transcript_segments: list) -> str:
    """D3: Map-Reduce, один проход, без NLI (A+C, чередование)."""
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
                offset = _safe_float(seg.get("start"), 0.0)
                break
        chunks.append((chunk, offset))

    all_theses = []
    all_qa = []
    for idx, (chunk, off) in enumerate(chunks, 1):
        # C: чередование — нечётные (1,3,5) → low (20b), чётные (2,4,6) → medium (120b)
        task = "low" if (idx % 2 == 1) else "medium"
        print(f"      {idx}/{len(chunks)} (task={task})...")
        result = _extract_theses_chunk(chunk, off, task=task)
        all_theses.extend(result.get("theses", []))
        all_qa.extend(result.get("qa_pairs", []))
        if idx < len(chunks):
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)

    print("[SEARCH] Проверка цитат (A: из сегментов)...")
    verified_qa = _verify_qa_pairs(all_qa, transcript_segments)

    print("[STYLE] Reduce: пересказ...")
    text = _build_commentary(all_theses, verified_qa)
    if len(text) < COMMENTARY_MIN_CHARS:
        print(f"      [WARN]  commentary < {COMMENTARY_MIN_CHARS} -> fallback")
        text = _fallback_theses(all_theses, verified_qa)

    return _fix_names(text.strip())