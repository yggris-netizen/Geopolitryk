"""CRUD для experts_seen_today."""

import sqlite3
from datetime import datetime, timedelta, timezone

from config import DB_PATH, DISPLAY_TIMEZONE_OFFSET


def _get_today_msk() -> str:
    now_msk = datetime.now(timezone.utc) + timedelta(hours=DISPLAY_TIMEZONE_OFFSET)
    return now_msk.strftime("%Y-%m-%d")


def _get_experts_seen_today() -> set:
    today = _get_today_msk()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT expert_name FROM experts_seen_today WHERE date = ?", (today,)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _mark_expert_seen_today(expert_name: str):
    if not expert_name or expert_name == "Неизвестный эксперт":
        return
    today = _get_today_msk()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO experts_seen_today (date, expert_name) VALUES (?, ?)",
            (today, expert_name)
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup_old_experts(days_keep: int = 30):
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=DISPLAY_TIMEZONE_OFFSET) - timedelta(days=days_keep)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM experts_seen_today WHERE date < ?", (cutoff,))
    conn.commit()
    conn.close()