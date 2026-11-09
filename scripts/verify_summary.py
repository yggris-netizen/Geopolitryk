# -*- coding: utf-8 -*-
"""NLI-проверка тезисов суммаризации (Вариант A.4).

Поиск цитаты:
1. exact normalized (один сегмент)
2. multi-segment (2-3 соседних)
3. fuzzy (rapidfuzz.partial_ratio > 90, только если сегмент >= 30 символов)

Premise для NLI = 3-5 сегментов вокруг найденного (контекст).
NLI (rubert-base-cased-nli-threeway):
- contradiction > 0.5 -> contradiction (убрать)
- entailment > 0.3 -> entailment (verified)
- иначе neutral (убрать)

id2label: {0: entailment, 1: contradiction, 2: neutral}
"""

import json
import re

try:
    from rapidfuzz import fuzz as _fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

_nli_model = None
_nli_tokenizer = None
NLI_AVAILABLE = False

_NLI_LABELS = ["entailment", "contradiction", "neutral"]

FUZZY_THRESHOLD = 90           # было 80 — ужесточено
FUZZY_MIN_SEG_LEN = 30         # fuzzy только для сегментов >= 30 символов
MAX_MERGE_SEGMENTS = 3
PREMISE_CONTEXT_SEGMENTS = 2   # +2 до/после (итого 5)
NLI_ENTAILMENT_THRESHOLD = 0.3
NLI_CONTRADICTION_THRESHOLD = 0.5


def _load_nli():
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


def _nli_probs(claim: str, premise: str) -> dict:
    model = _load_nli()
    if model is None:
        return {"entailment": 0.0, "contradiction": 0.0, "neutral": 1.0}
    try:
        import torch
        inputs = _nli_tokenizer(
            premise, claim,
            return_tensors="pt", truncation=True, max_length=512,
        )
        with torch.no_grad():
            logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)[0].tolist()
        return {_NLI_LABELS[i]: float(probs[i]) for i in range(len(_NLI_LABELS))}
    except Exception as e:
        print(f"      ⚠️  NLI: {e}")
        return {"entailment": 0.0, "contradiction": 0.0, "neutral": 1.0}


def _nli_verdict(probs: dict) -> str:
    if probs.get("contradiction", 0) > NLI_CONTRADICTION_THRESHOLD:
        return "contradiction"
    if probs.get("entailment", 0) > NLI_ENTAILMENT_THRESHOLD:
        return "entailment"
    return "neutral"


def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _build_premise(seg_idx: int, segments: list) -> str:
    lo = max(0, seg_idx - PREMISE_CONTEXT_SEGMENTS)
    hi = min(len(segments), seg_idx + PREMISE_CONTEXT_SEGMENTS + 1)
    parts = []
    for k in range(lo, hi):
        t = (segments[k].get("text") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def _find_quote_in_segments(quote: str, segments: list):
    """Возвращает (seg, seg_idx) или (None, -1)."""
    if not quote or not segments:
        return None, -1
    q_norm = _normalize(quote)
    if len(q_norm) < 10:
        return None, -1

    # 1. Exact (один сегмент)
    for i, seg in enumerate(segments):
        if q_norm in _normalize(seg.get("text", "")):
            return seg, i

    q_prefix = q_norm[:60]

    # 2. Multi-segment (2-3 соседних) — сначала, до fuzzy
    for i in range(len(segments)):
        combined = _normalize(segments[i].get("text", ""))
        for j in range(1, MAX_MERGE_SEGMENTS):
            if i + j >= len(segments):
                break
            combined = combined + " " + _normalize(segments[i + j].get("text", ""))
            if q_prefix in combined:
                return segments[i], i

    # 3. Fuzzy — только для длинных сегментов и строгим порогом
    if FUZZY_AVAILABLE:
        for i, seg in enumerate(segments):
            text_norm = _normalize(seg.get("text", ""))
            if len(text_norm) < FUZZY_MIN_SEG_LEN:
                continue
            if _fuzz.partial_ratio(q_prefix, text_norm) > FUZZY_THRESHOLD:
                print(f"      🔍 fuzzy: seg_idx={i}, len={len(text_norm)}, "
                      f"score={_fuzz.partial_ratio(q_prefix, text_norm):.0f}")
                return seg, i

    return None, -1


def verify_summary(claims_json: str, segments: list):
    empty = {"total": 0, "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}
    if not claims_json or not segments:
        return [], empty

    try:
        claims = json.loads(claims_json)
    except json.JSONDecodeError:
        return [], empty

    if not isinstance(claims, list):
        return [], empty

    stats = {"total": len(claims), "not_found": 0, "contradiction": 0, "neutral": 0, "verified": 0}
    verified = []
    debug_neutral_shown = 0

    for c in claims:
        if not isinstance(c, dict):
            continue
        claim = (c.get("claim") or "").strip()
        quote = (c.get("quote") or "").strip()
        if not claim:
            continue

        seg, seg_idx = _find_quote_in_segments(quote, segments) if quote else (None, -1)
        if not seg:
            stats["not_found"] += 1
            print(f"   ❌ not_found: {claim[:60]}")
            continue

        premise = _build_premise(seg_idx, segments)
        probs = _nli_probs(claim, premise)
        verdict = _nli_verdict(probs)

        if verdict == "entailment":
            verified.append({
                "claim": claim,
                "quote": quote,
                "start": float(seg.get("start", 0)),
                "nli": verdict,
                "p_entailment": round(probs.get("entailment", 0), 3),
            })
            stats["verified"] += 1
            print(f"   ✅ entailment ({probs.get('entailment', 0):.2f}): {claim[:60]}")
        elif verdict == "contradiction":
            stats["contradiction"] += 1
            print(f"   ❌ contradiction ({probs.get('contradiction', 0):.2f}): {claim[:60]}")
        else:
            stats["neutral"] += 1
            print(f"   ⚠️  neutral (ent={probs.get('entailment', 0):.2f}): {claim[:60]}")
            if debug_neutral_shown < 3:
                print(f"      claim:   {claim[:150]}")
                print(f"      premise: {premise[:200]}")
                debug_neutral_shown += 1

    return verified, stats


def main():
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
    try:
        print(f"📊 Тезисов: {len(json.loads(claims_json))}")
    except Exception:
        print("📊 Тезисов: 0")

    print("🔍 NLI-проверка (premise = 3-5 сегментов)...")
    verified, stats = verify_summary(claims_json, segments)
    print(f"\n📊 Статистика: {stats}")
    print(f"\n✅ Подтверждено ({len(verified)}):")
    for v in verified:
        print(f"  [{v['start']:.0f}с] {v['claim']}")
        print(f"    «{v['quote'][:80]}»")


if __name__ == "__main__":
    main()
