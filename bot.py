"""
YouTube → Telegram бот: аналитические выжимки.
Единая очередь + Telegram-управление + воркер.
Глубина поиска: только 12 часов, без фолбэка.

Версия БЕЗ новостных дайджестов (RSS).
Сохраняет выжимки экспертов в video_summaries для будущего сравнения повестки.

Версия 3 (post-run fixes):
  - Разбивка длинных сообщений >4096 символов (Telegram limit)
  - Очистка мусорных окончаний в именах (Сытникнуть → Сытник)
  - Ужесточение промпта прогнозов (отсечение «событие, о котором говорится»)
  - message is too long → fatal (после внедрения разбивки)

Версия 2 (high-priority hardening):
  - RSS-first discovery: playlistItems.list только fallback (экономия квоты YouTube)
  - Exponential backoff для transient-ошибок (429, timeout, quota)
  - Классификация ошибок: transient / fatal
  - Колонка next_retry_at в task_queue
  - DB_PATH читается из env (для Bothost: /app/data/seen_videos.db)

Оптимизации RAM:
  - WHISPER_MODE=groq (без локального Whisper)
  - gc.collect() после каждой задачи
  - Ограничение роста кэшей YouTube (1000 записей)
  - Очистка audio.* при старте
  - RAM в /stats и /heartbeat
"""

import asyncio
import html
import json
import os
import random
import re
import signal
import subprocess
import sys
import sqlite3
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception as _e:
        print(f"⚠️  WindowsSelectorEventLoopPolicy: {_e}")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠️  psutil не установлен")

import httpx
import yt_dlp
from groq import Groq
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    import pymorphy3
    _MORPH = pymorphy3.MorphAnalyzer()
    MORPH_AVAILABLE = True
except ImportError:
    _MORPH = None
    MORPH_AVAILABLE = False
    print("⚠️  pymorphy3 не установлен")

from experts import KNOWN_EXPERT_BIOS, KNOWN_EXPERT_FULL


# ╔══════════════════════════════════════════════════════════╗
# ║                    НАСТРОЙКИ                             ║
# ╚══════════════════════════════════════════════════════════╝

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

WHISPER_MODE = os.getenv("WHISPER_MODE", "groq")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

MAX_TASK_ATTEMPTS = int(os.getenv("MAX_TASK_ATTEMPTS", "5"))
DIAGNOSTIC_MODE = os.getenv("DIAGNOSTIC_MODE", "0") == "1"
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")
GROQ_MAX_CHUNK_SECONDS = int(os.getenv("GROQ_MAX_CHUNK_SECONDS", "300"))
HEARTBEAT_HOURS = int(os.getenv("HEARTBEAT_HOURS", "0"))

GROQ_MAX_SIZE_MB = 24.0

TELEGRAM_MAX_LEN = 4096

_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if _creds_json:
    _creds_json = _creds_json.strip()
    if len(_creds_json) >= 2 and _creds_json[0] == '"' and _creds_json[-1] == '"':
        _creds_json = _creds_json[1:-1]
    _creds_json = _creds_json.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    try:
        _parsed_creds = json.loads(_creds_json)
        if not isinstance(_parsed_creds, dict) or "type" not in _parsed_creds:
            raise ValueError("JSON не содержит поля 'type'")
    except json.JSONDecodeError as _e:
        print(f"❌ GOOGLE_CREDENTIALS_JSON невалиден: {_e}")
        print(f"   Первые 80 символов: {_creds_json[:80]!r}")
        sys.exit(1)
    _creds_json = json.dumps(_parsed_creds, ensure_ascii=False)
    _creds_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    _creds_tmp.write(_creds_json)
    _creds_tmp.close()
    SERVICE_ACCOUNT_FILE = _creds_tmp.name
else:
    SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

CHANNELS_TO_WATCH = [
    {"id": "UCSRtRl2Msw71NnYQduHmMgw", "name": "Александр Шелест", "type": "author", "owner": "Александр Шелест"},
    {"id": "UCT0ve_Yq3PPSjw3pJDOK5DA", "name": "Руслан Бортник",   "type": "author", "owner": "Руслан Бортник"},
    {"id": "UCe3crzYhR7bx4sTY-83H2Gg", "name": "Дикий live",       "type": "author", "owner": "Виталий Дикий"},
    {"id": "UCdGKO6B6OWbEtWRk6bx4HkA", "name": "КАТАРСИС",         "type": "media"},
    {"id": "UCHgiqmsIHN2PlQnBIRFRGGg", "name": "Politeka Online",  "type": "media"},
    {"id": "UCxkdtxa54kQLRmmWaBH0K2w", "name": "UKRLIFETV",        "type": "media"},
]

TEST_VIDEO_URL = ""
TEST_VIDEO_TITLE = ""

VIDEO_LANGUAGE = "ru"

DB_PATH = os.getenv("DB_PATH", "seen_videos.db")

CHECK_INTERVAL_HOURS = 3
WORKER_POLL_INTERVAL = 5

HOURS_BACK = int(os.getenv("HOURS_BACK", "12"))
DAYS_BACK = HOURS_BACK / 24

MIN_VIDEO_DURATION_SECONDS = 180
MAX_VIDEO_DURATION_SECONDS = 5400
MAX_VIDEOS_PER_CHANNEL_PER_CHECK = int(os.getenv("MAX_VIDEOS_PER_CHANNEL_PER_CHECK", "5"))
MAX_REPEAT_EXPERTS_PER_CYCLE = 2
MIN_TRANSCRIPT_CHARS = 100

_YOUTUBE_UPLOADS_CACHE = {}
_YOUTUBE_VIDEOS_CACHE = {}
_YOUTUBE_DURATIONS_CACHE = {}
YOUTUBE_VIDEOS_CACHE_TTL = 1800
YOUTUBE_DURATIONS_CACHE_TTL = 86400
YOUTUBE_CACHE_MAX_ENTRIES = 1000

_QUOTA_USED = {"count": 0, "date": "", "by_op": {}}
YOUTUBE_DAILY_QUOTA = 10000

_SHUTDOWN_EVENT = None

NEUTRALIZE_TITLE = True
MAX_TITLE_LEN = 90
GENERATE_HASHTAGS = True

EXTRACT_PREDICTIONS = True
MAX_PREDICTIONS_PER_VIDEO = 10
MAX_PREDICTION_HORIZON_YEARS = 5

GROQ_DELAY_BETWEEN_CHUNKS = 5
GROQ_MAX_RETRIES = 5

DISPLAY_TIMEZONE_OFFSET = 3


# ╔══════════════════════════════════════════════════════════╗
# ║         ДИАГНОСТИКА + КВОТА + УТИЛИТЫ                    ║
# ╚══════════════════════════════════════════════════════════╝

def _log_memory(stage: str = ""):
    if not PSUTIL_AVAILABLE:
        return
    try:
        p = psutil.Process(os.getpid())
        mem_mb = p.memory_info().rss / 1024 / 1024
        print(f"   💾 [{stage}] RAM: {mem_mb:.0f} MB")
    except Exception:
        pass


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


def _maybe_trim_caches():
    if len(_YOUTUBE_VIDEOS_CACHE) > YOUTUBE_CACHE_MAX_ENTRIES:
        keys = list(_YOUTUBE_VIDEOS_CACHE.keys())[:len(_YOUTUBE_VIDEOS_CACHE) // 2]
        for k in keys:
            _YOUTUBE_VIDEOS_CACHE.pop(k, None)
    if len(_YOUTUBE_DURATIONS_CACHE) > YOUTUBE_CACHE_MAX_ENTRIES:
        keys = list(_YOUTUBE_DURATIONS_CACHE.keys())[:len(_YOUTUBE_DURATIONS_CACHE) // 2]
        for k in keys:
            _YOUTUBE_DURATIONS_CACHE.pop(k, None)


def _quota_tick(op: str, cost: int = 1):
    global _QUOTA_USED
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _QUOTA_USED["date"] != today_utc:
        _QUOTA_USED = {"count": 0, "date": today_utc, "by_op": {}}
    _QUOTA_USED["count"] += cost
    _QUOTA_USED["by_op"][op] = _QUOTA_USED["by_op"].get(op, 0) + cost


def _quota_info() -> dict:
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _QUOTA_USED["date"] != today_utc:
        return {"count": 0, "date": today_utc, "by_op": {}}
    return dict(_QUOTA_USED)


def _is_quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return "quotaexceeded" in s or ("quota" in s and "exceeded" in s)


# ─── Классификация ошибок ──────────────────────────────────

_FATAL_ERROR_MARKERS = (
    "too_short:", "too_long:", "transcript_too_short:",
    "ffmpeg_split_failed", "chunk_too_big",
    "video unavailable", "private video", "deleted video",
    "http error 404", "http error 410",
    "message is too long",
    "chat not found", "bot was blocked",
)

_TRANSIENT_ERROR_MARKERS = (
    "429", "rate_limit", "rate limit", "quotaexceeded",
    "timeout", "timed out", "connectionerror", "connection error",
    "connection reset", "remotedisconnected",
    "service unavailable", "bad gateway",
    " 500", " 502", " 503", " 504",
    "internal server error",
)


def _classify_error(reason: str) -> str:
    if not reason:
        return "unknown"
    r = reason.lower()
    for m in _FATAL_ERROR_MARKERS:
        if m in r:
            return "fatal"
    for m in _TRANSIENT_ERROR_MARKERS:
        if m in r:
            return "transient"
    return "unknown"


def _split_audio(src: str, chunk_seconds: int) -> list:
    base, _ = os.path.splitext(src)
    chunks = []
    idx = 0
    start = 0
    while True:
        out = f"{base}.part{idx}.m4a"
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-i", src,
            "-t", str(chunk_seconds), "-vn", "-c:a", "aac", "-b:a", "64k", out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except FileNotFoundError:
            print("   ❌ ffmpeg НЕ НАЙДЕН в PATH.")
            break
        except subprocess.CalledProcessError as e:
            err_tail = (e.stderr or b"")[-300:].decode("utf-8", errors="ignore")
            print(f"   ⚠️  ffmpeg split (start={start}): {err_tail}")
            break
        except Exception as e:
            print(f"   ⚠️  ffmpeg split: {e}")
            break
        if not os.path.exists(out) or os.path.getsize(out) < 1024:
            if os.path.exists(out):
                try:
                    os.remove(out)
                except Exception:
                    pass
            break
        chunks.append(out)
        start += chunk_seconds
        idx += 1
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src],
                check=True, capture_output=True, timeout=10, text=True,
            )
            total = float(probe.stdout.strip())
            if start >= total:
                break
        except Exception:
            break
    return chunks


