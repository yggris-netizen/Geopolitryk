"""Извлечение прогнозов из транскрипта. Оптимизировано под TPM Groq free tier."""

import json
import re
import time
from datetime import datetime, timezone

from config import (
    EXTRACT_PREDICTIONS, GROQ_MODEL,
    MAX_PREDICTION_HORIZON_YEARS, MAX_PREDICTIONS_PER_VIDEO,
)
from services.llm import groq_client


# TPM Groq free tier = 8000/мин. Один запрос не должен быть больше ~4000 токенов.
# 6000 символов русского ≈ 3000-4000 входных токенов + 2000 выходных = ~6000 токенов.
_SAFE_INPUT_CHARS = 6000
_FALLBACK_INPUT_CHARS = 3000
_MAX_OUTPUT_TOKENS = 2000


def _build_prompt(transcript, expert_name, today_str, current_year, max_year):
    return f"""Найди ПРОВЕРЯЕМЫЕ ПРОГНОЗЫ в интервью «{expert_name}».

СЕГОДНЯ: {today_str}. Текущий год: {current_year}.

ПРОГНОЗ — конкретное утверждение о БУДУЩЕМ с проверяемым исходом:
- Есть событие (что произойдёт)
- Есть срок/дата (когда)
- Можно проверить (да/нет)

НЕ ПРОГНОЗ:
- Общие рассуждения («ситуация ухудшится»)
- Оценки прошлого
- Без конкретики («произойдёт событие, о котором говорится»)
- Без срока

ПЛОХО (НЕ включай):
- «До 27 октября 2026 года произойдёт событие, о котором говорится в интервью»
- «Ситуация изменится в ближайшее время»

ХОРОШО (включай):
- «В ноябре 2026 года в Абу-Даби пройдут переговоры»
- «Трамп подпишет указ до конца декабря 2026 года»

ВАЖНО:
- Годы РАНЬШЕ {current_year} — не включай
- Горизонт > {MAX_PREDICTION_HORIZON_YEARS} лет — не включай
- Максимум {MAX_PREDICTIONS_PER_VIDEO} прогнозов
- Коротко, до 300 символов каждый

JSON-массив:
["прогноз 1", "прогноз 2"]

Нет — [].

Текст:
---
{transcript}
---

JSON:"""


def _call_groq(prompt):
    """Groq-вызов с retry при 429 (TPM)."""
    last_err = None
    for attempt in range(5):
        try:
            return groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=GROQ_MODEL, temperature=0.2, max_tokens=_MAX_OUTPUT_TOKENS,
            )
        except Exception as e:
            last_err = e
            err = str(e).lower()
            if "429" in err or "rate_limit" in err:
                m = re.search(r'try again in ([\d.]+)s', str(e))
                wait = float(m.group(1)) + 5 if m else 45
                wait = max(wait, 45)
                print(f"      ⏸  429 [predictions], ждём {wait:.0f}с (попытка {attempt+1}/5)")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Groq: 429 после 5 попыток: {last_err}")


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


def extract_predictions(transcript, expert_name):
    if not EXTRACT_PREDICTIONS:
        return []
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime('%d.%m.%Y')
    current_year = now_utc.year
    max_year = current_year + MAX_PREDICTION_HORIZON_YEARS

    safe_text = transcript if len(transcript) <= _SAFE_INPUT_CHARS else transcript[:_SAFE_INPUT_CHARS]
    prompt = _build_prompt(safe_text, expert_name, today_str, current_year, max_year)

    try:
        r = _call_groq(prompt)
    except Exception as e:
        err = str(e).lower()
        if "413" in err or "request_too_large" in err:
            print(f"   ⚠️  413 в extract_predictions ({len(safe_text)} симв.) — уменьшаю до {_FALLBACK_INPUT_CHARS}")
            safe_text = transcript[:_FALLBACK_INPUT_CHARS]
            prompt = _build_prompt(safe_text, expert_name, today_str, current_year, max_year)
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