# -*- coding: utf-8 -*-
"""MVP фактчекинга: Tavily search + Groq LLM для одного прогноза.

Использование:
    python scripts/test_factcheck.py           # 1 случайный прогноз
    python scripts/test_factcheck.py 3         # 3 прогноза
    python scripts/test_factcheck.py --id 42   # конкретный прогноз

Требует TAVILY_API_KEY в .env (https://tavily.com, free tier 1000 кредитов/мес).
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GROQ_MODEL, GROQ_FALLBACK_MODEL, TAVILY_API_KEY
from db.predictions import _recent_predictions, get_prediction
from services.llm import groq_client


def _clean_query(text: str) -> str:
    """Очищает prediction_text для Tavily: артефакты LLM, ёлочки, лишние пробелы."""
    t = text or ""
    t = re.sub(r'\(?\s*как\s+указано\s+в\s+интервью\s*\)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\(?\s*о\s+которой\s+говорилось\s+в\s+интервью\s*\)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\(?\s*о\s+котором\s+говорилось\s+в\s+интервью\s*\)?', '', t, flags=re.IGNORECASE)
    for ch in ('«', '»', '"', '“', '”', "'"):
        t = t.replace(ch, '')
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.strip('.,;: ')
    return t


def _tavily_search(query: str, max_results: int = 5) -> list:
    """Поиск через Tavily (basic — бесплатный тир, retry 1 раз)."""
    if not TAVILY_API_KEY:
        print("[ERR] TAVILY_API_KEY не задан в .env")
        print("   Зарегистрируйтесь на https://tavily.com (free 1000/мес),")
        print("   скопируйте ключ и добавьте в .env: TAVILY_API_KEY=tvly-...")
        return []
    try:
        from tavily import TavilyClient
    except ImportError:
        print("[ERR] tavily-python не установлен. Выполните: pip install tavily-python")
        return []

    for attempt in (1, 2):
        try:
            client = TavilyClient(api_key=TAVILY_API_KEY)
            res = client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
            )
            return res.get("results", []) or []
        except Exception as e:
            if attempt == 2:
                print(f"[WARN]  Tavily {type(e).__name__}: {e!r}")
                return []
            print(f"   [PAUSE]  Tavily retry ({type(e).__name__}: {e!r})...")
            time.sleep(2)
    return []


def _build_prompt(prediction_text: str, expert_name: str,
                  prediction_date: str, video_url: str, results: list) -> str:
    context = "\n\n".join(
        f"[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{(r.get('content') or '')[:500]}"
        for i, r in enumerate(results[:5], 1)
    ) or "(нет результатов поиска)"
    return f"""Проверь, подтвердился ли прогноз эксперта.

ПРОГНОЗ: «{prediction_text}»
ЭКСПЕРТ: {expert_name}
ДАТА ПРОГНОЗА: {prediction_date}
ВИДЕО-ИСТОЧНИК: {video_url}

РЕЗУЛЬТАТЫ ПОИСКА:
---
{context}
---

ПРАВИЛА:
1. confirmed — есть явное подтверждение (событие произошло, факт подтверждён).
2. refuted — есть явное опровержение (событие не произошло).
3. unverifiable — недостаточно данных или нерелевантно.
4. source_url — URL наиболее релевантного источника (1 шт).
5. reasoning — краткое обоснование (до 150 символов).

Верни ТОЛЬКО JSON, без markdown, без пояснений, без ```:
{{"verdict": "confirmed|refuted|unverifiable", "source_url": "...", "reasoning": "..."}}
"""


def _extract_json(raw: str) -> dict | None:
    """Извлекает JSON из ответа LLM: чистый / в ```json / с текстом вокруг."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _call_llm(prompt: str) -> dict:
    """Без JSON mode (response_format) — gpt-oss-20b падает с BadRequestError."""
    models = [GROQ_MODEL]
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)
    for model in models:
        try:
            kwargs = dict(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0,
                max_tokens=600,
                tools=[],
                tool_choice="none",
            )
            # reasoning_effort только для gpt-oss (try/except)
            if "gpt-oss" in model:
                try:
                    r = groq_client.chat.completions.create(
                        reasoning_effort="low", **kwargs
                    )
                except TypeError:
                    r = groq_client.chat.completions.create(**kwargs)
            else:
                r = groq_client.chat.completions.create(**kwargs)
            raw = (r.choices[0].message.content or "").strip()
            data = _extract_json(raw)
            if data is None:
                print(f"[WARN]  LLM ({model}): не JSON, raw={raw[:200]!r}")
                continue
            return {
                "verdict": data.get("verdict", "unverifiable"),
                "source_url": data.get("source_url", ""),
                "reasoning": (data.get("reasoning") or "")[:200],
            }
        except Exception as e:
            print(f"[WARN]  LLM ({model}): {type(e).__name__}: {e!r}")
            continue
    return {"verdict": "unverifiable", "source_url": "", "reasoning": "LLM не ответил"}


def factcheck_one(pred: dict) -> dict:
    pid = pred.get("id")
    text = (pred.get("prediction_text") or "").strip()
    expert = pred.get("expert_name") or "?"
    date = pred.get("prediction_date") or ""
    vid = pred.get("video_id") or ""
    video_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""

    if not text:
        return {"id": pid, "verdict": "unverifiable",
                "source_url": "", "reasoning": "пустой текст прогноза"}

    clean_text = _clean_query(text)
    if expert and expert not in ("?", "Неизвестный спикер", "Неизвестный эксперт"):
        query = _clean_query(f"{expert} {clean_text}")[:400]
    else:
        query = clean_text[:400]
    print(f"\n[SEARCH] #{pid} — {clean_text[:100]}")
    print(f"   [SEARCH] Tavily: {query[:120]}")
    results = _tavily_search(query)
    print(f"   [FILE] результатов: {len(results)}")
    if not results:
        return {"id": pid, "verdict": "unverifiable",
                "source_url": "", "reasoning": "Tavily: 0 результатов"}

    prompt = _build_prompt(clean_text, expert, date, video_url, results)
    data = _call_llm(prompt)
    return {"id": pid, **data}


def main():
    args = sys.argv[1:]
    n = 1
    pred_id = None
    if args:
        if args[0] == "--id" and len(args) >= 2:
            try:
                pred_id = int(args[1])
            except ValueError:
                print(f"[ERR] --id должен быть числом: {args[1]}")
                sys.exit(1)
        else:
            try:
                n = int(args[0])
            except ValueError:
                print("[ERR] Аргумент должен быть числом или --id <N>")
                sys.exit(1)

    if pred_id is not None:
        p = get_prediction(pred_id)
        if not p:
            print(f"[ERR] Прогноз #{pred_id} не найден")
            sys.exit(1)
        preds = [p]
    else:
        preds = _recent_predictions(limit=n)

    if not preds:
        print("[EMPTY] Нет прогнозов в БД")
        return

    results = []
    for p in preds:
        r = factcheck_one(p)
        results.append(r)
        print(f"   [OK] {r['verdict']}: {r['reasoning']}")
        if r.get("source_url"):
            print(f"   [LINK] {r['source_url']}")

    print(f"\n{'=' * 60}")
    print(f"Проверено: {len(results)}")
    for r in results:
        print(f"  #{r['id']}: {r['verdict']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