# ╔══════════════════════════════════════════════════════════╗
# ║              ИНИЦИАЛИЗАЦИЯ                               ║
# ╚══════════════════════════════════════════════════════════╝

print("=" * 60)
print("ЗАПУСК БОТА")
print("=" * 60)

_errors = []
if not BOT_TOKEN:
    _errors.append("❌ BOT_TOKEN не задан")
if not GROQ_API_KEY:
    _errors.append("❌ GROQ_API_KEY не задан")
if not isinstance(CHANNEL_ID, int) or CHANNEL_ID >= 0:
    _errors.append("❌ CHANNEL_ID должен быть отрицательным")
if not isinstance(OWNER_USER_ID, int) or OWNER_USER_ID <= 0:
    _errors.append("❌ OWNER_USER_ID должен быть положительным")
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    _errors.append(f"❌ {SERVICE_ACCOUNT_FILE} не найден")
if WHISPER_MODE not in ("groq", "local"):
    _errors.append(f"❌ WHISPER_MODE: '{WHISPER_MODE}'")

for _ch in CHANNELS_TO_WATCH:
    if _ch["id"] != _ch["id"].strip():
        _errors.append(f"❌ ID «{_ch['name']}» содержит пробелы")

if _errors:
    print("\n⚠️  ОШИБКИ:\n")
    for _e in _errors:
        print(f"  {_e}")
    sys.exit(1)

print("✅ Настройки в порядке")
print(f"   Канал:            {CHANNEL_ID}")
print(f"   Owner:            {OWNER_USER_ID}")
print(f"   Модель LLM:       {GROQ_MODEL}")
print(f"   Режим Whisper:    {WHISPER_MODE}")
if WHISPER_MODE == "groq":
    print(f"   Groq Whisper:     {GROQ_WHISPER_MODEL}")
    print(f"   Нарезка:          {GROQ_MAX_CHUNK_SECONDS}с")
    print(f"   Лимит файла:      {GROQ_MAX_SIZE_MB} MB")
else:
    print(f"   Локальная:        {WHISPER_MODEL_SIZE}")
print(f"   Каналов:          {len(CHANNELS_TO_WATCH)}")
print(f"   Глубина поиска:   {HOURS_BACK} ч (без фолбэка)")
print(f"   Лимит видео/канал: {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
print(f"   Max attempts:     {MAX_TASK_ATTEMPTS}")
print(f"   БД:               {DB_PATH}")
print(f"   Словарь экспертов: {len(KNOWN_EXPERT_BIOS)} записей, "
      f"{len(KNOWN_EXPERT_FULL)} расширений")
if HEARTBEAT_HOURS > 0:
    print(f"   Heartbeat:        каждые {HEARTBEAT_HOURS} ч")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

try:
    _credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=YOUTUBE_SCOPES
    )
    youtube = build("youtube", "v3", credentials=_credentials)
    print("✅ YouTube API OK")
except Exception as _e:
    print(f"❌ YouTube API: {_e}")
    sys.exit(1)

_local_model = None
if WHISPER_MODE == "local":
    print(f"\nЗагружаю локальный Whisper '{WHISPER_MODEL_SIZE}'...")
    try:
        from faster_whisper import WhisperModel
        _local_model = WhisperModel(
            WHISPER_MODEL_SIZE, device="cpu", compute_type="int8",
            cpu_threads=1, num_workers=1,
        )
        print("✅ Whisper OK")
    except Exception as _e:
        print(f"❌ Whisper: {_e}")
        traceback.print_exc()
        sys.exit(1)
else:
    print("✅ Groq Whisper API (без локальной модели)")
print()


# ╔══════════════════════════════════════════════════════════╗
# ║            SQLITE                                         ║
# ╚══════════════════════════════════════════════════════════╝

def _init_db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception:
            pass

    conn = sqlite3.connect(DB_PATH)
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

    try:
        conn.execute("SELECT video_published_at FROM task_queue LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE task_queue ADD COLUMN video_published_at TEXT")
        print("🔄 Миграция: добавлена колонка video_published_at")

    try:
        conn.execute("SELECT next_retry_at FROM task_queue LIMIT 1")
    except sqlite3.OperationalError:
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


def _add_task_to_queue(video_url, video_id="", video_title="", video_published_at="",
                       channel_name="", channel_type="media", channel_owner="",
                       priority=5, source="auto"):
    conn = sqlite3.connect(DB_PATH)
    if video_id:
        row = conn.execute(
            "SELECT id FROM task_queue WHERE video_id = ? AND status IN ('pending','processing')",
            (video_id,)
        ).fetchone()
        if row:
            conn.close()
            return None
        if _is_video_processed(video_id):
            conn.close()
            return None
    else:
        row = conn.execute(
            "SELECT id FROM task_queue WHERE video_url = ? AND status IN ('pending','processing')",
            (video_url,)
        ).fetchone()
        if row:
            conn.close()
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
    conn.close()
    return tid


def _get_next_pending_task():
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE task_queue SET status = 'processing', started_at = CURRENT_TIMESTAMP, "
        "next_retry_at = NULL, "
        "attempts = attempts + 1 WHERE id = ?", (task_id,)
    )
    conn.commit()
    conn.close()


def _mark_task_done(task_id, error_message=""):
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT attempts FROM task_queue WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    attempts = row[0] or 0
    delay_minutes = min(2 ** attempts, 60)
    conn.execute(
        "UPDATE task_queue SET status = 'pending', "
        "error_message = ?, "
        "next_retry_at = datetime('now', ?), "
        "started_at = NULL, "
        "finished_at = NULL "
        "WHERE id = ?",
        (error_message[:2000], f"+{delay_minutes} minutes", task_id)
    )
    conn.commit()
    conn.close()
    print(f"   ⏸  #{task_id} → retry через {delay_minutes} мин "
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
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return False, "не найдена"
    if row[0] in ('done', 'error'):
        conn.close()
        return False, f"уже {row[0]}"
    conn.execute(
        "UPDATE task_queue SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
        "next_retry_at = NULL, "
        "error_message = 'cancelled' WHERE id = ?", (task_id,)
    )
    conn.commit()
    conn.close()
    return True, "отменена"


def _reset_queue():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute(
        "UPDATE task_queue SET status = 'error', finished_at = CURRENT_TIMESTAMP, "
        "next_retry_at = NULL, "
        "error_message = 'manually_reset' "
        "WHERE status IN ('pending','processing')"
    ).rowcount
    conn.commit()
    conn.close()
    return n


def _queue_stats():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT status, COUNT(*) FROM task_queue GROUP BY status").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _queue_list(limit=20):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM task_queue WHERE status IN ('pending','processing') "
        "ORDER BY priority DESC, created_at ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _recent_errors(limit=10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, video_title, video_url, error_message, attempts, finished_at "
        "FROM task_queue WHERE status = 'error' "
        "ORDER BY finished_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def _cleanup_done_tasks(days_keep=3):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM task_queue WHERE status IN ('done','error') "
        "AND finished_at < datetime('now', ?)", (f'-{days_keep} days',)
    )
    conn.commit()
    conn.close()


# ╔══════════════════════════════════════════════════════════╗
# ║  YOUTUBE: RSS-first + API fallback                       ║
# ╚══════════════════════════════════════════════════════════╝

def _get_uploads_playlist_id(channel_id: str) -> str:
    if channel_id in _YOUTUBE_UPLOADS_CACHE:
        return _YOUTUBE_UPLOADS_CACHE[channel_id]
    try:
        _quota_tick("channels.list")
        response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    except HttpError as e:
        if _is_quota_error(e):
            print(f"   ⚠️  quotaExceeded в channels.list")
        else:
            print(f"   ❌ channels.list: {e}")
        return ""
    except Exception as e:
        print(f"   ❌ channels.list: {e}")
        return ""
    items = response.get("items", [])
    if not items:
        return ""
    uploads_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    if uploads_id:
        _YOUTUBE_UPLOADS_CACHE[channel_id] = uploads_id
        print(f"   🔑 uploads playlist: {uploads_id}")
    return uploads_id


def _get_channel_videos_rss(channel_id: str) -> list:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        xml_text = resp.text
    except Exception as e:
        print(f"   ❌ RSS fetch: {e}")
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"   ❌ RSS parse: {e}")
        return []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    videos = []
    for entry in root.findall("atom:entry", ns):
        try:
            vid_el = entry.find("yt:videoId", ns)
            if vid_el is None or not vid_el.text:
                continue
            title_el = entry.find("atom:title", ns)
            pub_el = entry.find("atom:published", ns)
            desc_el = entry.find(".//media:description", ns)
            videos.append({
                "video_id": vid_el.text,
                "title": title_el.text if title_el is not None else "",
                "url": f"https://www.youtube.com/watch?v={vid_el.text}",
                "channel": "",
                "description": desc_el.text if desc_el is not None and desc_el.text else "",
                "published_at": pub_el.text if pub_el is not None else "",
            })
        except Exception:
            continue
    print(f"   📡 RSS: получено {len(videos)} (без квоты)")
    return videos


def _get_channel_videos_raw(channel_id: str, max_results: int = 50) -> list:
    now = time.time()
    cached = _YOUTUBE_VIDEOS_CACHE.get(channel_id)
    if cached:
        ts, videos = cached
        if now - ts < YOUTUBE_VIDEOS_CACHE_TTL:
            print(f"   💾 кэш видео ({len(videos)} шт., {int(now - ts)}с назад)")
            return videos

    print(f"   📡 RSS-first discovery")
    videos = _get_channel_videos_rss(channel_id)
    if videos:
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, videos)
        _maybe_trim_caches()
        return videos

    print(f"   🔄 RSS пуст → API fallback")
    uploads_id = _get_uploads_playlist_id(channel_id)
    if not uploads_id:
        print(f"   ❌ API: uploads playlist не получен")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []

    try:
        _quota_tick("playlistItems.list")
        response = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=uploads_id,
            maxResults=min(max_results, 50),
        ).execute()
    except HttpError as e:
        print(f"   ❌ playlistItems.list: {e}")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []
    except Exception as e:
        print(f"   ❌ playlistItems.list: {e}")
        _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, [])
        _maybe_trim_caches()
        return []

    videos = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        video_id = snippet.get("resourceId", {}).get("videoId") or content.get("videoId", "")
        title = snippet.get("title", "")
        if not video_id or title in ("Private video", "Deleted video", "[Private video]", "[Deleted video]"):
            continue
        published_at = content.get("videoPublishedAt") or snippet.get("publishedAt", "")
        videos.append({
            "video_id": video_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel": snippet.get("channelTitle", ""),
            "description": snippet.get("description", ""),
            "published_at": published_at,
        })

    _YOUTUBE_VIDEOS_CACHE[channel_id] = (now, videos)
    _maybe_trim_caches()
    print(f"   📡 playlistItems (fallback): получено {len(videos)}")
    return videos


