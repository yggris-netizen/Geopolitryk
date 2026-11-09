# -*- coding: utf-8 -*-
"""NLI-проверка тезисов суммаризации.

1. Для каждого claim ищем quote в transcript_segments (дословно).
2. Если цитаты нет → not_found → убрать.
3. Если цитата есть → NLI (rubert-base-cased-nli-threeway):
   entailment → оставить, contradiction/neutral → убрать.
4. Возвращает отфильтрованный JSON-список.

Модель: cointegrated/rubert-base-cased-nli-threeway (~700 МБ, CPU).
ВАЖНО: id2label модели = {0: entailment, 1: contradiction, 2: neutral}
"""

import json
import re

_nli_model = None
_nli_tokenizer = None
NLI_AVAILABLE = False

# Порядок меток именно для cointegrated/rubert-base-cased-nli-threeway
_NLI_LABELS = ["entailment", "contradiction", "neutral"]


def _load_nli():
    """Ленивая загрузка NLI-модели (один раз)."""
    global _nli_model, _nli_tokenizer, NLI_AVAILABLE
    if _nli_model is not None:
        return _nli_model
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        print("📥 Загружаю NLI-модель (rubert-base-cased-nli-threeway)...")
        model_name = "cointegrated/rubert-base-cased-nli-threeway"
        _nli_tokenizer = AutoTokenizer.from_pretrained(model_name)
        _nli_model = AutoModelForSequenceClassification.from_pretrained(model_name)
        _nli_model.eval()
        NLI_AVAILABLE = True
        print("✅ NLI-модель OK")
        return _nli_model
    except Exception as e:
        print(f"❌ NLI не загрузилась: {e}")
        NLI_AVAILABLE = False
        return None


def _nli_check(claim: str, premise: str) -> str:
    """Возвращает 'entailment' / 'contradiction' / 'neutral'."""
    model = _load_nli()
    if model is None:
        return "unknown"
    try:
        import torch
        inputs = _nli_tokenizer(
            premise, claim,
            return_tensors="pt", truncation=True, max_length=512,
        )
        with torch.no_grad():
            logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)[0]
        idx = int(torch.argmax(probs).item())
        return _NLI_LABELS[idx] if idx < len(_NLI_LABELS) else "unknown"
    except Exception as e:
        print(f"      ⚠️  NLI: {e}")
        return "unknown"


def _normalize(s: str) -> str:
    """Нормализация для поиска цитаты: нижний регистр, без пунктуации, сжатые пробелы."""
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _find_quote_in_segments(quote: str, segments: list) -> dict | None:
    """Ищет дословную цитату в сегментах (с допуском на пунктуацию)."""
    if not quote or not segments:
        return None
    q_norm = _normalize(quote)
    if len(q_norm) < 10:
        return None
    # Ищем сегмент, в котором quote — подстрока
    for seg in segments:
        text_norm = _normalize(seg.get("text", ""))
        if q_norm in text_norm:
            return seg
    # Если не нашли дословно — ищем по первым 20 символам
    q_prefix = q_norm[:20]
    for seg in segments:
        text_norm = _normalize(seg.get("text", ""))
        if q_prefix in text_norm:
            return seg
    return None


def verify_summary(claims_json: str, segments: list) -> tuple:
    """Проверяет тезисы: цитата + NLI.

    Возвращает (verified_claims, stats):
    - verified_claims: [{claim, quote, start, nli}]
    - stats: {total, not_found, contradiction, neutral, verified}
    """
    if not claims_json or not segments:
        return [], {"total": 0, "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}

    try:
        claims = json.loads(claims_json)
    except json.JSONDecodeError:
        return [], {"total": 0, "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}

    if not isinstance(claims, list):
        return [], {"total": 0, "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}

    stats = {"total": len(claims), "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}
    verified = []

    for c in claims:
        if not isinstance(c, dict):
            continue
        claim = (c.get("claim") or "").strip()
        quote = (c.get("quote") or "").strip()
        if not claim:
            continue

        # 1. Поиск цитаты
        seg = _find_quote_in_segments(quote, segments) if quote else None
        if not seg:
            stats["not_found"] += 1
            print(f"   ❌ not_found: {claim[:60]}")
            continue

        # 2. NLI: следует ли claim из quote
        nli = _nli_check(claim, seg.get("text", ""))
        if nli == "entailment":
            verified.append({
                "claim": claim,
                "quote": quote,
                "start": float(seg.get("start", 0)),
                "nli": nli,
            })
            stats["verified"] += 1
            print(f"   ✅ entailment: {claim[:60]}")
        elif nli == "contradiction":
            stats["contradiction"] += 1
            print(f"   ❌ contradiction: {claim[:60]}")
        else:
            stats["neutral"] += 1
            print(f"   ⚠️  neutral: {claim[:60]}")

    return verified, stats


def main():
    """CLI для теста: python scripts/verify_summary.py <video_id>"""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from db.transcripts_raw import get_transcript_raw_segments
    from services.summarization import summarize_text

    if len(sys.argv) < 2:
        print("Использование: python scripts/verify_summary.py <video_id>")
        sys.exit(1)

    vid = sys.argv[1]
    segments = get_transcript_raw_segments(vid)
    if not segments:
        print(f"❌ Сегменты для {vid} не найдены")
        sys.exit(1)

    transcript = " ".join(s.get("text", "") for s in segments)
    print(f"📝 Транскрипт: {len(transcript)} символов, сегментов: {len(segments)}")

    print("🧠 Суммаризация с цитатами...")
    claims_json = summarize_text(transcript, segments)
    print(f"📊 Тезисов: {len(json.loads(claims_json))}")

    print("🔍 NLI-проверка...")
    verified, stats = verify_summary(claims_json, segments)
    print(f"\n📊 Статистика: {stats}")
    print(f"\n✅ Подтверждено ({len(verified)}):")
    for v in verified:
        print(f"  [{v['start']:.0f}с] {v['claim']}")
        print(f"    «{v['quote'][:80]}»")


if __name__ == "__main__":
    main()