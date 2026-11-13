"""Кеш транскриптов (Postgres)."""
from datetime import datetime, timedelta, timezone

from config import TRANSCRIPTS_CACHE_DAYS, MIN_TRANSCRIPT_CHARS
from db.connection import get_conn


def get_cached_transcript(video_id: str) -> str:
    if not video_id:
        return ""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT transcript FROM transcripts_cache WHERE video_id=%s", (video_id,))
        row = cur.fetchone()
        if not row:
            return ""
        text = row[0] or ""
        if len(text) < MIN_TRANSCRIPT_CHARS:
            return ""
        return text
    finally:
        cur.close()
        conn.close()


def save_cached_transcript(video_id: str, transcript: str) -> bool:
    if not video_id or not transcript:
        return False
    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transcripts_cache (video_id, transcript, cached_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (video_id) DO UPDATE SET "
            "transcript=EXCLUDED.transcript, cached_at=NOW()",
            (video_id, transcript)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[WARN]  save_cached_transcript: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def cleanup_old_transcripts(days_keep: int = None):
    if days_keep is None:
        days_keep = TRANSCRIPTS_CACHE_DAYS
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM transcripts_cache WHERE cached_at < NOW() - make_interval(days => %s)",
            (days_keep,)
        )
        if cur.rowcount:
            print(f"🧹 Удалено старых транскриптов: {cur.rowcount}")
        conn.commit()
    finally:
        cur.close()
        conn.close()