def _parse_iso_duration(duration: str) -> int:
    if not duration:
        return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _get_video_durations(video_ids: list) -> dict:
    if not video_ids:
        return {}
    now = time.time()
    durations = {}
    uncached = []
    for vid in video_ids:
        cached = _YOUTUBE_DURATIONS_CACHE.get(vid)
        if cached:
            ts, dur = cached
            if now - ts < YOUTUBE_DURATIONS_CACHE_TTL:
                durations[vid] = dur
                continue
        uncached.append(vid)

    if not uncached:
        print(f"   💾 длительности из кэша: {len(durations)}")
        return durations

    for i in range(0, len(uncached), 50):
        batch = uncached[i:i + 50]
        try:
            _quota_tick("videos.list")
            response = youtube.videos().list(
                part="contentDetails", id=",".join(batch), maxResults=50,
            ).execute()
            for item in response.get("items", []):
                vid = item["id"]
                dur_str = item.get("contentDetails", {}).get("duration", "")
                dur = _parse_iso_duration(dur_str)
                durations[vid] = dur
                _YOUTUBE_DURATIONS_CACHE[vid] = (now, dur)
            print(f"   📡 videos.list: получено {len(batch)} длительностей")
        except HttpError as e:
            if _is_quota_error(e):
                print(f"   ⚠️  quotaExceeded в videos.list — yt-dlp fallback")
                for vid in batch[:5]:
                    if vid in durations:
                        continue
                    try:
                        info = get_video_info_ytdlp(f"https://www.youtube.com/watch?v={vid}")
                        dur = int(info.get("duration", 0) or 0)
                        durations[vid] = dur
                        _YOUTUBE_DURATIONS_CACHE[vid] = (now, dur)
                    except Exception:
                        durations[vid] = 0
            else:
                print(f"   ⚠️  durations: {e}")
        except Exception as e:
            print(f"   ⚠️  durations: {e}")

    _maybe_trim_caches()
    return durations


def _format_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _format_published_date(published_at_iso: str) -> str:
    if not published_at_iso:
        return ""
    try:
        dt_utc = datetime.fromisoformat(published_at_iso.replace("Z", "+00:00"))
        dt_local = dt_utc + timedelta(hours=DISPLAY_TIMEZONE_OFFSET)
        tz = "МСК" if DISPLAY_TIMEZONE_OFFSET == 3 else f"UTC+{DISPLAY_TIMEZONE_OFFSET}"
        return f"{dt_local.strftime('%d.%m.%Y')} · {dt_local.strftime('%H:%M')} {tz}"
    except Exception:
        return ""


def _fetch_videos_from_channel(channel_id: str, days_back: float) -> list:
    raw_videos = _get_channel_videos_raw(channel_id, max_results=50)
    if not raw_videos:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    candidates = []
    for v in raw_videos:
        published = v.get("published_at", "")
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except Exception:
                pass
        if _is_video_processed(v["video_id"]):
            continue
        candidates.append(v)
    if not candidates:
        return []
    durations = _get_video_durations([c["video_id"] for c in candidates])
    filtered = []
    for video in candidates:
        dur = durations.get(video["video_id"], 0)
        if dur == 0:
            print(f"   ⏭  нет длительности: {video['title'][:60]}")
            continue
        if dur < MIN_VIDEO_DURATION_SECONDS:
            print(f"   ⏭  короткое ({_format_duration(dur)}): {video['title'][:60]}")
            continue
        if dur > MAX_VIDEO_DURATION_SECONDS:
            print(f"   ⏭  длинное ({_format_duration(dur)}): {video['title'][:60]}")
            continue
        video["duration_seconds"] = dur
        filtered.append(video)
    return filtered


def get_new_videos_from_channel(channel_id: str) -> list:
    videos = _fetch_videos_from_channel(channel_id, DAYS_BACK)
    if videos:
        print(f"   📅 Окно: последние {HOURS_BACK} ч — найдено {len(videos)}")
    else:
        print(f"   📭 За {HOURS_BACK} ч новых нет")
    return videos


def get_video_info_ytdlp(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            upload_date = info.get("upload_date", "")
            published_at = ""
            if upload_date and len(upload_date) == 8:
                published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
            return {
                "title": info.get("title", ""),
                "duration": int(info.get("duration", 0) or 0),
                "channel": info.get("channel", "") or info.get("uploader", ""),
                "description": info.get("description", ""),
                "published_at": published_at,
            }
    except Exception as e:
        print(f"⚠️  yt-dlp: {e}")
        return {"title": "", "duration": 0, "channel": "", "description": "", "published_at": ""}


def _extract_video_id(url: str) -> str:
    m = re.search(r'(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})', url)
    return m.group(1) if m else ""


def _get_thumbnail_url(video_id: str) -> str:
    if not video_id:
        return ""
    return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"


def _is_youtube_url(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]{11}', text))


def _extract_youtube_url(text: str) -> str:
    m = re.search(r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})', text)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


# ╔══════════════════════════════════════════════════════════╗
# ║              ТРАНСКРИБАЦИЯ                                ║
# ╚══════════════════════════════════════════════════════════╝

def download_audio(url: str, output_path: str = "audio.mp3") -> str:
    base = os.path.splitext(output_path)[0]
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".mp4", ".ogg", ".wav", ".aac", ".flac"):
        for suffix in ("", ".part"):
            f = base + ext + suffix
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    ydl_opts = {
        'format': 'bestaudio[abr<=96]/bestaudio/best',
        'outtmpl': base + ".%(ext)s",
        'quiet': False,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path


def _transcribe_groq(audio_path: str, total_duration: float = 0) -> str:
    file_size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"   📁 Размер: {file_size_mb:.1f} MB")

    chunks = []
    if file_size_mb > GROQ_MAX_SIZE_MB:
        print(f"   ✂️  нарезаю по {GROQ_MAX_CHUNK_SECONDS}с...")
        chunks = _split_audio(audio_path, GROQ_MAX_CHUNK_SECONDS)
        if not chunks:
            raise RuntimeError(
                f"ffmpeg_split_failed: файл {file_size_mb:.1f} MB > "
                f"{GROQ_MAX_SIZE_MB} MB, нарезка не удалась."
            )
        for chunk in chunks:
            size_mb = os.path.getsize(chunk) / 1024 / 1024
            if size_mb > GROQ_MAX_SIZE_MB:
                raise RuntimeError(
                    f"chunk_too_big: {size_mb:.1f} MB > {GROQ_MAX_SIZE_MB} MB "
                    f"({os.path.basename(chunk)})."
                )
    else:
        chunks = [audio_path]

    print(f"   📤 Отправляю {len(chunks)} кусков в Groq...")
    full_parts = []
    for i, chunk in enumerate(chunks, 1):
        chunk_mb = os.path.getsize(chunk) / 1024 / 1024
        print(f"   [{i}/{len(chunks)}] {chunk_mb:.1f} MB...")
        try:
            with open(chunk, "rb") as f:
                r = groq_client.audio.transcriptions.create(
                    file=(os.path.basename(chunk), f),
                    model=GROQ_WHISPER_MODEL,
                    language=VIDEO_LANGUAGE,
                    response_format="text",
                )
            text = (r.text if hasattr(r, "text") else str(r)).strip()
            print(f"   ✅ {len(text)} символов")
            full_parts.append(text)
        except Exception as e:
            print(f"   ❌ Groq chunk {i}: {e}")
            raise
        finally:
            if chunk != audio_path and os.path.exists(chunk):
                try:
                    os.remove(chunk)
                except Exception:
                    pass

    full = " ".join(full_parts)
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except Exception:
            pass
    return full


def _transcribe_local(audio_path: str, total_duration: float = 0) -> str:
    if _local_model is None:
        raise RuntimeError("локальный Whisper не инициализирован")
    segments, _info = _local_model.transcribe(
        audio_path, beam_size=5, language=VIDEO_LANGUAGE, vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=("Это интервью на русском языке. Обсуждается геополитика, "
                        "политика, экономика, выборы, парламент, эксперты."),
    )
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())
    full = " ".join(parts)
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except Exception:
            pass
    return full


def transcribe_audio(audio_path: str, total_duration: float = 0) -> str:
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)
    if WHISPER_MODE == "groq":
        return _transcribe_groq(audio_path, total_duration)
    return _transcribe_local(audio_path, total_duration)


# ─── Определение эксперта ──────────────────────────────

_SURNAME_SUFFIXES = (
    "ов", "ев", "ёв", "ин", "ын", "ский", "цкий", "ская", "цкая",
    "ой", "ый", "ко", "ук", "юк", "ич", "енко", "чук", "дзе", "швили", "ян", "янц",
)

