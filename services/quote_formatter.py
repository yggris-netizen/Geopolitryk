# -*- coding: utf-8 -*-
"""D1 v5: связки ТОЛЬКО «на вопрос» + plain. Без «По теме» (обрывки)."""
import re

CONNECTORS_AFTER_QUESTION = [
    "эксперт отмечает",
    "отвечая на вопрос, спикер заявляет",
    "по вопросу ведущего эксперт подчёркивает",
    "отвечая, он говорит",
]
CONNECTORS_PLAIN = [
    "эксперт отмечает",
    "спикер заявляет",
    "он подчёркивает",
    "по его словам",
    "также он говорит",
    "отдельно отмечает",
    "далее эксперт заявляет",
    "спикер обращает внимание",
    "он добавляет",
    "эксперт указывает",
]

SENTENCES_PER_PARAGRAPH = 3
MIN_QUOTE_LEN = 40


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


def _clean_quote(q: str) -> str:
    if not q:
        return ""
    q = q.strip()
    q = re.sub(r'^[\s\.…]+', '', q)
    for prefix in ("И ", "А ", "Но ", "Ну ", "Вот ", "То есть ", "Так ",
                   "В общем ", "Поэтому ", "Причем ", "Причём "):
        if q.startswith(prefix):
            q = q[len(prefix):]
            break
    q = re.sub(r'[\s\.…]+$', '', q)
    if q:
        q = q[0].upper() + q[1:]
    return q


def _build_question(seg_idx: int, segments: list) -> str:
    if not segments or seg_idx <= 0:
        return ""
    for back in range(1, min(6, seg_idx + 1)):
        prev = segments[seg_idx - back].get("text", "").strip()
        if not prev:
            continue
        if prev.rstrip().endswith("?"):
            q = prev.lstrip("— «\"' ")
            q = q[:150] + ("…" if len(q) > 150 else "")
            return q
    return ""


def format_quotes(verified: list, segments: list = None) -> str:
    if not verified:
        return ""

    sentences = []
    last_ctx_seg = -1
    q_idx = {"question": 0, "plain": 0}

    for v in verified:
        quote = _clean_quote(v.get("quote", ""))
        if len(quote) < MIN_QUOTE_LEN:
            continue
        ts = _format_duration(v.get("start", 0))
        ts_str = f" [{ts}]" if ts else ""

        seg_idx = v.get("seg_idx", -1)
        question = ""
        if segments and seg_idx > 0 and seg_idx != last_ctx_seg:
            question = _build_question(seg_idx, segments)
            if question:
                last_ctx_seg = seg_idx

        if question:
            connector = CONNECTORS_AFTER_QUESTION[q_idx["question"] % len(CONNECTORS_AFTER_QUESTION)]
            q_idx["question"] += 1
            sentence = f"На вопрос ведущего «{question}» {connector}: «{quote}»{ts_str}"
        else:
            connector = CONNECTORS_PLAIN[q_idx["plain"] % len(CONNECTORS_PLAIN)]
            q_idx["plain"] += 1
            sentence = f"{connector.capitalize()}: «{quote}»{ts_str}"

        sentences.append(sentence)

    if not sentences:
        return ""

    paragraphs = []
    for i in range(0, len(sentences), SENTENCES_PER_PARAGRAPH):
        chunk = sentences[i:i + SENTENCES_PER_PARAGRAPH]
        paragraphs.append(" ".join(chunk))

    return "\n\n".join(paragraphs)
