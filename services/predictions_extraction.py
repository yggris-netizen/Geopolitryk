"""Извлечение прогнозов из транскрипта. Head+Tail. С фиксом 413."""

import json
import re
from datetime import datetime, timezone

from config import (
    EXTRACT_PREDICTIONS, GROQ_MODEL,
    MAX_PREDICTION_HORIZON_YEARS, MAX_PREDICTIONS_PER_VIDEO,
)
from services.llm import groq_client


_SAFE_INPUT_CHARS = 12000
_FALLBACK_INPUT_CHARS = 6000


def _build_prompt(transcript, expert_name, today_str, current_year, max_year):
    return f"""Найди ПРОВЕРЯЕМЫЕ ПРОГНОЗЫ в интервью «{expert_name}».

СЕГОДНЯ: {today_str}. Текущий год: {current_year}.

ПРОГНОЗ — это КОНКРЕТНОЕ утверждение о БУДУЩЕМ, которое можно проверить:
- Есть событие (что именно произойдёт)
- Есть срок/дата (когда)
- Есть проверяемый исход (да/нет, случилось/не случилось)

НЕ ПРОГНОЗ:
- Общие рассуждения («ситуация будет ухудшаться»)
- Оценки прошлого
- Формулировки без конкретики («произойдёт событие, о котором говорится»)
- Прогнозы без срока

ПЛОХИЕ примеры (НЕ включай):
- «До 27 октября 2026 года произойдёт событие, о котором говорится в интервью»
- «Ситуация изменится в ближайшее время»
- «Эксперт считает, что всё будет плохо»

ХОРОШИЕ примеры (включай):
- «В ноябре 2026 года в Абу-Даби пройдут переговоры»
- «Если война продлится 2 года, конфликт закончится до 2029 года»
- «Трамп подпишет указ до конца декабря 2026 года»

ВАЖНО:
- Не используй годы РАНЬШЕ {current_year}
- Горизонт > {MAX_PREDICTION_HORIZON_YEARS} лет — не включай
- Максимум {MAX_PREDICTIONS_PER_VIDEO} прогнозов

JSON-массив строк:
["прогноз 1", "прогноз 2"]

Нет — [].

Текст:
---
{transcript}
---

JSON:"""


def _call_groq(prompt):
    return groq_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=GROQ_MODEL, temperature=0.2, max_tokens=4000,
    )


def _parse_response(raw, current_year, max_year):
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    if not raw:
        return []
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        return []
    try:
        preds = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(preds, list):
        return []
    cleaned = []
    for p in preds:
        if not isinstance(p, str):
            continue
        p = p.strip().strip('"').strip("'").strip("•").strip("-").strip()
        if not (15 <= len(p) <= 400):
            continue
        if "о котором говорится в интервью" in p.lower():
            continue
        years_in_pred = re.findall(r'\b(20\d{2})\b', p)
        if any(int(y) < current_year for y in years_in_pred):
            continue
        if any(int(y) > max_year for y in years_in_pred):
            continue
        cleaned.append(p)
    return cleaned[:MAX_PREDICTIONS_PER_VIDEO]


def _extract_from_text(text, expert_name, today_str, current_year, max_year):
    if not text or not text.strip():
        return []
    safe_text = text if len(text) <= _SAFE_INPUT_CHARS else text[:_SAFE_INPUT_CHARS]
    prompt = _build_prompt(safe_text, expert_name, today_str, current_year, max_year)

    try:
        r = _call_groq(prompt)
    except Exception as e:
        err = str(e).lower()
        if "413" in err or "request_too_large" in err:
            print(f"   ⚠️  413 в extract_predictions ({len(safe_text)} симв.) — уменьшаю")
            shorter = text[:_FALLBACK_INPUT_CHARS]
            prompt = _build_prompt(shorter, expert_name, today_str, current_year, max_year)
            try:
                r = _call_groq(prompt)
            except Exception as e2:
                print(f"⚠️  прогнозы (retry): {e2}")
                return []
        else:
            print(f"⚠️  прогнозы: {e}")
            return []

    try:
        raw = r.choices[0].message.content.strip()
        return _parse_response(raw, current_year, max_year)
    except Exception as e:
        print(f"⚠️  прогнозы (parse): {e}")
        return []


def extract_predictions(transcript, expert_name):
    if not EXTRACT_PREDICTIONS:
        return []
    if not transcript:
        return []
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime('%d.%m.%Y')
    current_year = now_utc.year
    max_year = current_year + MAX_PREDICTION_HORIZON_YEARS

    seen = set()
    all_preds = []

    def _add(preds):
        for p in preds:
            if p not in seen:
                seen.add(p)
                all_preds.append(p)

    # Голова — всегда
    head = transcript[:_SAFE_INPUT_CHARS]
    _add(_extract_from_text(head, expert_name, today_str, current_year, max_year))

    # Хвост — если транскрипт ощутимо длиннее безопасного окна
    if len(transcript) > _SAFE_INPUT_CHARS * 1.5:
        tail_start = max(_SAFE_INPUT_CHARS, len(transcript) - _SAFE_INPUT_CHARS)
        tail = transcript[tail_start:]
        if tail.strip():
            print(f"   🎯 хвост транскрипта ({len(tail)} симв.) — доп. извлечение")
            _add(_extract_from_text(tail, expert_name, today_str, current_year, max_year))

    return all_preds[:MAX_PREDICTIONS_PER_VIDEO]