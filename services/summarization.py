"""Суммаризация, нейтрализация заголовков, хештеги. Фикс 413 + каскад моделей на TPD."""

import html
import re
import time

from config import (
    GROQ_DELAY_BETWEEN_CHUNKS, GROQ_FALLBACK_MODEL,
    GROQ_MAX_RETRIES, GROQ_MODEL, MAX_TITLE_LEN,
    SUMMARIZE_CHUNK_CHARS, SUMMARIZE_FINAL_TOKENS, SUMMARIZE_PARTIAL_TOKENS,
)
from services.llm import groq_client


def generate_hashtags(expert_name, summary):
    prompt = f"""3 хештега (#Слово через пробел).

Эксперт: {expert_name}
Текст: {summary[:600]}

Хештеги:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.4, max_tokens=300,
        )
        tags = r.choices[0].message.content.strip()
        valid = [t for t in tags.split() if t.startswith("#") and len(t) > 2]
        return " ".join(valid[:3])
    except Exception:
        return ""


def _is_title_neutral(title):
    if not title:
        return True
    if title.isupper():
        return False
    if any(c in title for c in "!🎯🍀🔥💥⚡📌❗❕"):
        return False
    if len(title) > MAX_TITLE_LEN:
        return False
    words = title.split()
    if words:
        caps_count = sum(1 for w in words if len(w) > 2 and w.isupper())
        if caps_count / len(words) > 0.3:
            return False
    return True


def _simple_neutralize(title):
    if not title:
        return ""
    c = re.sub(r'[🍀🔥💥⚡️🎯📌❗❕!]', '', title)
    c = re.sub(r'\s+', ' ', c).strip()
    words = c.split()
    new_words = [w.capitalize() if len(w) > 2 and w.isupper() else w for w in words]
    c = " ".join(new_words)
    if len(c) > MAX_TITLE_LEN:
        c = c[:MAX_TITLE_LEN].rsplit(' ', 1)[0] + "…"
    return c


def neutralize_title(video_title):
    if not video_title:
        return ""
    if _is_title_neutral(video_title):
        return video_title
    prompt = f"""Перепиши заголовок нейтрально (до 80 символов). Без CAPS, «!», эмодзи, кликбейта.

Оригинал: {video_title}

Нейтральный:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.4, max_tokens=500,
        )
        new = r.choices[0].message.content.strip().strip('"').strip("'").strip()
        if not new or len(new) < 5 or new.lower() == video_title.lower() or new.isupper():
            return _simple_neutralize(video_title)
        if not _is_title_neutral(new):
            return _simple_neutralize(video_title)
        return new
    except Exception:
        return _simple_neutralize(video_title)


def _format_title(title, max_len=MAX_TITLE_LEN):
    if not title:
        return ""
    title = html.unescape(title)
    if len(title) > max_len:
        title = title[:max_len - 1] + "…"
    return f"<b>{title.upper()}</b>"


def _call_groq_with_retry(messages, temperature, max_tokens, label=""):
    """
    TPM (tokens per minute) → retry с паузой на текущей модели.
    TPD (tokens per day) → переключение на GROQ_FALLBACK_MODEL.
    Если все модели исчерпали TPD — RuntimeError (worker пометит как error).
    """
    models = [GROQ_MODEL]
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)

    last_err = None
    for mi, model in enumerate(models):
        is_last = (mi == len(models) - 1)
        for attempt in range(GROQ_MAX_RETRIES):
            try:
                return groq_client.chat.completions.create(
                    messages=messages, model=model,
                    temperature=temperature, max_tokens=max_tokens,
                )
            except Exception as e:
                last_err = e
                err = str(e).lower()

                # TPD — переключаемся на следующую модель
                if "tokens per day" in err or "tpd" in err:
                    short = model.split("/")[-1]
                    if not is_last:
                        next_short = models[mi + 1].split("/")[-1]
                        print(f"      🔄 TPD [{label}] {short} → {next_short}")
                        break
                    raise RuntimeError(f"TPD exhausted on all models (last: {short}): {e}")

                # TPM — retry с паузой
                if ("429" in err or "rate_limit" in err) and attempt < GROQ_MAX_RETRIES - 1:
                    wait = 30 * (2 ** attempt)
                    m = re.search(r'try again in ([\d.]+)s', str(e))
                    if m:
                        wait = max(float(m.group(1)) + 2, 5)
                    print(f"      ⏸  429 [TPM {label}], {wait:.0f}с (попытка {attempt+1}/{GROQ_MAX_RETRIES})")
                    time.sleep(wait)
                    continue

                raise

    raise RuntimeError(f"Groq: exhausted all models: {last_err}")


