"""CRUD для processed_videos (Postgres)."""
from db.connection import get_conn


def _is_video_processed(video_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM processed_videos WHERE video_id=%s", (video_id,))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def _mark_video_processed(video_id: str):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO processed_videos (video_id) VALUES (%s) "
            "ON CONFLICT (video_id) DO NOTHING", (video_id,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
