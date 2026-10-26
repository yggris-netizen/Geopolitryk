"""CRUD для predictions."""

import sqlite3

from config import DB_PATH


def _save_predictions(video_id, expert_name, channel_name, predictions_list):
    if not predictions_list:
        return 0
    conn = sqlite3.connect(DB_PATH)
    saved = 0
    for p in predictions_list:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO predictions "
                "(video_id, expert_name, channel_name, prediction_text) VALUES (?, ?, ?, ?)",
                (video_id, expert_name, channel_name, p)
            )
            saved += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return saved


def _recent_predictions(limit=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, expert_name, channel_name, prediction_text, "
        "       prediction_date, verdict "
        "FROM predictions "
        "ORDER BY prediction_date DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_expert_priority_rating(expert_name: str) -> float:
    if not expert_name or expert_name == "Неизвестный эксперт":
        return 50.0
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT verdict FROM predictions WHERE expert_name = ? AND verdict IN ('confirmed','refuted')",
        (expert_name,)
    ).fetchall()
    conn.close()
    if not rows:
        return 50.0
    return round(sum(1 for r in rows if r[0] == "confirmed") / len(rows) * 100, 1)