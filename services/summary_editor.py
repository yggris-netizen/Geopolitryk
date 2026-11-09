# -*- coding: utf-8 -*-
"""D2 v2: связная аналитика из цитат (перефразировка + 2-4 прямые цитаты).

Логика:
1. LLM пишет связный текст (3-5 абзацев), перефразируя цитаты.
2. 2-4 КЛЮЧЕВЫЕ прямые цитаты — дословно в «...» + [MM:SS].
3. Остальное — перефразировка БЕЗ [MM:SS].
4. Код: невалидные [MM:SS] удаляются (только [MM:SS], не предложение).
5. Fallback: < 200 символов -> D1 (format_quotes).
"""

import re

from config import D2_MIN_CHARS
from config import GROQ_TASK_MODELS, GROQ_FALLBACK_MODEL, GROQ_MODEL, SUMMARIZE_FINAL_TOKENS
from services.llm import groq_client, groq_chat_create


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


def _build_prompt(verified: list) -> str:
    items = []
    for v in verified:
        ts = _format_duration(v.get("start", 0))
        quote = (v.get("quote") or "").strip()
        q_short = quote[:300] + ("…" if len(quote) > 300 else "")
        items.append(f'[{ts}] «{q_short}»')
    items_text = "\n".join(items)

    return f"""Ты — аналитик. Напиши СВЯЗНУЮ АНАЛИТИЧЕСКУЮ ВЫЖИМКУ из интервью.

ЦИТАТЫ (ТОЛЬКО они — не добавляй фактов):
{items_text}

ПРАВИЛА:
1. Связный текст (3-5 абзацев), НЕ список, НЕ маркированный список.
2. ПЕРЕФРАЗИРУЙ цитаты, НЕ теряя смысла. Можно менять слова, но смысл — тот же.
3. 2-4 КЛЮЧЕВЫЕ ПРЯМЫЕ ЦИТАТЫ оставь ДОСЛОВНО в кавычках «...» + [MM:SS] после.
4. Остальное — перефразируй БЕЗ [MM:SS].
5. НЕ добавляй фактов, которых нет в цитатах.
6. Тон — нейтральный, аналитический.
7. Без markdown, без «Вот текст:», без пояснений.

ПРИМЕР:
Демура считает, что Трамп — недалёкий человек, действия которого непредсказуемы: «Трамп настолько существо безмозглое, что чем ему в голову взбредет, то и будет» [3:38]. По мнению эксперта, война на Украине и на Ближнем Востоке — это один конфликт [15:28]. Он уверен, что после смены власти в США ничего не изменится [17:26]. «Война – дело молодых, лекарство от морщин», — добавляет Демура [1:29:03].

Верни ТОЛЬКО текст (3-5 абзацев):
"""


def _call_groq(prompt: str, max_tokens: int, task="high") -> str:
    models = GROQ_TASK_MODELS.get(task, [GROQ_MODEL])
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)
    for model in models:
        try:
            kwargs = dict(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.3,
                max_tokens=max_tokens,
            )
            if "gpt-oss" in model:
                try:
                    r = groq_chat_create(reasoning_effort="low", **kwargs)
                except TypeError:
                    r = groq_chat_create(**kwargs)
            else:
                r = groq_chat_create(**kwargs)
            content = (r.choices[0].message.content or "").strip()
            if content:
                return content
        except Exception as e:
            print(f"      ⚠️  editor ({model}): {type(e).__name__}: {e!r}")
            continue
    return ""


def _verify_timestamps(text: str, verified: list) -> tuple:
    """Удаляет невалидные [MM:SS] (только их, не предложения)."""
    valid_ts = set()
    for v in verified:
        ts = _format_duration(v.get("start", 0))
        if ts:
            valid_ts.add(ts)

    found = re.findall(r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]', text)
    removed = 0
    for ts in found:
        if ts not in valid_ts:
            text = text.replace(f"[{ts}]", "")
            removed += 1

    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\s+([.,!?])', r'\1', text)
    return text.strip(), removed


def merge_verified_into_analytics(verified: list) -> str:
    """Связная аналитика из verified. Fallback: D1 (format_quotes)."""
    if not verified:
        return ""

    prompt = _build_prompt(verified)
    text = _call_groq(prompt, max_tokens=SUMMARIZE_FINAL_TOKENS)
    if not text:
        print("      ⚠️  editor: пусто -> fallback D1")
        from services.quote_formatter import format_quotes
        return format_quotes(verified)

    text = re.sub(r'^```(?:\w+)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text).strip()

    verified_text, removed = _verify_timestamps(text, verified)
    if removed:
        print(f"      🔍 timestamp-check: удалено {removed} невалидных [MM:SS]")

    if len(verified_text) < D2_MIN_CHARS:
        print(f"      ⚠️  editor: {len(verified_text)} < {D2_MIN_CHARS} -> fallback D1")
        from services.quote_formatter import format_quotes
        return format_quotes(verified)

    return verified_text
