"""CRUD для task_queue (Postgres, атомарный захват)."""
import random
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from config import MAX_TASK_ATTEMPTS, _classify_error
from db.connection import get_conn
from db.videos import _is_video_processed


def _add_task_to_queue(video_url, video_id="", video_title="", video_published_at="",
                       channel_name="", channel_type="media", channel_owner="",
                       priority=5, source="auto"):
    conn = get_conn()
    cur = conn.cursor()
    try:
        if video_id:
            cur.execute(
                "SELECT id FROM task_queue WHERE video_id = %s "
                "AND status IN ('pending','processing','uploaded','processed')",
                (video_id,)
            )
            if cur.fetchone():
                return None
            if _is_video_processed(video_id):
                return None
        else:
            cur.execute(
                "SELECT id FROM task_queue WHERE video_url = %s "
                "AND status IN ('pending','processing','uploaded','processed')",
                (video_url,)
            )
            if cur.fetchone():
                return None
        cur.execute("""
            INSERT INTO task_queue
                (video_url, video_id, video_title, video_published_at, channel_name,
                 channel_type, channel_owner, priority, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (video_url, video_id, video_title, video_published_at, channel_name,
              channel_type, channel_owner, priority, source))
        tid = cur.fetchone()[0]
        conn.commit()
        return tid
    finally:
        cur.close()
        conn.close()


def _atomic_grab(status):
    """Атомарно взять задачу: UPDATE ... RETURNING * с SKIP LOCKED."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            UPDATE task_queue
            SET status='processing', started_at=NOW(),
                next_retry_at=NULL, attempts=attempts+1
            WHERE id = (
                SELECT id FROM task_queue
                WHERE status=%s AND attempts < %s
                  AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
        """, (status, MAX_TASK_ATTEMPTS))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        cur.close()
        conn.close()


def _get_next_pending_task():
    return _atomic_grab('pending')


def _get_next_task_by_status(status):
    return _atomic_grab(status)


def _mark_task_processing(task_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='processing', started_at=NOW(), "
            "next_retry_at=NULL, attempts=attempts+1 WHERE id=%s", (task_id,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _mark_task_done(task_id, error_message=""):
    conn = get_conn()
    cur = conn.cursor()
    try:
        if error_message:
            cur.execute(
                "UPDATE task_queue SET status='error', finished_at=NOW(), "
                "next_retry_at=NULL, error_message=%s WHERE id=%s",
                (error_message[:2000], task_id)
            )
        else:
            cur.execute(
                "UPDATE task_queue SET status='done', finished_at=NOW(), "
                "next_retry_at=NULL WHERE id=%s", (task_id,)
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _mark_task_retry(task_id, error_message):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT attempts FROM task_queue WHERE id=%s", (task_id,))
        row = cur.fetchone()
        if not row:
            return
        attempts = row[0] or 0
        delay_minutes = min(2 ** attempts, 60)
        jitter_seconds = random.randint(0, 30)
        cur.execute(
            "UPDATE task_queue SET status='pending', error_message=%s, "
            "next_retry_at=NOW() + make_interval(mins => %s, secs => %s), "
            "started_at=NULL, finished_at=NULL WHERE id=%s",
            (error_message[:2000], delay_minutes, jitter_seconds, task_id)
        )
        conn.commit()
        print(f"   [PAUSE]  #{task_id} -> retry через {delay_minutes}м{jitter_seconds}с "
              f"(attempts={attempts}, err={error_message[:80]})")
    finally:
        cur.close()
        conn.close()


def _mark_task_retry_tpd(task_id, error_message):
    """TPD исчерпан: ждём до 03:00 МСК (00:00 UTC). attempts НЕ расходуем."""
    now_utc = datetime.now(timezone.utc)
    next_reset = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='pending', error_message=%s, "
            "next_retry_at=%s, attempts=GREATEST(0, attempts-1), "
            "started_at=NULL, finished_at=NULL WHERE id=%s",
            (error_message[:2000], next_reset, task_id)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"   [TPD]  #{task_id} -> ждём до {next_reset.isoformat()} UTC (03:00 МСК), attempts не расходуем")


def _mark_task_uploaded(task_id, remote_path=""):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='uploaded', remote_path=%s, "
            "started_at=NULL, finished_at=NULL, error_message=NULL, "
            "next_retry_at=NULL WHERE id=%s",
            (remote_path or "", task_id)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"   [OK] #{task_id} -> uploaded ({remote_path or '—'})")


def _mark_task_processed(task_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='processed', finished_at=NULL, "
            "error_message=NULL WHERE id=%s", (task_id,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"   [OK] #{task_id} -> processed")


def _mark_task_published(task_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='published', finished_at=NOW() WHERE id=%s",
            (task_id,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"   [OK] #{task_id} -> published")


def _handle_task_failure(task_id, reason, attempts_now):
    if "tpd_exhausted" in (reason or "").lower():
        _mark_task_retry_tpd(task_id, reason)
        return
    err_class = _classify_error(reason)
    if err_class != "fatal" and attempts_now < MAX_TASK_ATTEMPTS:
        _mark_task_retry(task_id, reason)
    else:
        _mark_task_done(task_id, error_message=reason)
        print(f"   [WARN]  #{task_id} -> error "
              f"(class={err_class}, attempts={attempts_now}/{MAX_TASK_ATTEMPTS}): {reason[:120]}")


def _cancel_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status FROM task_queue WHERE id=%s", (task_id,))
        row = cur.fetchone()
        if not row:
            return False, "не найдена"
        if row[0] in ('done', 'error'):
            return False, f"уже {row[0]}"
        cur.execute(
            "UPDATE task_queue SET status='error', finished_at=NOW(), "
            "next_retry_at=NULL, error_message='cancelled' WHERE id=%s", (task_id,)
        )
        conn.commit()
        return True, "отменена"
    finally:
        cur.close()
        conn.close()


def _reset_queue():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE task_queue SET status='error', finished_at=NOW(), "
            "next_retry_at=NULL, error_message='manually_reset' "
            "WHERE status IN ('pending','processing')"
        )
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def _queue_stats():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, COUNT(*) FROM task_queue GROUP BY status")
        rows = cur.fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        cur.close()
        conn.close()


def _queue_list(limit=20, statuses=None):
    if statuses is None:
        statuses = ('pending', 'processing')
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM task_queue WHERE status = ANY(%s) "
            "ORDER BY priority DESC, created_at ASC LIMIT %s",
            (list(statuses), limit)
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        cur.close()
        conn.close()


def _recent_errors(limit=10):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT id, video_title, video_url, error_message, attempts, finished_at "
            "FROM task_queue WHERE status='error' "
            "ORDER BY finished_at DESC LIMIT %s", (limit,)
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        cur.close()
        conn.close()


def _cleanup_done_tasks(days_keep=3):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM task_queue WHERE status IN ('done','error','published') "
            "AND finished_at < NOW() - make_interval(days => %s)", (days_keep,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