_SAFE_INPUT_CHARS = 14000
_FALLBACK_INPUT_CHARS = 7000


def _summarize_chunk(text, is_final):
    if is_final:
        prompt_template = """Аналитическая выжимка интервью на русском.

ПРАВИЛА:
1. Игнорируй рубрики/заставки.
2. Исправляй ошибки распознавания.
3. Атрибуция: «по мнению эксперта» (не более 2 раз).
4. Не приписывай страны/имена, которых нет.
5. Без эмодзи, без * и - — только •.

Структура:

<b>ГЛАВНЫЙ ТЕЗИС</b>
(1-2 предложения)

<b>АРГУМЕНТЫ И ФАКТЫ</b>
• (3-5)

<b>ПРОГНОЗЫ И ОЦЕНКИ</b>
• (если есть)

Текст:
---
{text}
---

Выжимка:"""
        max_tokens = SUMMARIZE_FINAL_TOKENS
    else:
        prompt_template = """Тезисы, аргументы, прогнозы. Кратко.
Атрибуция: «эксперт утверждает». Списки — •.

---
{text}
---

Выжимка:"""
        max_tokens = SUMMARIZE_PARTIAL_TOKENS

    safe_text = text if len(text) <= _SAFE_INPUT_CHARS else text[:_SAFE_INPUT_CHARS]
    prompt = prompt_template.replace("{text}", safe_text)

    try:
        c = _call_groq_with_retry(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=max_tokens, label="summarize",
        )
        return c.choices[0].message.content.strip()
    except Exception as e:
        err = str(e).lower()
        if "413" in err or "request_too_large" in err:
            print(f"      ⚠️  413 на входе {len(safe_text)} символов — уменьшаю до {_FALLBACK_INPUT_CHARS}")
            safe_text = text[:_FALLBACK_INPUT_CHARS]
            prompt = prompt_template.replace("{text}", safe_text)
            c = _call_groq_with_retry(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=max_tokens, label="summarize-retry",
            )
            return c.choices[0].message.content.strip()
        raise


def summarize_text(text):
    if not text.strip():
        raise ValueError("пустой текст")
    CHUNK = SUMMARIZE_CHUNK_CHARS
    if len(text) <= CHUNK:
        return _summarize_chunk(text, is_final=True)
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    print(f"📄 Map-Reduce: {len(chunks)} частей (chunk={CHUNK}, final={SUMMARIZE_FINAL_TOKENS} tok)")
    partials = []
    for i, c in enumerate(chunks, 1):
        print(f"   {i}/{len(chunks)}...")
        partials.append(_summarize_chunk(c, is_final=False))
        if i < len(chunks):
            print(f"      ⏸  {GROQ_DELAY_BETWEEN_CHUNKS}с...")
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)
    return _summarize_chunk("\n\n---\n\n".join(partials), is_final=True)


def _check_context_errors(summary, original):
    ukr = ["рада", "зеленск", "киев", "украин", "київ"]
    rus = ["дума", "путин", "кремл", "россий", "рф "]
    tl, sl = original.lower(), summary.lower()
    is_ukr = any(m in tl for m in ukr)
    is_rus = any(m in tl for m in rus)
    if is_ukr and not is_rus and "росси" in sl:
        print("⚠️  подмена: Украина→Россия")
    if is_rus and not is_ukr and "украин" in sl:
        print("⚠️  подмена: Россия→Украина")
    return summary