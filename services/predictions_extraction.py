"""Извлечение прогнозов. Каскад моделей на TPD + фикс 413."""

import json
import re
import time
from datetime import datetime, timezone

from config import (GROQ_TASK_MODELS, 
    EXTRACT_PREDICTIONS, GROQ_FALLBACK_MODEL,
    GROQ_MAX_RETRIES, GROQ_MODEL,
    MAX_PREDICTION_HORIZON_YEARS, MAX_PREDICTIONS_PER_VIDEO,
)
from services.llm import groq_client, groq_chat_create
from db.token_usage import mark_exhausted


_SAFE_INPUT_CHARS = 6000
_FALLBACK_INPUT_CHARS = 3000
_MAX_OUTPUT_TOKENS = 2000


def _build_prompt(transcript, expert_name, today_str, current_year, max_year):
    return f"""Найди ПРОГНОЗЫ и МНЕНИЯ О БУДУЩЕМ в интервью «{expert_name}».

СЕГОДНЯ: {today_str}. Текущий год: {current_year}.

ПРОГНОЗ — ЛЮБОЕ утверждение спикера о БУДУЩЕМ (мнение, вероятность, сценарий):
- Событие (что может произойти) — даже без точной даты
- Мнение: «я думаю», «по моему мнению», «вероятно», «может быть»
- Сценарий: «если X, то Y», «в случае Z — W»
- Срок: дата, «в ближайшее время», «до конца года», «скоро» — любой

ВКЛЮЧАЙ:
- «Может быть, Трамп подпишет указ» (без даты)
- «Если Европа не договорится, будет война» (сценарий)
- «Я думаю, переговоры пройдут в ноябре» (мнение + дата)
- «Вероятно, ситуация изменится» (вероятность)

НЕ ПРОГНОЗ (только прошлое):
- «Он сказал вчера» (прошлое)
- «Это было в 2024» (прошлое)

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
    """Groq с каскадом моделей: TPD → fallback, TPM → retry."""
    models = list(GROQ_TASK_MODELS["high"])  # copy: не мутировать config
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)

    last_err = None
    for mi, model in enumerate(models):
        is_last = (mi == len(models) - 1)
        for attempt in range(GROQ_MAX_RETRIES):
            try:
                return groq_chat_create(
                    task="high",
                    messages=[{"role": "user", "content": prompt}],
                    model=model, temperature=0.2, max_tokens=_MAX_OUTPUT_TOKENS,
                )
            except Exception as e:
                last_err = e
                err = str(e).lower()

                if "tpd_exhausted" in err:
                    short = model.split("/")[-1]
                    if not is_last:
                        next_short = models[mi + 1].split("/")[-1]
                        print(f"      [TPD] [predictions] {short} → {next_short}")
                        break
                    raise RuntimeError(f"TPD exhausted on all models (predictions): {e}")

                if "tokens per day" in err or "tpd" in err:
                    try:
                        mark_exhausted(model, "high")
                    except Exception:
                        pass
                    short = model.split("/")[-1]
                    if not is_last:
                        next_short = models[mi + 1].split("/")[-1]
                        print(f"      [RETRY] TPD [predictions] {short} → {next_short}")
                        break
                    raise RuntimeError(f"TPD exhausted on all models (predictions): {e}")

                if ("429" in err or "rate_limit" in err) and attempt < GROQ_MAX_RETRIES - 1:
                    wait = 30 * (2 ** attempt)
                    m = re.search(r'try again in ([\d.]+)s', str(e))
                    if m:
                        wait = max(float(m.group(1)) + 2, 5)
                    print(f"      [PAUSE]  429 [TPM predictions], {wait:.0f}с (попытка {attempt+1}/{GROQ_MAX_RETRIES})")
                    time.sleep(wait)
                    continue

                raise

    raise RuntimeError(f"Groq predictions: exhausted: {last_err}")


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
        if not (10 <= len(p) <= 500):
            continue
        if "о котором говорится в интервью" in p.lower():
            continue
        years_in_pred = re.findall(r'\b(20\d{2})\b', p)
        # Ослаблено: если год есть — проверяем, если нет — НЕ отсеиваем
        if years_in_pred:
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
            print(f"   [WARN]  413 в extract_predictions ({len(safe_text)} симв.) — уменьшаю до {_FALLBACK_INPUT_CHARS}")
            safe_text = transcript[:_FALLBACK_INPUT_CHARS]
            prompt = _build_prompt(safe_text, expert_name, today_str, current_year, max_year)
            try:
                r = _call_groq(prompt)
            except Exception as e2:
                print(f"[WARN]  прогнозы (retry): {e2}")
                return []
        else:
            print(f"[WARN]  прогнозы: {e}")
            return []

    try:
        raw = r.choices[0].message.content.strip()
        return _parse_response(raw, current_year, max_year)
    except Exception as e:
        print(f"[WARN]  прогнозы (parse): {e}")
        return []


def extract_predictions_from_theses(theses: list, expert_name: str) -> list:
    """Извлекает прогнозы из тезисов Map (без LLM).

    theses: [{"text", "is_important", "is_forecast", "start", "quote"}]
    Фильтр is_forecast -> prediction_text (quote приоритетнее text).
    Валидация годов/длины — через _parse_response.
    """
    if not EXTRACT_PREDICTIONS:
        return []
    if not theses:
        print("   [STAT] прогнозов из тезисов: 0 (нет тезисов)")
        return []

    now_utc = datetime.now(timezone.utc)
    current_year = now_utc.year
    max_year = current_year + MAX_PREDICTION_HORIZON_YEARS

    forecasts = [t for t in theses if t.get("is_forecast")]
    if not forecasts:
        print("   [STAT] прогнозов из тезисов: 0 (нет is_forecast)")
        return []

    raw_texts = []
    for t in forecasts:
        quote = (t.get("quote") or "").strip()
        ttext = (t.get("text") or "").strip()
        chosen = quote if len(quote) >= 15 else ttext
        if chosen:
            raw_texts.append(chosen)

    if not raw_texts:
        print("   [STAT] прогнозов из тезисов: 0 (пустые тексты)")
        return []

    try:
        parsed = _parse_response(
            json.dumps(raw_texts, ensure_ascii=False), current_year, max_year
        )
    except Exception as e:
        print(f"   [WARN]  прогнозы из тезисов (parse): {e}")
        return []

    print(f"   [STAT] прогнозов из тезисов: {len(parsed)} (из {len(forecasts)} is_forecast)")
    return parsed

