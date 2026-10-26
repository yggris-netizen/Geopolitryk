"""CRUD для video_summaries."""

import sqlite3

from config import DB_PATH


def _save_summary(video_id, expert_name, channel_name, summary_text,
                  video_title="", video_published_at=""):
    if not video_id or not summary_text:
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO video_summaries "
            "(video_id, expert_name, channel_name, summary_text, "
            " video_title, video_published_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, expert_name, channel_name, summary_text,
             video_title, video_published_at)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"⚠️  _save_summary: {e}")
        return False
    finally:
        conn.close()