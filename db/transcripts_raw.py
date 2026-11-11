"""CRUD для transcripts_raw: сырые сегменты транскриптов для Бота 3.

Контракт schema_version=2:
  transcript_json = '[{"start": 0.0, "end": 4.2, "text": "...", "speaker": "SPEAKER_01"}, ...]'
  start/end — секунды (float).
  speaker — ID спикера ("SPEAKER_00", "SPEAKER_01", "UNKNOWN") или "".
"""

import json
import sqlite3

from config import DB_PATH


def save_transcript_raw(video_id, interview_id, expert_name, source_url,
                        segments, language="ru", duration_sec=0, schema_version=2):
    """Сохраняет сегменты транскрипта в transcripts_raw."""
    if not video_id or not segments:
        return False
    payload = json.dumps(segments, ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO transcripts_raw "
            "(video_id, interview_id, expert_name, source_url, transcript_json, "
            " language, duration_sec, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (video_id, interview_id or "", expert_name or "", source_url or "",
             payload, language or "ru", int(duration_sec or 0), schema_version)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[WARN]  save_transcript_raw: {e}")
        return False
    finally:
        conn.close()


def get_transcript_raw(video_id):
    if not video_id:
        return None
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, video_id, interview_id, expert_name, source_url, "
        "       transcript_json, language, duration_sec, schema_version, created_at "
        "FROM transcripts_raw WHERE video_id = ?", (video_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_transcript_raw_segments(video_id):
    row = get_transcript_raw(video_id)
    if not row:
        return []
    try:
        return json.loads(row["transcript_json"])
    except (json.JSONDecodeError, TypeError):
        return []


def has_transcript_raw(video_id) -> bool:
    if not video_id:
        return False
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    row = conn.execute(
        "SELECT 1 FROM transcripts_raw WHERE video_id = ?", (video_id,)
    ).fetchone()
    conn.close()
    return row is not None