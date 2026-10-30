"""Подключение к SQLite, миграции, очистка временных файлов."""

import os
import sqlite3

from config import DB_PATH, MAX_TASK_ATTEMPTS


def _cleanup_temp_files():
    cwd = os.getcwd()
    try:
        for f in os.listdir(cwd):
            if f == "audio.mp3" or (f.startswith("audio.part") and f.endswith(".m4a")):
                try:
                    os.remove(os.path.join(cwd, f))
                    print(f"🧹 Удалён остаток: {f}")
                except Exception:
                    pass
    except Exception:
        pass


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _init_db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass

    conn = sqlite3.connect(DB_PATH, timeout=10.0)

    # WAL позволяет читать во время записи (воркер + планировщик + Telegram-хендлеры)
    # и снижает вероятность "database is locked". PRAGMA сохраняется между запусками.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception as e:
        print(f"⚠️  PRAGMA WAL: {e}")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_videos ("
        "video_id TEXT PRIMARY KEY, "
        "processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS predictions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "video_id TEXT NOT NULL, "
        "expert_name TEXT NOT NULL, "
        "channel_name TEXT, "
        "prediction_text TEXT NOT NULL, "
        "prediction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "verdict TEXT DEFAULT NULL, "
        "verdict_date TIMESTAMP DEFAULT NULL, "
        "verdict_notes TEXT DEFAULT NULL, "
        "UNIQUE(video_id, prediction_text))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(prediction_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pred_expert ON predictions(expert_name)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS experts_seen_today ("
        "date TEXT NOT NULL, "
        "expert_name TEXT NOT NULL, "
        "seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (date, expert_name))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_experts_date ON experts_seen_today(date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL UNIQUE,
            expert_name TEXT NOT NULL,
            channel_name TEXT,
            summary_text TEXT NOT NULL,
            video_title TEXT,
            video_published_at TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_date ON video_summaries(processed_at)")

    # ─── Кеш транскриптов ──────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcripts_cache (
            video_id TEXT PRIMARY KEY,
            transcript TEXT NOT NULL,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_date ON transcripts_cache(cached_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_url TEXT NOT NULL,
            video_id TEXT,
            video_title TEXT,
            video_published_at TEXT,
            channel_name TEXT,
            channel_type TEXT DEFAULT 'media',
            channel_owner TEXT DEFAULT '',
            priority INTEGER DEFAULT 5,
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'auto',
            attempts INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            next_retry_at TIMESTAMP
        )
    """)

    if not _column_exists(conn, "task_queue", "video_published_at"):
        conn.execute("ALTER TABLE task_queue ADD COLUMN video_published_at TEXT")
        print("🔄 Миграция: добавлена колонка video_published_at")

    if not _column_exists(conn, "task_queue", "next_retry_at"):
        conn.execute("ALTER TABLE task_queue ADD COLUMN next_retry_at TIMESTAMP")
        print("🔄 Миграция: добавлена колонка next_retry_at")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tq_status ON task_queue(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tq_priority ON task_queue(priority DESC, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tq_video_id ON task_queue(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tq_next_retry ON task_queue(next_retry_at)")

    n_reset = conn.execute(
        "UPDATE task_queue SET status = 'pending', started_at = NULL, next_retry_at = NULL "
        "WHERE status = 'processing'"
    ).rowcount
    if n_reset:
        print(f"🔄 Сброшено зависших processing → pending: {n_reset}")

    n_max = conn.execute(
        "UPDATE task_queue SET status = 'error', "
        "error_message = 'max_attempts_exceeded', "
        "finished_at = CURRENT_TIMESTAMP, "
        "next_retry_at = NULL "
        "WHERE status = 'pending' AND attempts >= ?",
        (MAX_TASK_ATTEMPTS,)
    ).rowcount
    if n_max:
        print(f"🛑 Помечено error (max attempts {MAX_TASK_ATTEMPTS}): {n_max}")

    conn.commit()
    conn.close()