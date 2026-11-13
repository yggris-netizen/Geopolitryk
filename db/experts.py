"""CRUD для experts_seen_today (Postgres)."""
from datetime import datetime, timedelta, timezone

from config import DISPLAY_TIMEZONE_OFFSET
from db.connection import get_conn


def _get_today_msk() -> str:
    now_msk = datetime.now(timezone.utc) + timedelta(hours=DISPLAY_TIMEZONE_OFFSET)
    return now_msk.strftime("%Y-%m-%d")


def _get_experts_seen_today() -> set:
    today = _get_today_msk()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT expert_name FROM experts_seen_today WHERE date=%s", (today,))
        return {r[0] for r in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def _mark_expert_seen_today(expert_name: str):
    if not expert_name or expert_name == "Неизвестный эксперт":
        return
    today = _get_today_msk()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO experts_seen_today (date, expert_name) VALUES (%s, %s) "
            "ON CONFLICT (date, expert_name) DO NOTHING",
            (today, expert_name)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _cleanup_old_experts(days_keep: int = 30):
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=DISPLAY_TIMEZONE_OFFSET)
              - timedelta(days=days_keep)).strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM experts_seen_today WHERE date < %s", (cutoff,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
