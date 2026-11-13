"""CRUD для transcripts_raw (Postgres)."""
import json

import psycopg2.extras

from db.connection import get_conn


def save_transcript_raw(video_id, interview_id, expert_name, source_url,
                        segments, language="ru", duration_sec=0, schema_version=2):
    if not video_id or not segments:
        return False
    payload = json.dumps(segments, ensure_ascii=False)
    clean_expert = "" if (expert_name or "").strip() in (
        "Неизвестный спикер", "Неизвестный эксперт", "?") else (expert_name or "")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO transcripts_raw "
            "(video_id, interview_id, expert_name, source_url, transcript_json, "
            " language, duration_sec, schema_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (video_id) DO UPDATE SET "
            "interview_id=EXCLUDED.interview_id, expert_name=EXCLUDED.expert_name, "
            "source_url=EXCLUDED.source_url, transcript_json=EXCLUDED.transcript_json, "
            "language=EXCLUDED.language, duration_sec=EXCLUDED.duration_sec, "
            "schema_version=EXCLUDED.schema_version",
            (video_id, interview_id or "", clean_expert, source_url or "",
             payload, language or "ru", int(duration_sec or 0), schema_version)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[WARN]  save_transcript_raw: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def get_transcript_raw(video_id):
    if not video_id:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, video_id, interview_id, expert_name, source_url, "
            "       transcript_json, language, duration_sec, schema_version, created_at "
            "FROM transcripts_raw WHERE video_id=%s", (video_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close()
        conn.close()


def get_transcript_raw_segments(video_id):
    row = get_transcript_raw(video_id)
    if not row:
        return []
    try:
        return json.loads(row["transcript_json"])
    except (json.JSONDecodeError, TypeError):
        return []


def get_transcript_raw_expert_name(video_id):
    if not video_id:
        return ""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT expert_name FROM transcripts_raw WHERE video_id=%s", (video_id,))
        row = cur.fetchone()
        if not row:
            return ""
        val = (row[0] or "").strip()
        if val in ("Неизвестный спикер", "Неизвестный эксперт", "?"):
            return ""
        return val
    finally:
        cur.close()
        conn.close()


def has_transcript_raw(video_id) -> bool:
    if not video_id:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM transcripts_raw WHERE video_id=%s", (video_id,))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()
