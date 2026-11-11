# -*- coding: utf-8 -*-
"""Суммаризация (Map-Reduce) + JSON с цитатами. reasoning_effort='low'."""
import json
import re
import time

from config import (
    GROQ_EXTRA_MODELS,
    GROQ_TASK_MODELS,
    GROQ_DELAY_BETWEEN_CHUNKS, GROQ_FALLBACK_MODEL,
    GROQ_MAX_RETRIES, GROQ_MODEL,
    SUMMARIZE_CHUNK_CHARS, SUMMARIZE_FINAL_TOKENS,
    SUMMARIZE_PARTIAL_TOKENS,
)
from services.llm import groq_client, groq_chat_create


def _call_groq_with_retry(messages, max_tokens, temperature=0.2, reasoning_effort="low", task="medium"):
    """Groq с каскадом (20b -> 120b), retry при TPM, reasoning_effort='low'."""
    models = GROQ_TASK_MODELS.get(task, [GROQ_MODEL] + GROQ_EXTRA_MODELS)
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)

    last_error = None
    for mi, model in enumerate(models):
        is_last = (mi == len(models) - 1)
        for attempt in range(GROQ_MAX_RETRIES):
            try:
                kwargs = dict(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                # reasoning_effort только для gpt-oss (try/except)
                if "gpt-oss" in model and reasoning_effort:
                    try:
                        r = groq_chat_create(
                            reasoning_effort=reasoning_effort, **kwargs
                        )
                    except TypeError:
                        # старый SDK — без reasoning_effort
                        r = groq_chat_create(**kwargs)
                else:
                    r = groq_chat_create(**kwargs)

                msg = r.choices[0].message
                content = (msg.content or "").strip()
                finish = r.choices[0].finish_reason
                reasoning = getattr(msg, "reasoning_content", None) or ""

                if not content:
                    print(f"      [WARN]  {model}: content пуст "
                          f"(finish={finish}, reasoning={len(reasoning)} симв)")
                    if is_last:
                        return ""
                    break  # -> fallback

                return content
            except Exception as e:
                err = str(e).lower()
                last_error = e
                if ("tokens per day" in err or "tpd" in err) and not is_last:
                    print(f"      [RETRY] TPD -> fallback")
                    break
                if ("429" in err or "rate_limit" in err) and attempt < GROQ_MAX_RETRIES - 1:
                    wait = 30
                    m = re.search(r'try again in ([\d.]+)s', str(e))
                    if m:
                        wait = max(float(m.group(1)) + 2, 5)
                    print(f"      [PAUSE]  429, {wait:.0f}с")
                    time.sleep(wait)
                    continue
                raise
    raise RuntimeError(f"Groq exhausted: {last_error}")


def _extract_json(raw: str):
    """JSON-массив из ответа LLM (чистый / в ```json / с текстом)."""
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
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # Fallback: закрыть обрезанный массив
    m = re.search(r'\[.*', raw, re.DOTALL)
    if m:
        partial = m.group(0).rstrip().rstrip(",")
        for suffix in ("}]", "}]", "}]]", "]"):
            try:
                return json.loads(partial + suffix)
            except json.JSONDecodeError:
                continue
    return None


def _summarize_chunk_with_citations(chunk: str, chunk_offset_sec: float = 0) -> list:
    prompt = f"""Извлеки КЛЮЧЕВЫЕ ТЕЗИСЫ из фрагмента интервью.

ТЕКСТ:
---
{chunk}
---

ПРАВИЛА:
1. Каждый тезис — утверждение эксперта (мнение, прогноз, оценка).
2. К тезису — ДОСЛОВНАЯ ЦИТАТА (не перефразируй).
3. start — секунды от начала фрагмента.
4. Если тезисов нет — верни [].

Верни ТОЛЬКО JSON-массив, без markdown, без пояснений:
[{{"claim": "...", "quote": "...", "start": 0.0}}]

JSON:"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=SUMMARIZE_PARTIAL_TOKENS,
            temperature=0.2,
            reasoning_effort="low",
        )
        if not raw:
            print(f"      [WARN]  chunk: пустой ответ")
            return []
        data = _extract_json(raw)
        if data is None:
            print(f"      [WARN]  chunk: не JSON, raw={raw[:150]!r}")
            return []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "start" in item:
                    item["start"] = float(item.get("start", 0)) + chunk_offset_sec
            return [item for item in data if isinstance(item, dict) and item.get("claim")]
    except Exception as e:
        print(f"      [WARN]  chunk: {type(e).__name__}: {e!r}")
    return []


def summarize_text(transcript_text: str, transcript_segments: list = None) -> str:
    if not transcript_text:
        return "[]"

    chunks = []
    for i in range(0, len(transcript_text), SUMMARIZE_CHUNK_CHARS):
        chunk = transcript_text[i:i + SUMMARIZE_CHUNK_CHARS]
        offset = 0.0
        if transcript_segments:
            for seg in transcript_segments:
                if len(" ".join(s.get("text", "") for s in transcript_segments[:transcript_segments.index(seg) + 1])) >= i:
                    offset = float(seg.get("start", 0))
                    break
        chunks.append((chunk, offset))

    all_claims = []
    for idx, (chunk, off) in enumerate(chunks, 1):
        print(f"      {idx}/{len(chunks)}...")
        claims = _summarize_chunk_with_citations(chunk, off)
        all_claims.extend(claims)
        if idx < len(chunks):
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)

    if len(all_claims) > 50:
        all_claims = all_claims[:50]

    return json.dumps(all_claims, ensure_ascii=False)


def neutralize_title(title: str) -> str:
    if not title:
        return title
    prompt = f"""Перепиши заголовок нейтрально, без эмоций и капса.

ЗАГОЛОВОК: {title}

Верни ТОЛЬКО переписанный заголовок (до 90 символов):"""
    try:
        return _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000, temperature=0.3, reasoning_effort="low",
            task="low",
        )

    except Exception:
        return title


def generate_hashtags(expert_name: str, summary: str) -> str:
    prompt = f"""Сгенерируй 3-5 хештегов.

ЭКСПЕРТ: {expert_name}
ТЕКСТ: {summary[:500]}

Верни ТОЛЬКО хештеги через пробел (начиная с #):"""
    try:
        raw = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80, temperature=0.3, reasoning_effort="low",
            task="low",
        )

        return raw.strip()
    except Exception:
        return ""


def _check_context_errors(summary: str, transcript: str) -> str:
    return summary


def _format_title(title: str) -> str:
    return f"<b>{title}</b>" if title else ""