# -*- coding: utf-8 -*-
"""Извлечение ДОСЛОВНЫХ цитат из транскрипта (без claim, без NLI)."""
import json
import time

from config import (
    GROQ_DELAY_BETWEEN_CHUNKS, SUMMARIZE_CHUNK_CHARS, SUMMARIZE_PARTIAL_TOKENS,
)
from services.summarization import _call_groq_with_retry, _extract_json


def _extract_quotes_chunk(chunk: str, chunk_offset_sec: float = 0) -> list:
    prompt = f"""Извлеки 3-5 САМЫХ ЗНАЧИМЫХ ДОСЛОВНЫХ ЦИТАТ из фрагмента.

ТЕКСТ:
---
{chunk}
---

ПРАВИЛА:
1. Цитата — ДОСЛОВНО из текста (не перефразируй, не сокращай).
2. Каждая цитата — 1-3 предложения (законченная мысль).
3. Укажи start (секунды от начала фрагмента).
4. НЕ выделяй:
   - приветствия/прощания («добрый вечер», «всем спасибо», «до свидания»)
   - вопросы ведущего (заканчиваются на «?»)
   - «воду» (тавтологии: «вот-вот», «на самом деле», «по большому счёту», «это вот уже», «должно начать происходить»)
   - обрывки (начинаются с «который», «это», «и», «а», «но»)
   - междометия («да, да», «нет, нет»)
5. Если нет значимых цитат — верни [].

Верни ТОЛЬКО JSON-массив, без markdown, без пояснений:
[{{"quote": "...", "start": 0.0}}]

JSON:"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=SUMMARIZE_PARTIAL_TOKENS,
            temperature=0.2,
            reasoning_effort="low",
        )
        if not raw:
            print(f"      ⚠️  quotes: пустой ответ")
            return []
        data = _extract_json(raw)
        if data is None:
            print(f"      ⚠️  quotes: не JSON, raw={raw[:150]!r}")
            return []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "start" in item:
                    item["start"] = float(item.get("start", 0)) + chunk_offset_sec
            return [item for item in data if isinstance(item, dict) and item.get("quote")]
    except Exception as e:
        print(f"      ⚠️  quotes: {type(e).__name__}: {e!r}")
    return []


def extract_quotes(transcript_text: str, transcript_segments: list = None) -> str:
    """Возвращает JSON-строку [{quote, start}]."""
    if not transcript_text:
        return "[]"

    chunks = []
    for i in range(0, len(transcript_text), SUMMARIZE_CHUNK_CHARS):
        chunk = transcript_text[i:i + SUMMARIZE_CHUNK_CHARS]
        offset = 0.0
        if transcript_segments:
            acc = 0
            for seg in transcript_segments:
                acc += len(seg.get("text", "")) + 1
                if acc >= i:
                    offset = float(seg.get("start", 0))
                    break
        chunks.append((chunk, offset))

    all_quotes = []
    for idx, (chunk, off) in enumerate(chunks, 1):
        print(f"      {idx}/{len(chunks)}...")
        quotes = _extract_quotes_chunk(chunk, off)
        all_quotes.extend(quotes)
        if idx < len(chunks):
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)

    return json.dumps(all_quotes, ensure_ascii=False)
