"""CRUD для processed_videos."""

import sqlite3

from config import DB_PATH


def _is_video_processed(video_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM processed_videos WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    return row is not None


def _mark_video_processed(video_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO processed_videos (video_id) VALUES (?)", (video_id,))
    conn.commit()
    conn.close()