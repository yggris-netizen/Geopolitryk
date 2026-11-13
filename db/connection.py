"""Подключение к Postgres, миграции, очистка временных файлов."""
import os

import psycopg2
import psycopg2.extras

from config import DATABASE_URL, MAX_TASK_ATTEMPTS


# Prisma-параметры, которые psycopg2 не понимает
_PRISMA_PARAMS = ("connection_limit", "pool_timeout", "pgbouncer", "connect_timeout_prisma")


def _sanitize_dsn(dsn: str) -> str:
    """Убирает Prisma-параметры из DSN (?sslmode=require&connection_limit=10 -> ?sslmode=require)."""
    if not dsn:
        return dsn
    base, sep, query = dsn.partition("?")
    if not sep:
        return dsn
    keep = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key in _PRISMA_PARAMS:
            continue
        keep.append(part)
    return base + ("?" + "&".join(keep) if keep else "")


def get_conn():
    """Единая точка подключения к Postgres (DSN санитизируется)."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в .env (Postgres обязателен)")
    dsn = _sanitize_dsn(DATABASE_URL)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


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


def _init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_videos (
                video_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                video_id TEXT NOT NULL,
                expert_name TEXT NOT NULL,
                channel_name TEXT,
                prediction_text TEXT NOT NULL,
                prediction_date TIMESTAMP DEFAULT NOW(),
                verdict TEXT DEFAULT NULL,
                verdict_date TIMESTAMP DEFAULT NULL,
                verdict_notes TEXT DEFAULT NULL,
                source_url TEXT,
                fact_check_date TIMESTAMP,
                speaker_id TEXT,
                UNIQUE(video_id, prediction_text)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(prediction_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pred_expert ON predictions(expert_name)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS experts_seen_today (
                date TEXT NOT NULL,
                expert_name TEXT NOT NULL,
                seen_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (date, expert_name)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_experts_date ON experts_seen_today(date)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS video_summaries (
                id SERIAL PRIMARY KEY,
                video_id TEXT NOT NULL UNIQUE,
                expert_name TEXT NOT NULL,
                channel_name TEXT,
                summary_text TEXT NOT NULL,
                video_title TEXT,
                video_published_at TEXT,
                processed_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_summaries_date ON video_summaries(processed_at)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS transcripts_cache (
                video_id TEXT PRIMARY KEY,
                transcript TEXT NOT NULL,
                cached_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_date ON transcripts_cache(cached_at)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS transcripts_raw (
                id SERIAL PRIMARY KEY,
                video_id TEXT UNIQUE NOT NULL,
                interview_id TEXT,
                expert_name TEXT,
                source_url TEXT,
                transcript_json TEXT NOT NULL,
                language TEXT,
                duration_sec INTEGER,
                schema_version INTEGER DEFAULT 2,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_raw_video ON transcripts_raw(video_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_raw_expert ON transcripts_raw(expert_name)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS voice_profiles (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                bio TEXT DEFAULT '',
                role TEXT NOT NULL DEFAULT 'guest',
                embedding BYTEA NOT NULL,
                duration_sec REAL DEFAULT 0,
                source_video_id TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_voice_profiles_name ON voice_profiles(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_voice_profiles_role ON voice_profiles(role)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_voices (
                id SERIAL PRIMARY KEY,
                video_id TEXT NOT NULL,
                video_url TEXT NOT NULL,
                speaker_id TEXT DEFAULT '',
                channel_name TEXT,
                channel_type TEXT,
                embedding BYTEA NOT NULL,
                duration_sec REAL DEFAULT 0,
                role_guess TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                resolved_at TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_voices_status ON pending_voices(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_voices_video ON pending_voices(video_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_queue (
                id SERIAL PRIMARY KEY,
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
                created_at TIMESTAMP DEFAULT NOW(),
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                next_retry_at TIMESTAMP,
                remote_path TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tq_status ON task_queue(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tq_priority ON task_queue(priority DESC, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tq_video_id ON task_queue(video_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tq_next_retry ON task_queue(next_retry_at)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id SERIAL PRIMARY KEY,
                model TEXT NOT NULL,
                task TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_model_date "
                    "ON token_usage(model, created_at)")

        # сброс зависших processing -> pending
        cur.execute("UPDATE task_queue SET status='pending', started_at=NULL, "
                    "next_retry_at=NULL WHERE status='processing'")
        if cur.rowcount:
            print(f"[RETRY] Сброшено зависших processing -> pending: {cur.rowcount}")

        # max attempts -> error
        cur.execute("UPDATE task_queue SET status='error', "
                    "error_message='max_attempts_exceeded', "
                    "finished_at=NOW(), next_retry_at=NULL "
                    "WHERE status='pending' AND attempts >= %s", (MAX_TASK_ATTEMPTS,))
        if cur.rowcount:
            print(f"🛑 Помечено error (max attempts {MAX_TASK_ATTEMPTS}): {cur.rowcount}")

        conn.commit()
    finally:
        cur.close()
        conn.close()
