"""CRUD для task_queue."""

import random
import sqlite3

from config import DB_PATH, MAX_TASK_ATTEMPTS, _classify_error
from db.videos import _is_video_processed


def _add_task_to_queue(video_url, video_id="", video_title="", video_published_at="",
                       channel_name="", channel_type="media", channel_owner="",
                       priority=5, source="auto"):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        if video_id:
            row = conn.execute(
                "SELECT id FROM task_queue WHERE video_id = ? AND status IN ('pending','processing')",
                (video_id,)
            ).fetchone()
            if row:
                return None
            if _is_video_processed(video_id):
                return None
        else:
            row = conn.execute(
                "SELECT id FROM task_queue WHERE video_url = ? AND status IN ('pending','processing')",
                (video_url,)
            ).fetchone()
            if row:
                return None
        cur = conn.execute("""
            INSERT INTO task_queue
                (video_url, video_id, video_title, video_published_at, channel_name,
                 channel_type, channel_owner, priority, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (video_url, video_id, video_title, video_published_at, channel_name,
              channel_type, channel_owner, priority, source))
        tid = cur.lastrowid
        conn.commit()
        return tid
    finally:
        conn.close()


def _get_next_pending_task():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM task_queue WHERE status = 'pending' "
        "AND attempts < ? "
        "AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP) "
        "ORDER BY priority DESC, created_at ASC LIMIT 1",
        (MAX_TASK_ATTEMPTS,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _mark_task_processing(task_id):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute(
        "UPDATE task_queue SET status = 'processing', started_at = CURRENT_TIMESTAMP, "
        "next_retry_at = NULL, "
        "attempts = attempts + 1 WHERE id = ?", (task_id,)
    )
    conn.commit()
    conn.close()


def _mark_task_done(task_id, error_message=""):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    if error_message:
        conn.execute(
            "UPDATE task_queue SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
            "next_retry_at = NULL, "
            "error_message = ? WHERE id = ?", (error_message[:2000], task_id)
        )
    else:
        conn.execute(
            "UPDATE task_queue SET status = 'done', finished_at = CURRENT_TIMESTAMP, "
            "next_retry_at = NULL WHERE id = ?",
            (task_id,)
        )
    conn.commit()
    conn.close()


def _mark_task_retry(task_id, error_message):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    row = conn.execute(
        "SELECT attempts FROM task_queue WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    attempts = row[0] or 0
    delay_minutes = min(2 ** attempts, 60)
    jitter_seconds = random.randint(0, 30)
    conn.execute(
        "UPDATE task_queue SET status = 'pending', "
        "error_message = ?, "
        "next_retry_at = datetime('now', ?, ?), "
        "started_at = NULL, "
        "finished_at = NULL "
        "WHERE id = ?",
        (error_message[:2000], f"+{delay_minutes} minutes",
         f"+{jitter_seconds} seconds", task_id)
    )
    conn.commit()
    conn.close()
    print(f"   ⏸  #{task_id} → retry через {delay_minutes}м{jitter_seconds}с "
          f"(attempts={attempts}, err={error_message[:80]})")


def _handle_task_failure(task_id, reason, attempts_now):
    err_class = _classify_error(reason)
    if err_class != "fatal" and attempts_now < MAX_TASK_ATTEMPTS:
        _mark_task_retry(task_id, reason)
    else:
        _mark_task_done(task_id, error_message=reason)
        print(f"   ⚠️  #{task_id} → error "
              f"(class={err_class}, attempts={attempts_now}/{MAX_TASK_ATTEMPTS}): "
              f"{reason[:120]}")


def _cancel_task(task_id):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        row = conn.execute("SELECT status FROM task_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False, "не найдена"
        if row[0] in ('done', 'error'):
            return False, f"уже {row[0]}"
        conn.execute(
            "UPDATE task_queue SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
            "next_retry_at = NULL, "
            "error_message = 'cancelled' WHERE id = ?", (task_id,)
        )
        conn.commit()
        return True, "отменена"
    finally:
        conn.close()


def _reset_queue():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        n = conn.execute(
            "UPDATE task_queue SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
            "next_retry_at = NULL, "
            "error_message = 'manually_reset' "
            "WHERE status IN ('pending','processing')"
        ).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _queue_stats():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    rows = conn.execute("SELECT status, COUNT(*) FROM task_queue GROUP BY status").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _queue_list(limit=20):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM task_queue WHERE status IN ('pending','processing') "
        "ORDER BY priority DESC, created_at ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _recent_errors(limit=10):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, video_title, video_url, error_message, attempts, finished_at "
        "FROM task_queue WHERE status = 'error' "
        "ORDER BY finished_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _cleanup_done_tasks(days_keep=3):
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute(
        "DELETE FROM task_queue WHERE status IN ('done','error') "
        "AND finished_at < datetime('now', ?)", (f'-{days_keep} days',)
    )
    conn.commit()
    conn.close()