_CAPS_BLACKLIST = {
    "СРОЧНО", "ШОК", "ВАЖНО", "ГЛАВНОЕ", "ИТОГИ", "НОВОСТИ",
    "ПРЯМОЙ", "ЭФИР", "ПОЛНЫЙ", "ТУПИК", "ФРОНТ", "ФРОНТЕ",
    "КИЕВ", "ВОЙНА", "ВОЙНЫ", "МИР", "МИРА", "РОССИЯ", "УКРАИНА",
    "США", "ТРАМП", "ПУТИН", "ЗЕЛЕНСКИЙ", "ЗАЯВЛЕНИЕ", "ВИДЕО",
    "СМОТРЕТЬ", "ЭКСКЛЮЗИВ", "СЕНСАЦИЯ", "ПРЕЗИДЕНТ", "ПРЕЗИДЕНТА",
    "РЕШЕНИЕ", "СОРВАЛОСЬ", "ВСЁ", "ВСЕ", "НАЧАЛО",
    "КОНЕЦ", "ПРАВДА", "ЛОЖЬ", "ТАЙНА", "ТАЙНЫ", "ВЫБОРЫ",
    "РАЗВЯЗКА", "УЛЬТИМАТУМ", "ФИНАЛЬНЫЕ", "ПЕРЕГОВОРЫ",
    "ШОКИРУЮЩАЯ", "ПРЯМО", "СЕЙЧАС", "НОЧЬЮ", "НОЧЬ",
    "РАСХОДНИК", "ДВОЕЧНИК", "КАРТА", "ФРОНТА", "СДЕЛКА",
    "ГОТОВЯТ", "СЛУЧИЛОСЬ", "НОВОЕ", "НОВОСТЬ", "ОБЗОР",
    "СРОЧНЫЙ", "ГЛАВНЫЙ", "МОЛНИЯ", "ПОСЛЕДНИЙ", "ШОКИРУЮЩИЙ",
    "ДУПЛО", "БЕЛОЧКИ", "ЗАМОРОЗКА", "ОТМОРОЗКА", "РАЗБУШЕВ",
    "СДАВАТЬСЯ", "УСКОРИЛ", "ЯРОСТИ", "АГОНИИ",
    "РЕВА", "РЕВ", "РЁВ",
}


def _looks_like_surname(word: str) -> bool:
    if not word:
        return False
    return any(word.lower().endswith(s) for s in _SURNAME_SUFFIXES)


def _sanitize_expert_name(name: str) -> str:
    if not name or name == "Неизвестный эксперт":
        return name
    words = name.split()
    if not words:
        return "Неизвестный эксперт"
    for w in words:
        if w.upper() in _CAPS_BLACKLIST:
            print(f"      🧹 мусор в имени («{name}» → «Неизвестный эксперт»)")
            return "Неизвестный эксперт"
    if len(words) >= 2 and all(w.isupper() for w in words):
        print(f"      🧹 всё CAPS в имени: «{name}»")
        return "Неизвестный эксперт"

    _BAD_SUFFIXES = ("нуть", "тся", "ться", "ешь", "ишь", "уть", "ать", "ять", "ить")
    cleaned = []
    for w in words:
        for suf in _BAD_SUFFIXES:
            if w.lower().endswith(suf) and len(w) > len(suf) + 3:
                w_new = w[:-len(suf)]
                if _looks_like_surname(w_new) or (MORPH_AVAILABLE and _MORPH.parse(w_new)):
                    print(f"      🧹 убираю «{suf}»: {w} → {w_new}")
                    w = w_new
                    break
        cleaned.append(w)
    return " ".join(cleaned)


def _to_nominative(name: str) -> str:
    if not name:
        return name
    if not MORPH_AVAILABLE:
        return name
    result = []
    for p in name.split():
        if len(p) < 3:
            result.append(p)
            continue
        parsed = _MORPH.parse(p)
        if not parsed:
            result.append(p)
            continue
        surn = next((h for h in parsed if 'Surn' in h.tag), None)
        if surn:
            nf = surn.normal_form
            result.append(nf.capitalize() if p[0].isupper() and nf.lower() != p.lower() else p)
            continue
        if p[0].isupper() and _looks_like_surname(p):
            result.append(p)
            continue
        nf = parsed[0].normal_form
        result.append(nf.capitalize() if p[0].isupper() and nf.lower() != p.lower() else p)
    return " ".join(result)


def _ensure_surname_first(name: str) -> str:
    if not name:
        return name
    words = name.split()
    if len(words) != 2:
        return name
    a, b = words
    if _looks_like_surname(b) and not _looks_like_surname(a):
        return f"{b} {a}"
    return name


def _finalize_name(name: str) -> str:
    if not name or name == "Неизвестный эксперт":
        return name
    name = _ensure_surname_first(_to_nominative(name))
    name = _sanitize_expert_name(name)
    key = name.lower().strip()
    if key in KNOWN_EXPERT_FULL and " " not in key:
        expanded = KNOWN_EXPERT_FULL[key]
        print(f"      📖 полное имя: {name} → {expanded}")
        name = expanded
    return name


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip().strip('"').strip("'").strip(".").strip()
    for prefix in ["г-н ", "господин ", "г-жа ", "госпожа ", "д-р ", "доктор "]:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):].strip()
    return re.sub(r'\s+', ' ', name)


def _looks_like_person_name(name: str) -> bool:
    if not name or len(name) < 5 or len(name) > 50:
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    return all(w[:1].isupper() for w in words if w)


def _is_likely_person(name: str) -> bool:
    if not name or len(name) < 5 or len(name) > 50:
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    if not all(w and w[0].isupper() for w in words):
        return False
    if MORPH_AVAILABLE:
        for w in words:
            parsed = _MORPH.parse(w)
            if parsed and any(k in str(parsed[0].tag) for k in ('Name', 'Surn', 'Patr')):
                return True
        for w in words:
            if any(w.lower().endswith(s) for s in _SURNAME_SUFFIXES):
                return True
            if w.lower().endswith(("ым", "им", "ом", "ем")):
                return True
        return False
    return True


def _extract_name_from_description(description: str) -> str:
    if not description:
        return ""
    patterns = [
        r'гость[ья]?\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'в\s+гостях\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'спикер\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'эксперт\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
    ]
    for pat in patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            cand = _normalize_name(m.group(1))
            if _looks_like_person_name(cand):
                return cand
    return ""


