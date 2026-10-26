"""Кеш транскриптов: чтобы не транскрибировать одно видео повторно."""

import sqlite3
from datetime import datetime, timedelta, timezone

from config import DB_PATH, TRANSCRIPTS_CACHE_DAYS, MIN_TRANSCRIPT_CHARS


def get_cached_transcript(video_id: str) -> str:
    """Возвращает транскрипт из кеша или пустую строку."""
    if not video_id:
        return ""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT transcript FROM transcripts_cache WHERE video_id = ?",
            (video_id,)
        ).fetchone()
        if not row:
            return ""
        text = row[0] or ""
        if len(text) < MIN_TRANSCRIPT_CHARS:
            return ""
        return text
    finally:
        conn.close()


def save_cached_transcript(video_id: str, transcript: str) -> bool:
    """Сохраняет транскрипт в кеш (INSERT OR REPLACE)."""
    if not video_id or not transcript:
        return False
    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO transcripts_cache "
            "(video_id, transcript, cached_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (video_id, transcript)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"⚠️  save_cached_transcript: {e}")
        return False
    finally:
        conn.close()


def cleanup_old_transcripts(days_keep: int = None):
    """Удаляет транскрипты старше N дней."""
    if days_keep is None:
        days_keep = TRANSCRIPTS_CACHE_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_keep)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute(
            "DELETE FROM transcripts_cache WHERE cached_at < ?",
            (cutoff,)
        ).rowcount
        conn.commit()
        if n:
            print(f"🧹 Удалено старых транскриптов: {n}")
    finally:
        conn.close()