# -*- coding: utf-8 -*-
"""TPD Circuit Breaker: учёт токенов Groq по моделям (UTC-день).

Сброс лимитов Groq — 00:00 UTC (03:00 МСК). Считаем по date(created_at) в UTC.
Предохранитель: блокируем вызов при >= 90% TPD (TPD_SAFETY_RATIO).
"""
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

# Лимиты TPD (бесплатный tier Groq). Ключ — префикс имени модели.
MODEL_TPD_LIMITS = {
    "llama-3.1-8b-instant": 200_000,
    "llama-3.3-70b-versatile": 200_000,
    "qwen/qwen3-32b": 200_000,
    "openai/gpt-oss-20b": 200_000,
    "openai/gpt-oss-120b": 200_000,
}
TPD_SAFETY_RATIO = 0.90


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _limit_for(model: str) -> int:
    for prefix, lim in MODEL_TPD_LIMITS.items():
        if model.startswith(prefix):
            return lim
    return 200_000


def log_usage(model: str, task: str, prompt_tokens: int,
              completion_tokens: int, total_tokens: int) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO token_usage (model, task, prompt_tokens, "
            "completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?)",
            (model, task, prompt_tokens or 0, completion_tokens or 0, total_tokens or 0),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[TPD] log_usage failed: {e!r}")


def mark_exhausted(model: str, task: str = "unknown") -> None:
    """Помечает модель исчерпанной до сброса TPD (00:00 UTC).

    Пишет total_tokens = limit_for(model). get_today_usage -> limit,
    can_call -> False (при TPD_SAFETY_RATIO < 1.0).
    """
    try:
        limit = _limit_for(model)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO token_usage (model, task, prompt_tokens, "
            "completion_tokens, total_tokens) VALUES (?, ?, 0, 0, ?)",
            (model, f"tpd_exhausted:{task}", limit),
        )
        conn.commit()
        conn.close()
        print(f"[TPD] {model}: помечена исчерпанной ({limit} ток.)")
    except Exception as e:
        print(f"[TPD] mark_exhausted({model}) failed: {e!r}")


def get_today_usage(model: str) -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM token_usage "
            "WHERE model = ? AND date(created_at) = ?",
            (model, _today_utc()),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[TPD] get_today_usage({model}) failed: {e!r}")
        return 0


def can_call(model: str, estimated_tokens: int = 0) -> bool:
    """True, если вызов не превысит 90% TPD."""
    limit = _limit_for(model)
    used = get_today_usage(model)
    return (used + estimated_tokens) < int(limit * TPD_SAFETY_RATIO)