def _extract_name_from_title(title: str) -> str:
    if not title:
        return ""
    title = html.unescape(title)

    m = re.search(r'\b(?:с|со)\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2})\b', title)
    if m:
        raw = _normalize_name(m.group(1))
        nom = _to_nominative(raw)
        words = nom.split()
        if not any(w.upper() in _CAPS_BLACKLIST for w in words):
            if len(words) >= 2 and _is_likely_person(nom):
                return nom
            if len(words) == 1:
                if MORPH_AVAILABLE:
                    parsed = _MORPH.parse(words[0])
                    if parsed and 'Surn' in parsed[0].tag:
                        return words[0]
                if words[0].lower() in KNOWN_EXPERT_BIOS or words[0].lower() in KNOWN_EXPERT_FULL:
                    return words[0]

    m = re.search(r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+(?:для|у|в|на)\s+', title)
    if m:
        cand = _normalize_name(m.group(1))
        if _looks_like_person_name(cand):
            return cand

    m = re.search(r'Прогноз\s+([А-ЯЁ][а-яё]+)(?:\s|$|[,.!?])', title)
    if m:
        s = m.group(1)
        if s.endswith("и") or s.endswith("ы"):
            s = s[:-1] + "а"
        elif s.endswith("а"):
            s = s[:-1]
        return s

    for mm in re.finditer(r'\|\s*([А-ЯЁ]{3,}(?:\s+[А-ЯЁ][а-яё]+)?)\s*[-—:]\s', title):
        cc = mm.group(1).strip()
        if cc.split()[0] in _CAPS_BLACKLIST:
            continue
        cand = cc.title()
        if 3 <= len(cand) <= 40:
            print(f"      (CAPS: {cc} → {cand})")
            return cand

    for pat in [r'/\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)\s*$',
                r'\s-\s([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)\s*$']:
        m = re.search(pat, title)
        if m:
            cand = _normalize_name(m.group(1))
            if 1 <= len(cand.split()) <= 2 and cand and cand[0].isupper():
                return cand

    m = re.search(r'\|\s*([А-ЯЁ][а-яё]+)\s*-\s', title)
    if m:
        cand = _normalize_name(m.group(1))
        if 3 <= len(cand) <= 30:
            return cand

    clean = re.sub(r'[🍀🔥💥⚡️🎯📌]', '', title).strip()
    for sep in ["|", "—", " - ", ":"]:
        if sep in clean:
            cand = _normalize_name(clean.split(sep)[-1])
            if _looks_like_person_name(cand):
                return cand
    return ""


def _expand_surname_in_transcript(surname: str, transcript: str) -> str:
    if not surname or not transcript:
        return surname
    if len(surname.split()) > 1:
        return surname
    window = transcript[:5000]
    variants = [surname]
    if surname.title() != surname:
        variants.append(surname.title())
    if surname.upper() != surname:
        variants.append(surname.upper())
    for v in variants:
        for pat in [rf'([А-ЯЁ][а-яё]+)\s+{re.escape(v)}', rf'{re.escape(v)}\s+([А-ЯЁ][а-яё]+)']:
            m = re.search(pat, window)
            if m:
                part = m.group(1)
                if not _looks_like_surname(part) and 3 <= len(part) <= 25:
                    res = _to_nominative(f"{v.title()} {part}")
                    print(f"      (расширил: {surname} → {res})")
                    return res
    return surname.title() if surname.isupper() else surname


def _extract_name_from_intro(transcript: str) -> str:
    intro = transcript[:3000]
    patterns = [
        r'в гостях\s+(?:у нас\s+)?(?:был[аи]?|стал[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'(?:наш|мой)\s+гост[ья]\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'на\s+связи\s+(?:был[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'(?:в\s+эфире|в\s+студии)\s+(?:был[аи]?)?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
        r'([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+),?\s+(?:политолог|политик|экономист|журналист|эксперт|аналитик)',
        r'мой\s+собеседник\s*[-—:]?\s*([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2})',
    ]
    for pat in patterns:
        m = re.search(pat, intro)
        if m:
            cand = _normalize_name(m.group(1))
            if _looks_like_person_name(cand):
                return cand
    return ""


def _is_owner_name(name: str, channel_owner: str) -> bool:
    if not name or not channel_owner:
        return False

    def _norm(s: str) -> str:
        return re.sub(r'[.\s]+', ' ', s.lower()).strip()

    n1 = _norm(name)
    n2 = _norm(channel_owner)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    w1 = n1.split()
    w2 = n2.split()
    if not w1 or not w2:
        return False
    if sorted(w1) == sorted(w2):
        return True
    if len(w1) == 2 and len(w2) == 2:
        for short, full in [(w1, w2), (w2, w1)]:
            a, b = short
            for ini, fam in [(a, b), (b, a)]:
                if len(ini) == 1 and len(fam) > 2:
                    for i, w in enumerate(full):
                        if w == fam:
                            other = full[1 - i] if 0 <= (1 - i) < len(full) else ""
                            if other and other[0] == ini:
                                return True
    for wa in w1:
        if len(wa) < 4:
            continue
        for wb in w2:
            if wa == wb and _looks_like_surname(wa):
                return True
    return False


def _llm_detect_expert(video_title, transcript, channel_name, channel_type, channel_owner):
    if channel_type == "author":
        hint = (f"Это АВТОРСКИЙ канал «{channel_name}». Владелец — {channel_owner} — "
                f"ВЕДУЩИЙ канала. Эксперт — ГОСТЬ. НЕ возвращай имя ведущего.")
    elif channel_type == "personal":
        hint = (f"Это ПЕРСОНАЛЬНЫЙ канал «{channel_name}». Владелец — {channel_owner}. "
                f"Если гость не назван, эксперт — владелец.")
    else:
        hint = f"Это МЕДИЙНЫЙ канал «{channel_name}». Эксперт — ГОСТЬ, а НЕ ведущий."

    prompt = f"""Определи ГЛАВНОГО ЭКСПЕРТА (гостя) в этом видео.

КОНТЕКСТ:
- Канал: «{channel_name}»
- Тип: {channel_type}
- {hint}
- Владелец канала: {channel_owner or '(нет)'}
- Заголовок: {video_title or "(без названия)"}

ПРИМЕРЫ:

Пример 1 (author, гость назван):
Канал: «Александр Шелест», владелец: Александр Шелест
Транскрипт: «У нас в гостях Дмитрий Спивак, политолог…»
Ответ: Спивак Дмитрий (НЕ «Шелест Александр» — он ведущий)

Пример 2 (media, гость на связи):
Канал: «UKRLIFETV», тип: media
Транскрипт: «Сегодня у нас на связи Руслан Бизяев…»
Ответ: Бизяев Руслан

Пример 3 (гость не назван):
Ответ: НЕИЗВЕСТНО

ПРАВИЛА:
1. Эксперт — тот, кто даёт интервью, НЕ ведущий.
2. Ищи «в гостях», «на связи», «наш гость», «в студии».
3. Или имя в заголовке после «|», «—», «:», «для», «с».
4. Верни ТОЛЬКО «Имя Фамилия» или «Фамилия Имя» в ИМЕНИТЕЛЬНОМ падеже.
5. Не определить — верни РОВНО: НЕИЗВЕСТНО

Начало транскрипта:
---
{transcript[:8000]}
---

Имя эксперта:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.1, max_tokens=500,
        )
        name = _normalize_name(r.choices[0].message.content)
        if not name or name.upper() == "НЕИЗВЕСТНО" or len(name) > 60 or "\n" in name:
            return ""
        if name.lower() in ("эксперт", "гость", "неизвестно"):
            return ""
        return name
    except Exception as e:
        print(f"⚠️  LLM: {e}")
        return ""


def detect_expert_name(video_title, transcript, description="",
                       channel_name="", channel_type="media", channel_owner=""):
    print("   Шаг 1: интро...")
    name = _extract_name_from_intro(transcript)
    if name:
        name = _finalize_name(name)
        if channel_type == "author" and _is_owner_name(name, channel_owner):
            print(f"      ⚠️  интро дало имя ведущего «{name}» — пропускаю")
        else:
            print(f"      ✅ {name}")
            return name
    print("      ⚠️  нет")

    print("   Шаг 2: заголовок...")
    name = _extract_name_from_title(video_title)
    if name:
        name = _expand_surname_in_transcript(name, transcript)
        name = _finalize_name(name)
        if channel_type == "author" and _is_owner_name(name, channel_owner):
            print(f"      ⚠️  заголовок дал имя ведущего «{name}» — пропускаю")
        else:
            print(f"      ✅ {name}")
            return name
    print("      ⚠️  нет")

    if description:
        print("   Шаг 3: описание...")
        name = _extract_name_from_description(description)
        if name:
            name = _finalize_name(name)
            if channel_type == "author" and _is_owner_name(name, channel_owner):
                print(f"      ⚠️  описание дало имя ведущего «{name}» — пропускаю")
            else:
                print(f"      ✅ {name}")
                return name
        print("      ⚠️  нет")

    print("   Шаг 4: LLM...")
    name = _llm_detect_expert(video_title, transcript, channel_name, channel_type, channel_owner)
    if name:
        name = _finalize_name(name)
        if channel_type == "author" and _is_owner_name(name, channel_owner):
            print(f"      ⚠️  LLM вернула имя ведущего «{name}» — пропускаю")
        else:
            print(f"      ✅ {name}")
            return name
    print("      ⚠️  нет")

    if channel_type == "personal" and channel_owner:
        on = _finalize_name(channel_owner)
        print(f"   Шаг 5: fallback владельца: {on}")
        return on

    print("   ⚠️  не определено")
    return "Неизвестный эксперт"


def generate_expert_bio(expert_name, transcript):
    if not expert_name or expert_name == "Неизвестный эксперт":
        return ""
    key = expert_name.lower().strip()
    if key in KNOWN_EXPERT_BIOS:
        return KNOWN_EXPERT_BIOS[key]
    fw = key.split()[0] if key.split() else ""
    if fw in KNOWN_EXPERT_BIOS:
        return KNOWN_EXPERT_BIOS[fw]
    return ""


def extract_predictions(transcript, expert_name):
    if not EXTRACT_PREDICTIONS:
        return []
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime('%d.%m.%Y')
    current_year = now_utc.year
    max_year = current_year + MAX_PREDICTION_HORIZON_YEARS
    prompt = f"""Найди ПРОВЕРЯЕМЫЕ ПРОГНОЗЫ в интервью «{expert_name}».

СЕГОДНЯ: {today_str}. Текущий год: {current_year}.

ПРОГНОЗ — это КОНКРЕТНОЕ утверждение о БУДУЩЕМ, которое можно проверить:
- Есть событие (что именно произойдёт)
- Есть срок/дата (когда)
- Есть проверяемый исход (да/нет, случилось/не случилось)

НЕ ПРОГНОЗ:
- Общие рассуждения («ситуация будет ухудшаться»)
- Оценки прошлого
- Формулировки без конкретики («произойдёт событие, о котором говорится»)
- Прогнозы без срока

ПЛОХИЕ примеры (НЕ включай):
- «До 27 октября 2026 года произойдёт событие, о котором говорится в интервью»
- «Ситуация изменится в ближайшее время»
- «Эксперт считает, что всё будет плохо»

ХОРОШИЕ примеры (включай):
- «В ноябре 2026 года в Абу-Даби пройдут переговоры»
- «Если война продлится 2 года, конфликт закончится до 2029 года»
- «Трамп подпишет указ до конца декабря 2026 года»

ВАЖНО:
- Не используй годы РАНЬШЕ {current_year}
- Горизонт > {MAX_PREDICTION_HORIZON_YEARS} лет — не включай
- Максимум {MAX_PREDICTIONS_PER_VIDEO} прогнозов

JSON-массив строк:
["прогноз 1", "прогноз 2"]

Нет — [].

Текст:
---
{transcript[:15000]}
---

JSON:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.2, max_tokens=4000,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        if not raw:
            return []
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return []
        try:
            preds = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(preds, list):
            return []
        cleaned = []
        for p in preds:
            if not isinstance(p, str):
                continue
            p = p.strip().strip('"').strip("'").strip("•").strip("-").strip()
            if not (15 <= len(p) <= 400):
                continue
            if "о котором говорится в интервью" in p.lower():
                continue
            years_in_pred = re.findall(r'\b(20\d{2})\b', p)
            if any(int(y) < current_year for y in years_in_pred):
                continue
            if any(int(y) > max_year for y in years_in_pred):
                continue
            cleaned.append(p)
        return cleaned[:MAX_PREDICTIONS_PER_VIDEO]
    except Exception as e:
        print(f"⚠️  прогнозы: {e}")
        return []


def _is_title_neutral(title):
    if not title:
        return True
    if title.isupper():
        return False
    if any(c in title for c in "!🎯🍀🔥💥⚡📌❗❕"):
        return False
    if len(title) > MAX_TITLE_LEN:
        return False
    words = title.split()
    if words:
        caps_count = sum(1 for w in words if len(w) > 2 and w.isupper())
        if caps_count / len(words) > 0.3:
            return False
    return True


def _simple_neutralize(title):
    if not title:
        return ""
    c = re.sub(r'[🍀🔥💥⚡️🎯📌❗❕!]', '', title)
    c = re.sub(r'\s+', ' ', c).strip()
    words = c.split()
    new_words = [w.capitalize() if len(w) > 2 and w.isupper() else w for w in words]
    c = " ".join(new_words)
    if len(c) > MAX_TITLE_LEN:
        c = c[:MAX_TITLE_LEN].rsplit(' ', 1)[0] + "…"
    return c


def neutralize_title(video_title):
    if not video_title:
        return ""
    if _is_title_neutral(video_title):
        return video_title
    prompt = f"""Перепиши заголовок нейтрально (до 80 символов). Без CAPS, «!», эмодзи, кликбейта.

Оригинал: {video_title}

Нейтральный:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.4, max_tokens=500,
        )
        new = r.choices[0].message.content.strip().strip('"').strip("'").strip()
        if not new or len(new) < 5 or new.lower() == video_title.lower() or new.isupper():
            return _simple_neutralize(video_title)
        if not _is_title_neutral(new):
            return _simple_neutralize(video_title)
        return new
    except Exception:
        return _simple_neutralize(video_title)


def generate_hashtags(expert_name, summary):
    prompt = f"""3 хештега (#Слово через пробел).

Эксперт: {expert_name}
Текст: {summary[:600]}

Хештеги:"""
    try:
        r = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=GROQ_MODEL, temperature=0.4, max_tokens=300,
        )
        tags = r.choices[0].message.content.strip()
        valid = [t for t in tags.split() if t.startswith("#") and len(t) > 2]
        return " ".join(valid[:3])
    except Exception:
        return ""


def _call_groq_with_retry(messages, temperature, max_tokens, label=""):
    for attempt in range(GROQ_MAX_RETRIES):
        try:
            return groq_client.chat.completions.create(
                messages=messages, model=GROQ_MODEL,
                temperature=temperature, max_tokens=max_tokens,
            )
        except Exception as e:
            err = str(e).lower()
            if ("429" in err or "rate_limit" in err or "rate limit" in err) and attempt < GROQ_MAX_RETRIES - 1:
                wait = 30 * (2 ** attempt)
                m = re.search(r'try again in ([\d.]+)s', str(e))
                if m:
                    wait = max(float(m.group(1)) + 2, 5)
                print(f"      ⏸  429 [{label}], {wait:.0f}с")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Groq: 429")


def _summarize_chunk(text, is_final):
    if is_final:
        prompt = f"""Аналитическая выжимка интервью на русском.

ПРАВИЛА:
1. Игнорируй рубрики/заставки.
2. Исправляй ошибки распознавания.
3. Атрибуция: «по мнению эксперта» (не более 2 раз).
4. Не приписывай страны/имена, которых нет.
5. Без эмодзи, без * и - — только •.

Структура:

<b>ГЛАВНЫЙ ТЕЗИС</b>
(1-2 предложения)

<b>АРГУМЕНТЫ И ФАКТЫ</b>
• (3-5)

<b>ПРОГНОЗЫ И ОЦЕНКИ</b>
• (если есть)

Текст:
---
{text}
---

Выжимка:"""
        max_tokens = 4000
    else:
        prompt = f"""Тезисы, аргументы, прогнозы. Кратко.
Атрибуция: «эксперт утверждает». Списки — •.

---
{text}
---

Выжимка:"""
        max_tokens = 1000

    c = _call_groq_with_retry(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5, max_tokens=max_tokens, label="summarize",
    )
    return c.choices[0].message.content.strip()


def summarize_text(text):
    if not text.strip():
        raise ValueError("пустой текст")
    CHUNK = 12000
    if len(text) <= CHUNK:
        return _summarize_chunk(text, is_final=True)
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    print(f"📄 Map-Reduce: {len(chunks)} частей")
    partials = []
    for i, c in enumerate(chunks, 1):
        print(f"   {i}/{len(chunks)}...")
        partials.append(_summarize_chunk(c, is_final=False))
        if i < len(chunks):
            print(f"      ⏸  {GROQ_DELAY_BETWEEN_CHUNKS}с...")
            time.sleep(GROQ_DELAY_BETWEEN_CHUNKS)
    return _summarize_chunk("\n\n---\n\n".join(partials), is_final=True)


def _check_context_errors(summary, original):
    ukr = ["рада", "зеленск", "киев", "украин", "київ"]
    rus = ["дума", "путин", "кремл", "россий", "рф "]
    tl, sl = original.lower(), summary.lower()
    is_ukr = any(m in tl for m in ukr)
    is_rus = any(m in tl for m in rus)
    if is_ukr and not is_rus and "росси" in sl:
        print("⚠️  подмена: Украина→Россия")
    if is_rus and not is_ukr and "украин" in sl:
        print("⚠️  подмена: Россия→Украина")
    return summary


def _format_title(title, max_len=MAX_TITLE_LEN):
    if not title:
        return ""
    title = html.unescape(title)
    if len(title) > max_len:
        title = title[:max_len - 1] + "…"
    return f"<b>{title.upper()}</b>"


# ╔══════════════════════════════════════════════════════════╗
# ║              ПУБЛИКАЦИЯ                                  ║
# ╚══════════════════════════════════════════════════════════╝

async def send_expert_post(video_url, video_title, expert_name, expert_bio,
                            channel_name, summary, published_at=""):
    video_id = _extract_video_id(video_url)
    date_str = _format_published_date(published_at)

    parts = []
    if video_title:
        parts.append(_format_title(video_title))
    el = f"<b>ЭКСПЕРТ</b>\n{expert_name}"
    if expert_bio:
        el += f" — {expert_bio}"
    parts.append(el)
    if channel_name and channel_name.strip().lower() != expert_name.strip().lower():
        parts.append(f"<b>КАНАЛ</b>\n{channel_name}")
    if date_str:
        parts.append(f"<b>ОПУБЛИКОВАНО</b>\n{date_str}")
    parts.append("<i>Выжимка ниже ↓</i>")
    caption = "\n\n".join(parts)
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    sent = False
    url = _get_thumbnail_url(video_id)
    if url:
        try:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=url,
                                 caption=caption, parse_mode=ParseMode.HTML)
            print("✅ Фото")
            sent = True
        except Exception as e:
            print(f"⚠️  Фото: {e}")
    if not sent:
        await bot.send_message(chat_id=CHANNEL_ID, text=caption)

    hashtags = generate_hashtags(expert_name, summary) if GENERATE_HASHTAGS else ""
    text = f"{summary}\n\n🔗 <a href='{video_url}'>Смотреть оригинал</a>\n"
    if hashtags:
        text += f"\n{hashtags}\n"
    text += f"\n<i>Сгенерировано автоматически.</i>"

    if len(text) <= TELEGRAM_MAX_LEN:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)
        print("✅ Выжимка")
    else:
        print(f"✂️  Текст {len(text)} > {TELEGRAM_MAX_LEN}, разбиваю...")
        parts = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 <= TELEGRAM_MAX_LEN:
                current = (current + "\n\n" + paragraph).strip() if current else paragraph
            else:
                if current:
                    parts.append(current)
                if len(paragraph) > TELEGRAM_MAX_LEN:
                    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
                    buf = ""
                    for s in sentences:
                        if len(buf) + len(s) + 1 <= TELEGRAM_MAX_LEN:
                            buf = (buf + " " + s).strip() if buf else s
                        else:
                            if buf:
                                parts.append(buf)
                            buf = s[:TELEGRAM_MAX_LEN]
                    if buf:
                        parts.append(buf)
                else:
                    current = paragraph
        if current:
            parts.append(current)

        for i, part in enumerate(parts, 1):
            header = f"<i>(часть {i}/{len(parts)})</i>\n\n" if len(parts) > 1 else ""
            await bot.send_message(chat_id=CHANNEL_ID, text=header + part)
            print(f"✅ Выжимка {i}/{len(parts)}")
            await asyncio.sleep(1)


async def process_and_publish(video_url, video_title="", video_description="",
                               video_duration=0, video_published_at="",
                               channel_name="", channel_type="media",
                               channel_owner="", skip_duration_check=False) -> tuple:
    print(f"\n{'=' * 60}")
    print(f"ОБРАБОТКА: {video_url}")
    if video_title:
        print(f"  Заголовок: {video_title[:80]}")
    if channel_name:
        print(f"  Канал: {channel_name} [{channel_type}]")
    print(f"{'=' * 60}\n")

    audio_file = None
    try:
        duration = video_duration
        published_at = video_published_at

        if not video_title:
            print("🔍 yt-dlp...")
            info = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            video_title = info.get("title", "")
            video_description = info.get("description", "")
            duration = info.get("duration", 0)
            if not published_at:
                published_at = info.get("published_at", "")
            if not channel_name:
                channel_name = info.get("channel", "")
            print(f"  Длительность: {_format_duration(duration)}\n")

            if not skip_duration_check and duration > 0:
                if duration < MIN_VIDEO_DURATION_SECONDS:
                    return False, f"too_short: {duration}s"
                if duration > MAX_VIDEO_DURATION_SECONDS:
                    return False, f"too_long: {duration}s"

        if not published_at:
            print("📅 Дата не задана — загружаю...")
            extra = await asyncio.to_thread(get_video_info_ytdlp, video_url)
            published_at = extra.get("published_at", "")

        print("📥 Шаг 1/4: Скачивание...")
        _log_memory("до скачивания")
        audio_file = await asyncio.to_thread(download_audio, video_url)
        print("✅ Скачано\n")
        _log_memory("после скачивания")

        print("🎤 Шаг 2/4: Транскрибация...")
        transcript = await asyncio.to_thread(transcribe_audio, audio_file, duration)
        audio_file = None

        transcript_len = len(transcript.strip())
        print(f"📊 Длина транскрипта: {transcript_len}")
        print(f"📊 Начало: {transcript[:200]!r}")

        if transcript_len < MIN_TRANSCRIPT_CHARS:
            return False, f"transcript_too_short: {transcript_len}"

        print(f"✅ ({transcript_len})\n")

        if DIAGNOSTIC_MODE:
            print("🔧 ДИАГНОСТИКА: транскрибация прошла")
            return True, f"diagnostic_ok: {transcript_len}"

        display_title = video_title
        if NEUTRALIZE_TITLE and video_title:
            print("🎨 Нейтрализация...")
            n = neutralize_title(video_title)
            if n and n != video_title:
                display_title = n
                print(f"✅ {display_title}\n")

        print("🔍 Эксперт...")
        expert_name = detect_expert_name(
            video_title, transcript, description=video_description,
            channel_name=channel_name, channel_type=channel_type,
            channel_owner=channel_owner,
        )
        print(f"✅ {expert_name}\n")

        print("📝 Специализация...")
        expert_bio = generate_expert_bio(expert_name, transcript)
        if expert_bio:
            print(f"✅ {expert_bio}\n")
        else:
            print("⚠️  нет\n")

        print("🧠 Шаг 3/4: Суммаризация...")
        summary = summarize_text(transcript)
        summary = _check_context_errors(summary, transcript)
        print("✅ Готово\n")

        print("📤 Шаг 4/4: Отправка...")
        await send_expert_post(
            video_url=video_url, video_title=display_title,
            expert_name=expert_name, expert_bio=expert_bio,
            channel_name=channel_name, summary=summary,
            published_at=published_at,
        )
        print("✅ Опубликовано\n")

        vid = _extract_video_id(video_url)
        _save_summary(vid, expert_name, channel_name, summary,
                      video_title=display_title, video_published_at=published_at)
        _mark_expert_seen_today(expert_name)

        if EXTRACT_PREDICTIONS:
            try:
                print("📊 Прогнозы...")
                preds = extract_predictions(transcript, expert_name)
                if preds:
                    saved = _save_predictions(vid, expert_name, channel_name, preds)
                    print(f"✅ Сохранено {saved}")
                    for p in preds:
                        print(f"   • {p[:120]}")
                else:
                    print("   нет")
            except Exception as e:
                print(f"⚠️  прогнозы: {e}")

        return True, ""

    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)[:500]
        print(f"\n❌ {err_type}: {err_msg}")
        traceback.print_exc()
        sys.stderr.flush()
        _log_memory("в except")
        if audio_file and os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
        return False, f"{err_type}: {err_msg}"


# ╔══════════════════════════════════════════════════════════╗
# ║              ВОРКЕР                                       ║
# ╚══════════════════════════════════════════════════════════╝

async def _worker_loop():
    print("🔧 Воркер запущен")
    while _SHUTDOWN_EVENT is None or not _SHUTDOWN_EVENT.is_set():
        try:
            task = _get_next_pending_task()
            if not task:
                await asyncio.sleep(WORKER_POLL_INTERVAL)
                continue

            tid = task['id']
            url = task['video_url']
            attempts_before = task['attempts'] or 0
            attempts_now = attempts_before + 1
            print(f"\n{'▓' * 60}")
            print(f"🔧 ЗАДАЧА #{tid} ({task['source']}, P{task['priority']}, "
                  f"attempts {attempts_before}→{attempts_now}): {url}")
            print(f"{'▓' * 60}")

            _mark_task_processing(tid)
            try:
                info = {}
                if not task['video_title'] or not task['channel_name']:
                    info = await asyncio.to_thread(get_video_info_ytdlp, url)

                video_title = task['video_title'] or info.get('title', '')
                video_published_at = (
                    task.get('video_published_at') or info.get('published_at', '')
                )
                channel_name = task['channel_name'] or info.get('channel', '')
                channel_type = task['channel_type'] or 'media'
                channel_owner = task['channel_owner'] or ''

                cn_low = (channel_name or '').lower().strip()
                if cn_low in ('виталий дикий', 'виталий дикий live'):
                    channel_name = "Дикий live"

                if channel_name:
                    cn_low = channel_name.lower().strip()
                    matched = False
                    for ch in CHANNELS_TO_WATCH:
                        ch_name_low = ch['name'].lower().strip()
                        ch_owner_low = (ch.get('owner', '') or '').lower().strip()
                        if (cn_low == ch_name_low
                                or cn_low in ch_name_low
                                or ch_name_low in cn_low
                                or (ch_owner_low and cn_low == ch_owner_low)):
                            channel_name = ch['name']
                            channel_type = ch.get('type', 'media')
                            channel_owner = ch.get('owner', '')
                            matched = True
                            break
                    if not matched:
                        channel_type = "media"
                        channel_owner = ""

                ok, reason = await process_and_publish(
                    url,
                    video_title=video_title,
                    video_description=info.get('description', ''),
                    video_duration=info.get('duration', 0),
                    video_published_at=video_published_at,
                    channel_name=channel_name,
                    channel_type=channel_type,
                    channel_owner=channel_owner,
                    skip_duration_check=(task['source'] == 'telegram'),
                )

                if ok:
                    _mark_task_done(tid)
                    vid = task['video_id'] or _extract_video_id(url)
                    if vid:
                        _mark_video_processed(vid)
                    print(f"✅ #{tid} OK")
                else:
                    _handle_task_failure(tid, reason or "unknown", attempts_now)

            except Exception as e:
                err_text = f"{type(e).__name__}: {e}"
                print(f"❌ #{tid}: {err_text}")
                traceback.print_exc()
                sys.stderr.flush()
                _handle_task_failure(tid, err_text, attempts_now)

            try:
                import gc
                gc.collect()
            except Exception:
                pass

        except Exception as e:
            print(f"⚠️  воркер: {e}")
            traceback.print_exc()
            sys.stderr.flush()
            await asyncio.sleep(WORKER_POLL_INTERVAL)

    print("🛑 Воркер завершён")


# ╔══════════════════════════════════════════════════════════╗
# ║              АВТОЦИКЛ                                     ║
# ╚══════════════════════════════════════════════════════════╝

async def check_all_channels():
    if not CHANNELS_TO_WATCH:
        return
    print(f"\n{'=' * 60}")
    print(f"АВТОПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 60}")

    try:
        _cleanup_old_experts(days_keep=30)
        _cleanup_done_tasks(days_keep=3)
    except Exception:
        pass

    channels_shuffled = CHANNELS_TO_WATCH.copy()
    random.shuffle(channels_shuffled)

    seen_experts = _get_experts_seen_today()
    if seen_experts:
        print(f"\n👥 Уже выступали сегодня ({len(seen_experts)}):")
        for e in sorted(seen_experts):
            print(f"   • {e}")

    repeat_count = {}
    added = 0
    skipped = 0

    for ch in channels_shuffled:
        if _SHUTDOWN_EVENT is not None and _SHUTDOWN_EVENT.is_set():
            print("⏹ Shutdown — прерываю автоцикл")
            break
        print(f"\n📺 {ch['name']}")
        try:
            videos = get_new_videos_from_channel(ch["id"])
        except Exception as e:
            print(f"   ❌ {e}")
            continue
        if not videos:
            print("   новых нет")
            continue
        if len(videos) > MAX_VIDEOS_PER_CHANNEL_PER_CHECK:
            print(f"   ⚠️  ограничиваю до {MAX_VIDEOS_PER_CHANNEL_PER_CHECK}")
            videos = videos[:MAX_VIDEOS_PER_CHANNEL_PER_CHECK]

        for v in videos:
            prelim = _extract_name_from_title(v["title"])
            if not prelim:
                prelim = _extract_name_from_description(v.get("description", ""))
            if prelim:
                prelim = _finalize_name(prelim)

            owner = ch.get("owner", "") or ""
            if ch.get("type") == "author" and owner and prelim:
                if _is_owner_name(prelim, owner):
                    prelim = ""

            is_rep = prelim and prelim in seen_experts
            prio = 0 if is_rep else 5

            if is_rep:
                cnt = repeat_count.get(prelim, 0)
                if cnt >= MAX_REPEAT_EXPERTS_PER_CYCLE:
                    print(f"   ⏭  лимит ({prelim}): {v['title'][:50]}")
                    _mark_video_processed(v["video_id"])
                    skipped += 1
                    continue
                repeat_count[prelim] = cnt + 1

            tid = _add_task_to_queue(
                video_url=v["url"], video_id=v["video_id"],
                video_title=v["title"],
                video_published_at=v.get("published_at", ""),
                channel_name=ch["name"],
                channel_type=ch.get("type", "media"),
                channel_owner=ch.get("owner", ""),
                priority=prio, source="auto",
            )
            if tid:
                tag = "🔁" if is_rep else "🆕"
                print(f"   {tag} #{tid}: {v['title'][:60]}")
                added += 1
            else:
                print(f"   ⏭  в очереди: {v['title'][:60]}")

    q = _quota_info()
    print(f"\n📊 Автоцикл: +{added}, пропущено {skipped}")
    print(f"📊 Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA} ({q['by_op']})")
    print(f"{'=' * 60}")


async def _heartbeat_job():
    try:
        s = _queue_stats()
        q = _quota_info()
        text = (f"💚 <b>Heartbeat</b>\n\n"
                f"⏳ {s.get('pending', 0)} | 🔄 {s.get('processing', 0)} | "
                f"✅ {s.get('done', 0)} | ❌ {s.get('error', 0)}\n"
                f"📊 Квота: {q['count']}/{YOUTUBE_DAILY_QUOTA}")
        if PSUTIL_AVAILABLE:
            try:
                mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                text += f"\n💾 RAM: {mem_mb:.0f} MB"
            except Exception:
                pass
        await bot.send_message(chat_id=OWNER_USER_ID, text=text)
    except Exception as e:
        print(f"⚠️  heartbeat: {e}")


# ╔══════════════════════════════════════════════════════════╗
# ║              TELEGRAM HANDLERS                            ║
# ╚══════════════════════════════════════════════════════════╝

def _is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_USER_ID


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    if not _is_owner(message):
        return
    mode = "🔧 ДИАГНОСТИКА" if DIAGNOSTIC_MODE else "📦 ОБЫЧНЫЙ"
    await message.answer(
        f"<b>ГЕОПОЛИТРУК</b>\n"
        f"Режим: {mode}\n"
        f"Модель: {GROQ_MODEL}\n\n"
        "<b>Команды:</b>\n"
        "/queue — очередь\n"
        "/stats — статистика\n"
        "/quota — квота YouTube\n"
        "/errors — ошибки\n"
        "/predictions — прогнозы\n"
        "/reset — сброс очереди\n"
        "/cancel &lt;id&gt; — отмена\n"
        "/priority — рейтинг\n"
        "/experts_today — кто выступал"
    )


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if not _is_owner(message):
        return
    tasks = _queue_list(20)
    if not tasks:
        await message.answer("📭 Очередь пуста")
        return
    lines = ["<b>📋 Очередь:</b>\n"]
    for t in tasks:
        ic = "🔄" if t['status'] == 'processing' else "⏳"
        src = "👤" if t['source'] == 'telegram' else "🤖"
        retry = ""
        if t.get('next_retry_at'):
            retry = f" [retry {t['next_retry_at']}]"
        lines.append(f"{ic}{src} #{t['id']} [P{t['priority']}] "
                     f"(попыток: {t['attempts']}){retry} "
                     f"{(t['video_title'] or t['video_url'])[:50]}")
    await message.answer("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_owner(message):
        return
    s = _queue_stats()
    q = _quota_info()
    mode = "🔧" if DIAGNOSTIC_MODE else "📦"
    text = (f"<b>📊 Статистика</b> ({mode})\n\n"
            f"⏳ Pending: {s.get('pending', 0)}\n"
            f"🔄 Processing: {s.get('processing', 0)}\n"
            f"✅ Done: {s.get('done', 0)}\n"
            f"❌ Error: {s.get('error', 0)}\n\n"
            f"📊 Квота YouTube: {q['count']}/{YOUTUBE_DAILY_QUOTA}")
    if PSUTIL_AVAILABLE:
        try:
            mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            text += f"\n💾 RAM: {mem_mb:.0f} MB"
        except Exception:
            pass
    await message.answer(text)


@dp.message(Command("quota"))
async def cmd_quota(message: Message):
    if not _is_owner(message):
        return
    q = _quota_info()
    ops = "\n".join(f"  • {op}: {n}" for op, n in q["by_op"].items()) or "  (нет)"
    remaining = YOUTUBE_DAILY_QUOTA - q["count"]
    pct = round(q["count"] / YOUTUBE_DAILY_QUOTA * 100, 2)
    await message.answer(
        f"<b>📊 Квота YouTube</b>\n\n"
        f"Дата (UTC): {q['date']}\n"
        f"Использовано: <b>{q['count']}</b> / {YOUTUBE_DAILY_QUOTA} ({pct}%)\n"
        f"Осталось: {remaining}\n\n"
        f"<b>По операциям:</b>\n{ops}"
    )


@dp.message(Command("errors"))
async def cmd_errors(message: Message):
    if not _is_owner(message):
        return
    errors = _recent_errors(10)
    if not errors:
        await message.answer("✅ Ошибок нет")
        return
    lines = ["<b>❌ Последние ошибки:</b>\n"]
    for e in errors:
        title = (e['video_title'] or e['video_url'])[:50]
        msg = (e['error_message'] or "—")[:400]
        lines.append(f"<b>#{e['id']}</b> (попыток: {e['attempts']})\n"
                     f"  {title}\n"
                     f"  <code>{html.escape(msg)}</code>\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"
    await message.answer(text)


@dp.message(Command("predictions"))
async def cmd_predictions(message: Message):
    if not _is_owner(message):
        return
    preds = _recent_predictions(15)
    if not preds:
        await message.answer("📭 Прогнозов пока нет")
        return
    lines = ["<b>🔮 Последние прогнозы:</b>\n"]
    for p in preds:
        icon = {"confirmed": "✅", "refuted": "❌",
                "unverifiable": "❔"}.get(p.get("verdict"), "⏳")
        lines.append(f"{icon} <b>#{p['id']}</b> — {p['expert_name']}\n"
                     f"  {(p['prediction_text'] or '')[:200]}\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "…"
    await message.answer(text)


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if not _is_owner(message):
        return
    n = _reset_queue()
    await message.answer(f"🛑 Сброшено в error: <b>{n}</b> задач")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if not _is_owner(message):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /cancel <id>")
        return
    try:
        tid = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом")
        return
    ok, msg = _cancel_task(tid)
    await message.answer(("✅ " if ok else "❌ ") + f"#{tid}: {msg}")


@dp.message(Command("priority"))
async def cmd_priority(message: Message):
    if not _is_owner(message):
        return
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT expert_name, "
        "SUM(CASE WHEN verdict='confirmed' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN verdict='refuted' THEN 1 ELSE 0 END) "
        "FROM predictions WHERE verdict IN ('confirmed','refuted') "
        "GROUP BY expert_name"
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Пока нет проверенных прогнозов")
        return
    rated = []
    for exp, c, r in rows:
        c = c or 0
        r = r or 0
        if c + r > 0:
            rated.append((exp, c, r, round(c / (c + r) * 100, 1)))
    rated.sort(key=lambda x: -x[3])
    lines = ["<b>🏆 Рейтинг:</b>\n"]
    for i, (e, c, r, acc) in enumerate(rated[:15], 1):
        lines.append(f"{i}. {e}: {acc}% (✅{c}/❌{r})")
    await message.answer("\n".join(lines))


@dp.message(Command("experts_today"))
async def cmd_experts_today(message: Message):
    if not _is_owner(message):
        return
    exp = _get_experts_seen_today()
    if not exp:
        await message.answer("Сегодня никто не выступал")
        return
    lines = [f"<b>👥 Сегодня ({_get_today_msk()}):</b>\n"]
    for e in sorted(exp):
        lines.append(f"• {e} — {get_expert_priority_rating(e)}%")
    await message.answer("\n".join(lines))


@dp.message(F.text)
async def on_text(message: Message):
    if not _is_owner(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    if not _is_youtube_url(text):
        return

    url = _extract_youtube_url(text)
    vid = _extract_video_id(url)

    if vid and _is_video_processed(vid):
        await message.answer("⚠️ Уже обработано ранее")
        return

    tid = _add_task_to_queue(video_url=url, video_id=vid, priority=10, source="telegram")
    if tid:
        await message.answer(
            f"✅ Добавлено: <b>#{tid}</b>\n"
            f"Приоритет: 10 (ручная)\n"
            f"Обработается после текущей задачи"
        )
    else:
        await message.answer("⏭ Уже в очереди")


# ╔══════════════════════════════════════════════════════════╗
# ║                       ЗАПУСК                             ║
# ╚══════════════════════════════════════════════════════════╝

async def _main():
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = asyncio.Event()

    print("🔍 [1/10] _main() стартовал")
    print(f"🔍 sys.argv = {sys.argv}")
    print(f"🔍 CWD = {os.getcwd()}")

    def _handle_shutdown_signal():
        print("\n⏹ Получен сигнал завершения")
        _SHUTDOWN_EVENT.set()

    try:
        loop = asyncio.get_running_loop()
        print(f"🔍 [2/10] Running loop: {type(loop).__name__}")
    except Exception as e:
        print(f"⚠️  get_running_loop: {e}")

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_shutdown_signal)
                print(f"🔍 signal {sig.name} зарегистрирован")
            except (NotImplementedError, RuntimeError, ValueError) as _e:
                print(f"🔍 signal {sig.name} не зарегистрирован ({type(_e).__name__})")
    except Exception as e:
        print(f"⚠️  signal setup: {e}")

    print("🔍 [3/10] CLI checks")
    if "--predictions" in sys.argv:
        _init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY prediction_date DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            print("Нет прогнозов")
            return
        for r in rows:
            m = {"confirmed": "✅", "refuted": "❌", "unverifiable": "❔"}.get(r["verdict"], "⏳")
            print(f"{m} [{r['id']}] {r['expert_name']}: {r['prediction_text'][:100]}")
        return

    if "--mark" in sys.argv:
        _init_db()
        idx = sys.argv.index("--mark")
        try:
            pid = int(sys.argv[idx + 1])
            v = sys.argv[idx + 2]
        except (IndexError, ValueError):
            print("python bot.py --mark <id> confirmed|refuted|unverifiable")
            return
        if v not in ("confirmed", "refuted", "unverifiable"):
            print("❌ Неверный вердикт")
            return
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE predictions SET verdict = ?, verdict_date = CURRENT_TIMESTAMP WHERE id = ?",
            (v, pid)
        )
        conn.commit()
        conn.close()
        print(f"✅ [{pid}] → {v}")
        return

    if "--queue" in sys.argv:
        _init_db()
        for t in _queue_list():
            ic = "🔄" if t['status'] == 'processing' else "⏳"
            src = "👤" if t['source'] == 'telegram' else "🤖"
            print(f"{ic}{src} #{t['id']} [P{t['priority']}] "
                  f"(попыток: {t['attempts']}) "
                  f"{(t['video_title'] or t['video_url'])[:60]}")
        return

    if "--errors" in sys.argv:
        _init_db()
        errs = _recent_errors(20)
        if not errs:
            print("✅ Ошибок нет")
            return
        for e in errs:
            print(f"❌ #{e['id']} (попыток: {e['attempts']}): "
                  f"{(e['video_title'] or e['video_url'])[:50]}")
            print(f"   {e['error_message'] or '—'}")
        return

    if "--cancel" in sys.argv:
        _init_db()
        idx = sys.argv.index("--cancel")
        try:
            tid = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("python bot.py --cancel <id>")
            return
        ok, msg = _cancel_task(tid)
        print(("✅" if ok else "❌") + f" {msg}")
        return

    if "--reset" in sys.argv:
        _init_db()
        n = _reset_queue()
        print(f"🛑 Сброшено в error: {n}")
        return

    if "--process" in sys.argv:
        _init_db()
        idx = sys.argv.index("--process")
        try:
            url = sys.argv[idx + 1]
        except IndexError:
            print("python bot.py --process <url>")
            return
        info = get_video_info_ytdlp(url)
        ok, reason = await process_and_publish(
            url,
            video_title=info.get("title", ""),
            video_description=info.get("description", ""),
            video_duration=info.get("duration", 0),
            video_published_at=info.get("published_at", ""),
            channel_name=info.get("channel", ""),
            channel_type="media",
            skip_duration_check=True,
        )
        print(f"\n{'=' * 60}")
        print(f"Результат: {'✅ OK' if ok else '❌ ' + reason}")
        print(f"{'=' * 60}")
        await bot.session.close()
        return

    print("🔍 [4/10] перед _init_db")
    _cleanup_temp_files()
    _init_db()
    print("🔍 [5/10] после _init_db")

    if TEST_VIDEO_URL:
        print("\n🧪 ТЕСТ")
        ok, reason = await process_and_publish(TEST_VIDEO_URL, video_title=TEST_VIDEO_TITLE)
        print(f"Тест: {'✅ OK' if ok else '❌ ' + reason}")

    print("🔍 [6/10] перед check_all_channels")
    await check_all_channels()
    print("🔍 [7/10] после check_all_channels")

    print("🔍 [8/10] создаю воркер и планировщик")
    worker_task = asyncio.create_task(_worker_loop())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_all_channels, "interval", hours=CHECK_INTERVAL_HOURS)

    if HEARTBEAT_HOURS > 0:
        scheduler.add_job(_heartbeat_job, "interval", hours=HEARTBEAT_HOURS)

    scheduler.start()
    print("🔍 [9/10] scheduler запущен")

    print(f"\n{'=' * 60}")
    print("🚀 БОТ ГОТОВ")
    print(f"   Воркер: запущен")
    print(f"   Планировщик: каждые {CHECK_INTERVAL_HOURS} ч")
    print(f"   Глубина: {HOURS_BACK} ч")
    print(f"   Whisper: {WHISPER_MODE} ({GROQ_WHISPER_MODEL})")
    print(f"   Модель LLM: {GROQ_MODEL}")
    print(f"   Owner: {OWNER_USER_ID}")
    print(f"   БД: {DB_PATH}")
    print(f"{'=' * 60}\n")

    print("🔍 [10/10] запускаю dp.start_polling()")
    try:
        await dp.start_polling(bot)
    finally:
        print("\n⏹ Останавливаюсь...")
        _SHUTDOWN_EVENT.set()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        print("⏳ Ожидание завершения текущей задачи (до 10 мин)...")
        try:
            await asyncio.wait_for(worker_task, timeout=600)
            print("✅ Воркер завершён")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            print("⏱ Таймаут — отменяю воркер")
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as e:
            print(f"⚠️  Ошибка при остановке: {e}")
        try:
            await bot.session.close()
        except Exception:
            pass
        print("⏹ Завершено")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n\n⏹ Остановлено (Ctrl+C)")
    except BaseException as e:
        print(f"\n\n❌ КРИТИЧЕСКАЯ ОШИБКА: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.stderr.flush()